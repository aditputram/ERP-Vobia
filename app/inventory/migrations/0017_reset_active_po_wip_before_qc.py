import uuid
from datetime import date, datetime
from decimal import Decimal

from django.db import migrations, transaction
from django.db.models import Sum


TARGETS = {
    "PO-VOB-06/26-084": Decimal("1"),
    "PO-VOB-06/26-087": Decimal("12"),
    "PO-VOB-06/26-088": Decimal("93"),
    "PO-VOB-06/26-090": Decimal("0"),
    "PO-VOB-06/26-093": Decimal("235"),
}
EXPECTED_TOTAL = sum(TARGETS.values(), Decimal("0"))
AUDIT_KEY = "five-active-po-wip-reset-before-qc"


def _json_value(value):
    if isinstance(value, (Decimal, uuid.UUID, date, datetime)):
        return str(value)
    return value


def _snapshot(queryset):
    model = queryset.model
    return [
        {
            field.attname: _json_value(getattr(obj, field.attname))
            for field in model._meta.concrete_fields
        }
        for obj in queryset.order_by(model._meta.pk.name)
    ]


def _reset(apps, schema_editor):
    database = schema_editor.connection.alias
    if schema_editor.connection.vendor != "postgresql":
        return

    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    ProductionOrder = apps.get_model("production", "ProductionOrder")
    ProductionActivity = apps.get_model("production", "ProductionActivity")
    ProductionDeliveryOrder = apps.get_model("production", "ProductionDeliveryOrder")
    QCInspection = apps.get_model("inventory", "QCInspection")
    InboundReceipt = apps.get_model("inventory", "InboundReceipt")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")
    FIFOAllocation = apps.get_model("inventory", "FIFOAllocation")
    InventoryException = apps.get_model("inventory", "InventoryException")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    pos = list(
        PurchaseOrder.objects.using(database).select_for_update().filter(
            po_number__in=TARGETS
        ).order_by("po_number")
    )
    if not pos:
        return
    if {po.po_number for po in pos} != set(TARGETS):
        raise RuntimeError("Reset QC dibatalkan: lima PO target tidak lengkap.")
    if any(po.source != "LEGACY_WIP" or po.status != "RELEASED" for po in pos):
        raise RuntimeError("Reset QC dibatalkan: target bukan Released Legacy PO WIP.")

    po_ids = [po.pk for po in pos]
    production_orders = ProductionOrder.objects.using(database).filter(po_id__in=po_ids)
    production_order_ids = list(production_orders.values_list("id", flat=True))
    if len(production_order_ids) != len(TARGETS) or production_orders.exclude(
        plan__status="ACTIVE"
    ).exists():
        raise RuntimeError("Reset QC dibatalkan: target bukan lima Production Plan Active.")

    lines = PurchaseOrderLine.objects.using(database).filter(po_id__in=po_ids)
    line_ids = list(lines.values_list("id", flat=True))
    if QCInspection.objects.using(database).filter(po_line_id__in=line_ids).exists():
        raise RuntimeError("Reset QC dibatalkan: sudah ada QC operasional aktual.")

    deliveries = ProductionActivity.objects.using(database).filter(
        production_order_id__in=production_order_ids,
        entry_kind="ACTIVITY",
        activity_type="WAREHOUSE_DELIVERY",
    )
    delivery_ids = list(deliveries.values_list("id", flat=True))
    if ProductionActivity.objects.using(database).filter(source_activity_id__in=delivery_ids).exists():
        raise RuntimeError("Reset QC dibatalkan: Delivery sudah memiliki koreksi.")

    delivery_by_po = {
        po_number: deliveries.filter(production_order__po__po_number=po_number).aggregate(
            total=Sum("quantity")
        )["total"] or Decimal("0")
        for po_number in TARGETS
    }
    if delivery_by_po != TARGETS:
        raise RuntimeError(f"Reset QC dibatalkan: Qty Delivery berubah ({delivery_by_po}).")

    receipts = InboundReceipt.objects.using(database).filter(delivery_activity_id__in=delivery_ids)
    receipt_ids = list(receipts.values_list("id", flat=True))
    inbound_by_po = {
        po_number: receipts.filter(po_line__po__po_number=po_number).aggregate(
            total=Sum("received_qty")
        )["total"] or Decimal("0")
        for po_number in TARGETS
    }
    if inbound_by_po != TARGETS:
        raise RuntimeError(f"Reset QC dibatalkan: Qty Inbound berubah ({inbound_by_po}).")

    movements = InventoryMovement.objects.using(database).filter(inbound_receipt_id__in=receipt_ids)
    movement_ids = list(movements.values_list("id", flat=True))
    if movements.count() != len(receipt_ids):
        raise RuntimeError("Reset QC dibatalkan: movement Inbound tidak lengkap.")
    layers = FIFOLayer.objects.using(database).filter(opening_movement_id__in=movement_ids)
    layer_ids = list(layers.values_list("id", flat=True))
    layer_totals = layers.aggregate(original=Sum("original_qty"), remaining=Sum("remaining_qty"))
    if (
        layers.count() != len(movement_ids)
        or (layer_totals["original"] or Decimal("0")) != EXPECTED_TOTAL
        or (layer_totals["remaining"] or Decimal("0")) != EXPECTED_TOTAL
    ):
        raise RuntimeError("Reset QC dibatalkan: FIFO layer 341 pcs tidak lagi utuh.")
    if FIFOAllocation.objects.using(database).filter(layer_id__in=layer_ids).exists():
        raise RuntimeError("Reset QC dibatalkan: stock Inbound sudah dipakai Sales/FIFO.")
    if InventoryException.objects.using(database).filter(movement_id__in=movement_ids).exists():
        raise RuntimeError("Reset QC dibatalkan: movement Inbound memiliki exception.")

    delivery_order_ids = list(deliveries.values_list("delivery_order_id", flat=True).distinct())
    delivery_orders = ProductionDeliveryOrder.objects.using(database).filter(pk__in=delivery_order_ids)
    if ProductionActivity.objects.using(database).filter(delivery_order_id__in=delivery_order_ids).exclude(
        pk__in=delivery_ids
    ).exists():
        raise RuntimeError("Reset QC dibatalkan: Delivery Order dipakai activity lain.")

    AuditEvent.objects.using(database).create(
        actor=None,
        action="active_po_wip_reset_before_qc_committed",
        entity_type="production.productionorder",
        entity_id=AUDIT_KEY,
        reason="Reset lima PO WIP aktif ke posisi sebelum QC atas persetujuan Adit.",
        before_values={
            "purchasing.PurchaseOrderLine": _snapshot(lines),
            "production.ProductionDeliveryOrder": _snapshot(delivery_orders),
            "production.ProductionActivity": _snapshot(deliveries),
            "inventory.InboundReceipt": _snapshot(receipts),
            "inventory.InventoryMovement": _snapshot(movements),
            "inventory.FIFOLayer": _snapshot(layers),
        },
        after_values={
            "target_po_numbers": list(TARGETS),
            "delivery_qty": "0",
            "inbound_qty": "0",
            "legacy_qc_baseline_qty": "0",
        },
        metadata={"inventory_qty_removed": str(EXPECTED_TOTAL)},
    )

    layers.delete()
    movements.delete()
    receipts.delete()
    deliveries.delete()
    delivery_orders.delete()
    lines.update(qc_passed_before_cutover_qty=Decimal("0"))

    if (
        InboundReceipt.objects.using(database).filter(po_line_id__in=line_ids).exists()
        or ProductionActivity.objects.using(database).filter(
            production_order_id__in=production_order_ids,
            entry_kind="ACTIVITY",
            activity_type="WAREHOUSE_DELIVERY",
        ).exists()
        or lines.exclude(qc_passed_before_cutover_qty=0).exists()
    ):
        raise RuntimeError("Reset QC gagal membersihkan seluruh target.")


def reset(apps, schema_editor):
    database = schema_editor.connection.alias
    if schema_editor.connection.vendor != "postgresql":
        return
    AuditEvent = apps.get_model("audit", "AuditEvent")
    try:
        with transaction.atomic(using=database):
            _reset(apps, schema_editor)
    except Exception as exc:
        AuditEvent.objects.using(database).create(
            actor=None,
            action="active_po_wip_reset_before_qc_blocked",
            entity_type="production.productionorder",
            entity_id=AUDIT_KEY,
            reason=str(exc),
            metadata={"exception_type": type(exc).__name__},
        )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("audit", "0001_initial"),
        ("inventory", "0016_pre_cutover_sellable_returns"),
        ("production", "0009_rejected_goods_delivery_activity"),
        ("purchasing", "0010_audit_operation_uat_links"),
    ]

    operations = [migrations.RunPython(reset, migrations.RunPython.noop)]
