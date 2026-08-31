from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.services import record_audit
from inventory.models import (
    ExpectedReturn,
    FIFOAllocation,
    FIFOOpeningSnapshot,
    FIFOLayer,
    InboundReceipt,
    InventoryException,
    InventoryMovement,
    PhysicalReturnReceipt,
    QCFollowUp,
    QCFollowUpEvent,
    QCInspection,
)
from purchasing.models import PurchaseOrder


CUTOVER_DATE = date(2026, 7, 31)


def qc_approved_qty(po_line):
    """Return sellable QC quantity, including units that pass Re-QC."""
    initial_passed = po_line.qc_passed_before_cutover_qty + (
        po_line.qc_inspections.aggregate(total=Sum("qty_passed"))["total"] or Decimal("0")
    )
    re_qc_passed = po_line.qc_follow_ups.aggregate(total=Sum("resolved_passed_qty"))["total"] or Decimal("0")
    return initial_passed + re_qc_passed


def ensure_qc_follow_up(inspection):
    if inspection.qty_failed <= 0 or not inspection.failed_disposition:
        return None
    status_map = {
        QCInspection.Disposition.REWORK: QCFollowUp.Status.AWAITING_REWORK,
        QCInspection.Disposition.REJECTED: QCFollowUp.Status.REJECTED,
        QCInspection.Disposition.ACCEPTED_EXCEPTION: QCFollowUp.Status.ACCEPTED_EXCEPTION,
    }
    follow_up, created = QCFollowUp.objects.get_or_create(
        source_inspection=inspection,
        defaults={
            "po_line": inspection.po_line,
            "status": status_map[inspection.failed_disposition],
            "original_failed_qty": inspection.qty_failed,
            "open_qty": inspection.qty_failed,
        },
    )
    if created:
        QCFollowUpEvent.objects.create(
            follow_up=follow_up,
            event_type=QCFollowUpEvent.EventType.CREATED,
            activity_date=timezone.localtime(inspection.inspected_at).date(),
            qty_inspected=inspection.qty_inspected,
            qty_passed=inspection.qty_passed,
            qty_failed=inspection.qty_failed,
            failed_disposition=inspection.failed_disposition,
            notes=inspection.notes,
            actor=inspection.recorded_by,
        )
    return follow_up


@transaction.atomic
def complete_qc_rework(*, follow_up, activity_date, notes, actor):
    follow_up = QCFollowUp.objects.select_for_update().get(pk=follow_up.pk)
    if follow_up.status != QCFollowUp.Status.AWAITING_REWORK:
        raise ValidationError("Hanya barang berstatus Menunggu Rework yang dapat diselesaikan.")
    follow_up.status = QCFollowUp.Status.READY_RE_QC
    follow_up.rework_cycle += 1
    follow_up.save(update_fields=("status", "rework_cycle", "updated_at"))
    event = QCFollowUpEvent.objects.create(
        follow_up=follow_up,
        event_type=QCFollowUpEvent.EventType.REWORK_COMPLETED,
        activity_date=activity_date,
        qty_inspected=follow_up.open_qty,
        notes=(notes or "").strip(),
        actor=actor,
    )
    record_audit(
        actor=actor,
        action="qc_rework_completed",
        entity_type="inventory.qcfollowup",
        entity_id=follow_up.id,
        after_values={"status": follow_up.status, "open_qty": str(follow_up.open_qty)},
    )
    return event


@transaction.atomic
def record_re_qc(*, follow_up, activity_date, qty_passed, failed_disposition, notes, actor):
    follow_up = QCFollowUp.objects.select_for_update().select_related("po_line__sku").get(pk=follow_up.pk)
    if follow_up.status != QCFollowUp.Status.READY_RE_QC:
        raise ValidationError("Re-QC hanya dapat dicatat setelah Rework Selesai.")
    inspected = Decimal(follow_up.open_qty)
    passed = Decimal(qty_passed)
    if passed != passed.to_integral_value() or passed < 0 or passed > inspected:
        raise ValidationError(f"Qty Lolos Re-QC harus antara 0 dan {inspected:.0f} pcs.")
    failed = inspected - passed
    if failed > 0 and failed_disposition not in {
        QCInspection.Disposition.REWORK,
        QCInspection.Disposition.REJECTED,
    }:
        raise ValidationError("Barang gagal Re-QC wajib diputuskan Rework atau Rejected.")
    before = {"status": follow_up.status, "open_qty": str(follow_up.open_qty)}
    follow_up.resolved_passed_qty += passed
    follow_up.open_qty = failed
    if failed == 0:
        follow_up.status = QCFollowUp.Status.RESOLVED
        failed_disposition = ""
    elif failed_disposition == QCInspection.Disposition.REWORK:
        follow_up.status = QCFollowUp.Status.AWAITING_REWORK
    else:
        follow_up.status = QCFollowUp.Status.REJECTED
    follow_up.save(update_fields=("resolved_passed_qty", "open_qty", "status", "updated_at"))
    event = QCFollowUpEvent.objects.create(
        follow_up=follow_up,
        event_type=QCFollowUpEvent.EventType.RE_QC,
        activity_date=activity_date,
        qty_inspected=inspected,
        qty_passed=passed,
        qty_failed=failed,
        failed_disposition=failed_disposition,
        notes=(notes or "").strip(),
        actor=actor,
    )
    record_audit(
        actor=actor,
        action="qc_reinspection_recorded",
        entity_type="inventory.qcfollowup",
        entity_id=follow_up.id,
        before_values=before,
        after_values={
            "status": follow_up.status,
            "inspected": str(inspected),
            "passed": str(passed),
            "failed": str(failed),
        },
    )
    return event


@transaction.atomic
def post_opening(*, sku, quantity, unit_cost, actor, warehouse=None, reason="FIFO cutover opening"):
    quantity = Decimal(quantity)
    unit_cost = Decimal(unit_cost)
    key = f"OPENING|20260731|{sku.sku}"
    if FIFOOpeningSnapshot.objects.filter(sku=sku).exists():
        raise ValidationError("Opening SKU ini sudah pernah diposting.")
    if unit_cost < 0:
        raise ValidationError("Frozen Unit COGS tidak boleh negatif.")
    FIFOOpeningSnapshot.objects.create(
        sku=sku,
        cutover_date=CUTOVER_DATE,
        opening_qty=quantity,
        frozen_unit_cogs=unit_cost,
        recorded_by=actor,
    )
    if quantity < 0:
        if InventoryException.objects.filter(code=InventoryException.Code.NEGATIVE_OPENING, sku=sku, status=InventoryException.Status.OPEN).exists():
            raise ValidationError("Negative Opening exception SKU ini sudah tercatat.")
        InventoryException.objects.create(
            code=InventoryException.Code.NEGATIVE_OPENING,
            sku=sku,
            quantity=abs(quantity),
            message=f"Opening Qty {quantity} tidak membentuk cost layer.",
        )
        record_audit(
            actor=actor,
            action="fifo_opening_exception_recorded",
            entity_type="master_data.sku",
            entity_id=sku.id,
            reason=reason,
            after_values={"quantity": str(quantity), "frozen_unit_cogs": str(unit_cost)},
        )
        return None
    if quantity == 0:
        record_audit(
            actor=actor,
            action="fifo_zero_opening_snapshotted",
            entity_type="master_data.sku",
            entity_id=sku.id,
            reason=reason,
            after_values={"quantity": "0", "frozen_unit_cogs": str(unit_cost)},
        )
        return None
    movement = InventoryMovement.objects.create(
        movement_key=key,
        movement_date=CUTOVER_DATE,
        movement_type=InventoryMovement.MovementType.OPENING,
        direction=InventoryMovement.Direction.IN,
        sku=sku,
        warehouse=warehouse,
        quantity=quantity,
        allocated_cost=quantity * unit_cost,
        source_reference="FIFO Opening EOD 2026-07-31",
        reason=reason,
        posted_by=actor,
    )
    FIFOLayer.objects.create(
        layer_key=key,
        sku=sku,
        source_type=FIFOLayer.SourceType.OPENING,
        source_reference="Opening",
        receipt_date=CUTOVER_DATE,
        original_qty=quantity,
        remaining_qty=quantity,
        unit_cost=unit_cost,
        opening_movement=movement,
    )
    record_audit(
        actor=actor,
        action="fifo_opening_posted",
        entity_type="inventory.inventorymovement",
        entity_id=movement.id,
        reason=reason,
        after_values={"quantity": str(quantity), "frozen_unit_cogs": str(unit_cost)},
    )
    return movement


def _consume_fifo(movement):
    required = Decimal(movement.quantity)
    total_cost = Decimal("0")
    layers = FIFOLayer.objects.select_for_update().filter(
        sku=movement.sku,
        remaining_qty__gt=0,
        receipt_date__lte=movement.movement_date,
    ).order_by("receipt_date", "created_at", "layer_key")
    for layer in layers:
        if required <= 0:
            break
        allocated = min(required, layer.remaining_qty)
        cost = allocated * layer.unit_cost
        FIFOAllocation.objects.create(
            outbound_movement=movement,
            layer=layer,
            allocated_qty=allocated,
            unit_cost=layer.unit_cost,
            allocated_cost=cost,
        )
        layer.remaining_qty -= allocated
        layer.save(update_fields=["remaining_qty"])
        required -= allocated
        total_cost += cost
    movement.allocated_cost = total_cost
    movement.save(update_fields=["allocated_cost"])
    if required > 0:
        InventoryException.objects.create(
            code=InventoryException.Code.FIFO_SHORT,
            sku=movement.sku,
            movement=movement,
            quantity=required,
            message=f"Sales Out melebihi FIFO layer tersedia sebanyak {required} unit.",
        )
    return total_cost, required


@transaction.atomic
def post_sales_out(sales_line, actor):
    key = f"SALES|{sales_line.order.display_source}|{sales_line.order.order_number}|{sales_line.sku.sku}"
    existing = InventoryMovement.objects.filter(movement_key=key).first()
    if existing:
        return existing
    movement = InventoryMovement.objects.create(
        movement_key=key,
        movement_date=sales_line.order.order_date,
        movement_type=InventoryMovement.MovementType.SALES_OUT,
        direction=InventoryMovement.Direction.OUT,
        sku=sales_line.sku,
        quantity=sales_line.quantity,
        source_reference=f"{sales_line.order.display_source}|{sales_line.order.order_number}",
        sales_line=sales_line,
        posted_by=actor,
    )
    _, short_qty = _consume_fifo(movement)
    record_audit(
        actor=actor,
        action="sales_out_posted",
        entity_type="inventory.inventorymovement",
        entity_id=movement.id,
        after_values={"quantity": str(movement.quantity), "fifo_short_qty": str(short_qty)},
    )
    return movement


@transaction.atomic
def record_qc(*, po_line, inspected_at, qty_inspected, qty_passed, qty_failed, actor, disposition="", notes=""):
    if po_line.po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("QC hanya boleh dicatat untuk PO Released.")
    from production.models import ProductionStage
    from production.services import ensure_production_order, qc_is_open, qc_line_availability

    production_order = ensure_production_order(po_line.po, actor=actor)
    if not qc_is_open(production_order):
        raise ValidationError("QC menunggu Qty output Trim yang belum pernah diperiksa.")
    if inspected_at.date() <= CUTOVER_DATE:
        raise ValidationError("QC operasional ERP dimulai 1 August 2026; histori sampai 31 July hanya melalui PO WIP migration.")
    quantities = [Decimal(qty_inspected), Decimal(qty_passed), Decimal(qty_failed)]
    if quantities[1] + quantities[2] != quantities[0]:
        raise ValidationError("Qty QC Passed + Qty QC Failed harus sama dengan Qty Inspected.")
    trim_completed = production_order.stages.get(stage=ProductionStage.Stage.TRIM).completed_qty
    po_inspected = sum(
        (
            line.qc_passed_before_cutover_qty
            + (line.qc_inspections.aggregate(total=Sum("qty_inspected"))["total"] or Decimal("0"))
            for line in po_line.po.lines.all()
        ),
        Decimal("0"),
    )
    if po_inspected + quantities[0] > trim_completed:
        raise ValidationError(
            f"Cumulative Qty Inspected tidak boleh melebihi output Trim ({trim_completed:.0f} pcs)."
        )
    line_availability = qc_line_availability(production_order, po_line)
    line_limit = line_availability["trim_qty"] or Decimal(po_line.ordered_qty)
    line_inspected = line_availability["inspected_qty"]
    if line_inspected + quantities[0] > line_limit:
        raise ValidationError(
            f"Cumulative Qty Inspected tidak boleh melebihi output Trim SKU "
            f"({line_limit:.0f} pcs)."
        )
    qc = QCInspection(
        po_line=po_line,
        inspected_at=inspected_at,
        qty_inspected=quantities[0],
        qty_passed=quantities[1],
        qty_failed=quantities[2],
        failed_disposition=disposition,
        notes=notes,
        recorded_by=actor,
    )
    qc.full_clean()
    qc.save()
    ensure_qc_follow_up(qc)
    record_audit(
        actor=actor,
        action="qc_recorded",
        entity_type="inventory.qcinspection",
        entity_id=qc.id,
        after_values={"inspected": str(qc.qty_inspected), "passed": str(qc.qty_passed), "failed": str(qc.qty_failed)},
    )
    return qc


@transaction.atomic
def record_inbound(
    *,
    po_line,
    inbound_date,
    received_qty,
    warehouse,
    reference,
    actor,
    notes="",
    delivery_activity=None,
):
    if po_line.po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Inbound hanya boleh dicatat untuk PO Released.")
    if inbound_date <= CUTOVER_DATE:
        raise ValidationError("Inbound operasional ERP dimulai 1 August 2026; receipt sampai 31 July sudah terserap ke FIFO Opening.")
    received_qty = Decimal(received_qty)
    if delivery_activity is not None:
        if (
            delivery_activity.activity_type != "WAREHOUSE_DELIVERY"
            or delivery_activity.entry_kind != "ACTIVITY"
            or delivery_activity.po_line_id != po_line.id
        ):
            raise ValidationError("Pengiriman Production tidak sesuai dengan SKU yang diterima.")
        if inbound_date < delivery_activity.activity_date:
            raise ValidationError(
                f"Tanggal Diterima minimal {delivery_activity.activity_date:%d/%m/%Y}, "
                "sama dengan Tanggal Kirim."
            )
        effective = (
            delivery_activity.correction_entries.filter(entry_kind="CORRECTION")
            .order_by("-occurred_at")
            .first()
            or delivery_activity
        )
        received_for_delivery = (
            delivery_activity.inbound_receipts.aggregate(total=Sum("received_qty"))["total"]
            or Decimal("0")
        )
        if received_for_delivery + received_qty > Decimal(effective.quantity or 0):
            raise ValidationError("Qty diterima tidak boleh melebihi sisa Qty Delivering.")
    passed = qc_approved_qty(po_line)
    already_received = (
        po_line.received_before_cutover_qty
        + (po_line.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0"))
    )
    if already_received + received_qty > passed:
        raise ValidationError("Cumulative Received Qty tidak boleh melebihi cumulative Qty QC Passed.")
    if po_line.cogs_snapshot is None:
        raise ValidationError("COGS snapshot PO belum tersedia.")
    receipt = InboundReceipt(
        po_line=po_line,
        inbound_date=inbound_date,
        received_qty=received_qty,
        warehouse=warehouse,
        reference=reference,
        retail_price_snapshot=po_line.sku.current_retail_price,
        notes=notes,
        recorded_by=actor,
        delivery_activity=delivery_activity,
    )
    receipt.full_clean()
    receipt.save()
    movement_key = f"INBOUND|{po_line.po.po_number}|{po_line.sku.sku}|{inbound_date:%Y%m%d}|{receipt.id}"
    movement = InventoryMovement.objects.create(
        movement_key=movement_key,
        movement_date=inbound_date,
        movement_type=InventoryMovement.MovementType.INCOMING,
        direction=InventoryMovement.Direction.IN,
        sku=po_line.sku,
        warehouse=warehouse,
        quantity=received_qty,
        allocated_cost=received_qty * po_line.cogs_snapshot,
        source_reference=po_line.po.po_number,
        inbound_receipt=receipt,
        posted_by=actor,
    )
    FIFOLayer.objects.create(
        layer_key=movement_key,
        sku=po_line.sku,
        source_type=FIFOLayer.SourceType.PURCHASE_ORDER,
        source_reference=po_line.po.po_number,
        source_po_line=po_line,
        receipt_date=inbound_date,
        original_qty=received_qty,
        remaining_qty=received_qty,
        unit_cost=po_line.cogs_snapshot,
        opening_movement=movement,
    )
    record_audit(
        actor=actor,
        action="inbound_posted",
        entity_type="inventory.inboundreceipt",
        entity_id=receipt.id,
        after_values={"movement_key": movement_key, "quantity": str(received_qty)},
    )
    return receipt, movement


@transaction.atomic
def receive_rejected_goods_delivery(*, delivery_activity, inbound_date, received_qty, warehouse, actor, notes=""):
    from production.models import ProductionActivity

    delivery_activity = ProductionActivity.objects.select_for_update().select_related(
        "delivery_order",
        "po_line__sku",
    ).get(pk=delivery_activity.pk)
    if delivery_activity.activity_type != ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY:
        raise ValidationError("Pengiriman ini bukan Rejected Goods.")
    if warehouse.code != "REJECT":
        raise ValidationError("Rejected Goods hanya dapat diterima di Reject Warehouse.")
    if inbound_date < delivery_activity.activity_date:
        raise ValidationError(
            f"Tanggal Diterima minimal {delivery_activity.activity_date:%d/%m/%Y}, sama dengan Tanggal Kirim."
        )
    follow_ups = list(
        QCFollowUp.objects.select_for_update().filter(
            delivery_activity=delivery_activity,
            status=QCFollowUp.Status.REJECTED,
        )
    )
    if not follow_ups or any(row.delivery_status == QCFollowUp.DeliveryStatus.INBOUND for row in follow_ups):
        raise ValidationError("Rejected Goods ini sudah diterima atau tidak lagi tersedia.")
    expected = sum((row.open_qty for row in follow_ups), Decimal("0"))
    received_qty = Decimal(received_qty)
    if received_qty != expected:
        raise ValidationError(f"Quantity Received harus sama dengan Qty Delivering ({expected:.0f} pcs).")
    QCFollowUp.objects.filter(pk__in=[row.pk for row in follow_ups]).update(
        delivery_status=QCFollowUp.DeliveryStatus.INBOUND,
        delivery_updated_by=actor,
        delivery_updated_at=timezone.now(),
        received_date=inbound_date,
        received_warehouse=warehouse,
        received_by=actor,
    )
    InventoryMovement.objects.create(
        movement_key=f"REJECTED|{delivery_activity.delivery_order.number}|{delivery_activity.po_line.sku.sku}|{delivery_activity.id}",
        movement_date=inbound_date,
        movement_type=InventoryMovement.MovementType.REJECTED_IN,
        direction=InventoryMovement.Direction.IN,
        sku=delivery_activity.po_line.sku,
        warehouse=warehouse,
        quantity=received_qty,
        allocated_cost=Decimal("0"),
        source_reference=delivery_activity.delivery_order.number,
        reason="Rejected Goods diterima fisik; non-sellable dan tanpa FIFO.",
        evidence_reference=delivery_activity.delivery_order.number,
        posted_by=actor,
    )
    record_audit(
        actor=actor,
        action="rejected_goods_received",
        entity_type="production.productionactivity",
        entity_id=delivery_activity.id,
        after_values={
            "delivery_order": delivery_activity.delivery_order.number,
            "sku": delivery_activity.po_line.sku.sku,
            "received_qty": str(received_qty),
            "received_date": str(inbound_date),
            "warehouse": warehouse.name,
            "stock_effect": "NONE",
            "notes": (notes or "").strip(),
        },
    )
    return follow_ups


@transaction.atomic
def undo_unallocated_inbound(*, receipt, actor, reason):
    receipt = InboundReceipt.objects.select_for_update().select_related(
        "delivery_activity__delivery_order",
        "po_line__sku",
        "warehouse",
    ).get(pk=receipt.pk)
    try:
        movement = receipt.movement
        layer = movement.created_fifo_layer
    except (InventoryMovement.DoesNotExist, FIFOLayer.DoesNotExist) as exc:
        raise ValidationError("Receipt tidak memiliki movement dan FIFO layer yang lengkap.") from exc
    if layer.allocations.exists() or layer.remaining_qty != layer.original_qty:
        raise ValidationError("Receipt tidak dapat di-undo karena FIFO layer sudah terpakai.")

    receipt_id = receipt.id
    snapshot = {
        "delivery_order": receipt.delivery_activity.delivery_order.number if receipt.delivery_activity else "",
        "sku": receipt.po_line.sku.sku,
        "quantity": str(receipt.received_qty),
        "inbound_date": str(receipt.inbound_date),
        "warehouse": receipt.warehouse.name,
        "movement_key": movement.movement_key,
    }
    FIFOLayer.objects.filter(pk=layer.pk).delete()
    InventoryMovement.objects.filter(pk=movement.pk).delete()
    InboundReceipt.objects.filter(pk=receipt.pk).delete()
    record_audit(
        actor=actor,
        action="inbound_receipt_undone",
        entity_type="inventory.inboundreceipt",
        entity_id=receipt_id,
        reason=reason,
        before_values=snapshot,
    )
    return snapshot


@transaction.atomic
def create_expected_return(sales_line):
    expected, _ = ExpectedReturn.objects.update_or_create(
        sales_line=sales_line,
        defaults={"expected_qty": sales_line.quantity},
    )
    return expected


@transaction.atomic
def record_physical_return(*, sales_line, received_date, quantity, warehouse, condition, actor, notes=""):
    if received_date <= CUTOVER_DATE:
        raise ValidationError("Return Log operasional ERP dimulai 1 August 2026.")
    quantity = Decimal(quantity)
    already = sales_line.physical_returns.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    if already + quantity > sales_line.quantity:
        raise ValidationError("Cumulative physical return tidak boleh melebihi Sales Out asli.")
    receipt = PhysicalReturnReceipt(
        sales_line=sales_line,
        received_date=received_date,
        quantity=quantity,
        warehouse=warehouse,
        condition=condition,
        notes=notes,
        recorded_by=actor,
    )
    receipt.full_clean()
    receipt.save()
    expected = ExpectedReturn.objects.filter(sales_line=sales_line).first()
    if expected:
        total_after = already + quantity
        expected.status = ExpectedReturn.Status.RECEIVED if total_after == expected.expected_qty else ExpectedReturn.Status.PARTIALLY_RECEIVED
        expected.save(update_fields=["status"])
    if condition != PhysicalReturnReceipt.Condition.SELLABLE:
        record_audit(
            actor=actor,
            action="physical_return_non_sellable_recorded",
            entity_type="inventory.physicalreturnreceipt",
            entity_id=receipt.id,
            after_values={"condition": condition, "quantity": str(quantity)},
        )
        return receipt, None
    sales_movement = sales_line.inventory_movements.filter(movement_type=InventoryMovement.MovementType.SALES_OUT).first()
    if sales_movement is None:
        InventoryException.objects.create(
            code=InventoryException.Code.RETURN_SOURCE_MISSING,
            sku=sales_line.sku,
            quantity=quantity,
            message="Sales Out asal belum memiliki FIFO allocation; Return In tidak diposting.",
        )
        return receipt, None
    restore_needed = quantity
    restored_cost = Decimal("0")
    allocations = list(
        sales_movement.fifo_allocations.select_for_update(of=("self", "layer")).select_related("layer__source_po_line__po").order_by(
            "layer__receipt_date", "layer__created_at"
        )
    )
    restoration_parts = []
    for allocation in allocations:
        if restore_needed <= 0:
            break
        restorable = allocation.allocated_qty - allocation.returned_qty
        restored = min(restore_needed, restorable)
        if restored <= 0:
            continue
        allocation.returned_qty += restored
        allocation.save(update_fields=["returned_qty"])
        allocation.layer.remaining_qty += restored
        allocation.layer.save(update_fields=["remaining_qty"])
        restored_cost += restored * allocation.unit_cost
        restore_needed -= restored
        restoration_parts.append({"layer": allocation.layer.layer_key, "qty": str(restored)})
    if restore_needed > 0:
        raise ValidationError("FIFO allocation asal tidak cukup untuk memulihkan return.")
    key = f"RETURN|{sales_line.order.display_source}|{sales_line.order.order_number}|{sales_line.sku.sku}|{receipt.id}"
    movement = InventoryMovement.objects.create(
        movement_key=key,
        movement_date=received_date,
        movement_type=InventoryMovement.MovementType.RETURN_IN,
        direction=InventoryMovement.Direction.IN,
        sku=sales_line.sku,
        warehouse=warehouse,
        quantity=quantity,
        allocated_cost=restored_cost,
        source_reference=f"{sales_line.order.display_source}|{sales_line.order.order_number}",
        return_receipt=receipt,
        posted_by=actor,
    )
    po_ids = {
        allocation.layer.source_po_line.po_id
        for allocation in allocations
        if allocation.layer.source_po_line_id
    }
    for po_id in po_ids:
        po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
        if po.close_date:
            po.close_date = None
            po.reopened_at = timezone.now()
            po.save(update_fields=["close_date", "reopened_at"])
    record_audit(
        actor=actor,
        action="sellable_return_posted",
        entity_type="inventory.inventorymovement",
        entity_id=movement.id,
        after_values={"quantity": str(quantity), "restored_cost": str(restored_cost), "layers": restoration_parts},
    )
    return receipt, movement


def inventory_balance(sku, as_of_date=None):
    movements = InventoryMovement.objects.filter(sku=sku).exclude(
        movement_type=InventoryMovement.MovementType.OPENING
    )
    if as_of_date is not None:
        movements = movements.filter(movement_date__lte=as_of_date)
    opening = FIFOOpeningSnapshot.objects.filter(sku=sku)
    if as_of_date is not None:
        opening = opening.filter(cutover_date__lte=as_of_date)
    opening_qty = opening.values_list("opening_qty", flat=True).first() or Decimal("0")
    incoming = movements.filter(direction=InventoryMovement.Direction.IN).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    outgoing = movements.filter(direction=InventoryMovement.Direction.OUT).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    return opening_qty + incoming - outgoing


@transaction.atomic
def post_adjustment(*, sku, movement_date, direction, quantity, actor, reason, evidence_reference, unit_cost=None, warehouse=None, exception=None):
    quantity = Decimal(quantity)
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValidationError("Adjustment Qty harus bilangan bulat positif.")
    if not reason.strip() or not evidence_reference.strip():
        raise ValidationError("Adjustment wajib memiliki alasan dan evidence/reference.")
    if exception:
        exception = InventoryException.objects.select_for_update().select_related("movement").get(pk=exception.pk)
        if exception.status != InventoryException.Status.OPEN or exception.sku_id != sku.id:
            raise ValidationError("Exception tidak open atau SKU tidak cocok.")
    key = f"ADJUSTMENT|{movement_date:%Y%m%d}|{sku.sku}|{timezone.now():%Y%m%d%H%M%S%f}"
    is_in = direction == InventoryMovement.Direction.IN
    if direction not in InventoryMovement.Direction.values:
        raise ValidationError("Direction adjustment tidak valid.")
    movement = InventoryMovement.objects.create(
        movement_key=key,
        movement_date=movement_date,
        movement_type=InventoryMovement.MovementType.ADJUSTMENT_IN if is_in else InventoryMovement.MovementType.ADJUSTMENT_OUT,
        direction=direction,
        sku=sku,
        warehouse=warehouse,
        quantity=quantity,
        source_reference=evidence_reference,
        reason=reason,
        evidence_reference=evidence_reference,
        posted_by=actor,
    )
    layer = None
    if is_in:
        if unit_cost is None or Decimal(unit_cost) < 0:
            raise ValidationError("Adjustment In wajib memiliki approved Unit Cost nonnegative.")
        unit_cost = Decimal(unit_cost)
        movement.allocated_cost = quantity * unit_cost
        movement.save(update_fields=["allocated_cost"])
        layer = FIFOLayer.objects.create(
            layer_key=key,
            sku=sku,
            source_type=FIFOLayer.SourceType.ADJUSTMENT,
            source_reference=evidence_reference,
            receipt_date=movement_date,
            original_qty=quantity,
            remaining_qty=quantity,
            unit_cost=unit_cost,
            opening_movement=movement,
        )
    else:
        _, short = _consume_fifo(movement)
        if short:
            raise ValidationError("Adjustment Out melebihi FIFO layer tersedia; koreksi sumber sebelum posting.")
    if exception:
        if not is_in:
            raise ValidationError("Resolusi Negative/FIFO Short membutuhkan Adjustment In yang sah.")
        if exception.code == InventoryException.Code.FIFO_SHORT:
            if not exception.movement_id or movement_date > exception.movement.movement_date:
                raise ValidationError("Evidence date adjustment harus pada/sebelum tanggal Sales Out yang mengalami FIFO Short.")
            resolved_qty = min(exception.quantity, layer.remaining_qty)
            FIFOAllocation.objects.create(
                outbound_movement=exception.movement,
                layer=layer,
                allocated_qty=resolved_qty,
                unit_cost=layer.unit_cost,
                allocated_cost=resolved_qty * layer.unit_cost,
            )
            layer.remaining_qty -= resolved_qty
            layer.save(update_fields=["remaining_qty"])
            exception.movement.allocated_cost += resolved_qty * layer.unit_cost
            exception.movement.save(update_fields=["allocated_cost"])
            exception.quantity -= resolved_qty
        elif exception.code == InventoryException.Code.NEGATIVE_OPENING:
            exception.quantity = max(exception.quantity - quantity, Decimal("0"))
        else:
            raise ValidationError("Tipe exception ini belum dapat diselesaikan oleh adjustment stock.")
        if exception.quantity == 0:
            exception.status = InventoryException.Status.RESOLVED
            exception.resolved_at = timezone.now()
            exception.resolved_by = actor
            exception.resolution_movement = movement
            exception.resolution_reason = reason
            exception.save()
        else:
            exception.resolution_movement = movement
            exception.resolution_reason = reason
            exception.save(update_fields=["quantity", "resolution_movement", "resolution_reason"])
    record_audit(
        actor=actor,
        action="inventory_adjustment_posted",
        entity_type="inventory.inventorymovement",
        entity_id=movement.id,
        reason=reason,
        after_values={"direction": direction, "quantity": str(quantity), "evidence": evidence_reference, "exception": str(exception.id) if exception else ""},
    )
    return movement
