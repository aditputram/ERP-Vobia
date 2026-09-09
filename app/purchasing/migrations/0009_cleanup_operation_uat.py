import hashlib
import uuid
from datetime import date
from decimal import Decimal

from django.db import migrations, transaction
from django.db.models import Q, Sum


TARGET_SCENARIO_ID = uuid.UUID("9caf7f15-9b04-45b4-917b-0dc3fc4d380a")
TARGET_SCENARIO_NAME = "Sep - Des 2026 Reguler Knitwear"
TARGET_POS = {
    uuid.UUID("686d0487-e5ed-47a3-a78c-67e758956b04"): "PO-VOB-08/26-001",
    uuid.UUID("edcff331-f75c-4fd7-aa13-5a9fda9422d9"): "PO-VOB-08/26-002",
    uuid.UUID("a6845cce-1e87-466a-b5fd-6da1ba30a24d"): "PO-VOB-08/26-003",
    uuid.UUID("bf466323-8548-4df6-8eeb-ff75702793a6"): "PO-VOB-08/26-004",
}
EXPECTED_LINE_COUNTS = {
    "PO-VOB-08/26-001": 10,
    "PO-VOB-08/26-002": 4,
    "PO-VOB-08/26-003": 6,
    "PO-VOB-08/26-004": 4,
}
ARCHIVE_KEY = "operation-uat-po-vob-08-26-001-004"


def _json_value(value):
    if isinstance(value, (Decimal, uuid.UUID, date)):
        return str(value)
    return value


def _snapshot(queryset):
    model = queryset.model
    rows = []
    for obj in queryset.order_by(model._meta.pk.name):
        rows.append(
            {
                field.attname: _json_value(getattr(obj, field.attname))
                for field in model._meta.concrete_fields
            }
        )
    return rows


def _inventory_fingerprint(InventoryMovement, database):
    fields = (
        "id",
        "movement_key",
        "movement_date",
        "movement_type",
        "direction",
        "sku_id",
        "warehouse_id",
        "quantity",
        "allocated_cost",
        "source_reference",
        "sales_line_id",
        "inbound_receipt_id",
        "return_receipt_id",
    )
    payload = "\n".join(
        "|".join("" if value is None else str(value) for value in row)
        for row in InventoryMovement.objects.using(database).order_by("id").values_list(*fields)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _restore_rows(model, rows, database):
    for row in rows:
        values = {}
        for field in model._meta.concrete_fields:
            value = row.get(field.attname)
            converter = field.target_field if field.is_relation else field
            values[field.attname] = None if value is None else converter.to_python(value)
        obj = model.objects.using(database).create(**values)
        model.objects.using(database).filter(pk=obj.pk).update(**values)


def _delete_activity_tree(ProductionActivity, activity_ids, database):
    remaining = set(activity_ids)
    while remaining:
        referenced = set(
            ProductionActivity.objects.using(database)
            .filter(pk__in=remaining, source_activity_id__in=remaining)
            .values_list("source_activity_id", flat=True)
        )
        leaves = remaining - referenced
        if not leaves:
            raise RuntimeError("Cleanup UAT dibatalkan: relasi koreksi Production Activity bersiklus.")
        ProductionActivity.objects.using(database).filter(pk__in=leaves).delete()
        remaining -= leaves


def _cleanup_operation_uat(apps, schema_editor):
    database = schema_editor.connection.alias
    if schema_editor.connection.vendor != "postgresql":
        return

    ProjectionScenario = apps.get_model("merchandising", "ProjectionScenario")
    ProjectionRule = apps.get_model("merchandising", "ProjectionRule")
    SalesProjection = apps.get_model("merchandising", "SalesProjection")
    IncomingPlan = apps.get_model("merchandising", "IncomingPlan")
    IncomingCarryover = apps.get_model("merchandising", "IncomingCarryover")
    IncomingMonthClose = apps.get_model("merchandising", "IncomingMonthClose")
    PPICRequirement = apps.get_model("purchasing", "PPICRequirement")
    PPICRequirementRevision = apps.get_model("purchasing", "PPICRequirementRevision")
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")
    ProductionOrder = apps.get_model("production", "ProductionOrder")
    ProductionPlan = apps.get_model("production", "ProductionPlan")
    ProductionStage = apps.get_model("production", "ProductionStage")
    ProductionTrial = apps.get_model("production", "ProductionTrial")
    ProductionDeliveryOrder = apps.get_model("production", "ProductionDeliveryOrder")
    ProductionCogsFinalization = apps.get_model("production", "ProductionCogsFinalization")
    ProductionActivity = apps.get_model("production", "ProductionActivity")
    QCInspection = apps.get_model("inventory", "QCInspection")
    QCFollowUp = apps.get_model("inventory", "QCFollowUp")
    QCFollowUpEvent = apps.get_model("inventory", "QCFollowUpEvent")
    InboundReceipt = apps.get_model("inventory", "InboundReceipt")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")
    FIFOAllocation = apps.get_model("inventory", "FIFOAllocation")
    InventoryException = apps.get_model("inventory", "InventoryException")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    pos = list(
        PurchaseOrder.objects.using(database)
        .select_for_update()
        .filter(pk__in=TARGET_POS)
        .order_by("po_number")
    )
    if not pos and not ProjectionScenario.objects.using(database).filter(pk=TARGET_SCENARIO_ID).exists():
        return
    if {po.id: po.po_number for po in pos} != TARGET_POS:
        raise RuntimeError("Cleanup UAT dibatalkan: identitas empat PO target tidak cocok.")
    if any(po.source != "INCOMING_PLAN" or po.status != "RELEASED" for po in pos):
        raise RuntimeError("Cleanup UAT dibatalkan: PO target bukan Released dari Approved Incoming Plan.")

    scenario = ProjectionScenario.objects.using(database).select_for_update().filter(pk=TARGET_SCENARIO_ID).first()
    if not scenario or scenario.name != TARGET_SCENARIO_NAME:
        raise RuntimeError("Cleanup UAT dibatalkan: Scenario target tidak cocok.")
    if ProjectionScenario.objects.using(database).exclude(pk=scenario.pk).filter(superseded_by_id=scenario.pk).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: Scenario target dipakai Scenario lain.")

    po_ids = [po.id for po in pos]
    lines = PurchaseOrderLine.objects.using(database).filter(po_id__in=po_ids)
    line_ids = list(lines.values_list("id", flat=True))
    line_counts = {
        po_number: lines.filter(po__po_number=po_number).count()
        for po_number in TARGET_POS.values()
    }
    if line_counts != EXPECTED_LINE_COUNTS or lines.count() != 24:
        raise RuntimeError("Cleanup UAT dibatalkan: komposisi 24 PO line berubah.")
    if (lines.aggregate(total=Sum("ordered_qty"))["total"] or Decimal("0")) != Decimal("624"):
        raise RuntimeError("Cleanup UAT dibatalkan: total PO Qty bukan 624 pcs.")
    if lines.filter(requirement_id__isnull=True).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: ada PO line tanpa PPIC Requirement.")

    requirements = PPICRequirement.objects.using(database).filter(incoming_plan__scenario_id=scenario.pk)
    requirement_ids = list(requirements.values_list("id", flat=True))
    linked_po_numbers = set(
        PurchaseOrderLine.objects.using(database)
        .filter(requirement_id__in=requirement_ids)
        .values_list("po__po_number", flat=True)
    )
    if linked_po_numbers != set(TARGET_POS.values()):
        raise RuntimeError("Cleanup UAT dibatalkan: Requirement Scenario terhubung ke PO di luar target.")
    if lines.exclude(requirement_id__in=requirement_ids).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: PO target terhubung ke Scenario lain.")

    production_orders = ProductionOrder.objects.using(database).filter(po_id__in=po_ids)
    production_order_ids = list(production_orders.values_list("id", flat=True))
    if len(production_order_ids) != 4:
        raise RuntimeError("Cleanup UAT dibatalkan: Production Order target tidak lengkap.")
    activities = ProductionActivity.objects.using(database).filter(production_order_id__in=production_order_ids)
    activity_ids = list(activities.values_list("id", flat=True))
    if ProductionActivity.objects.using(database).filter(po_line_id__in=line_ids).exclude(
        production_order_id__in=production_order_ids
    ).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: PO line dipakai Production Activity di luar target.")
    if ProductionActivity.objects.using(database).exclude(pk__in=activity_ids).filter(
        source_activity_id__in=activity_ids
    ).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: Production Activity target dirujuk activity lain.")

    delivery_orders = ProductionDeliveryOrder.objects.using(database).filter(
        production_order_id__in=production_order_ids
    )
    delivery_numbers = list(delivery_orders.values_list("number", flat=True))
    inspections = QCInspection.objects.using(database).filter(po_line_id__in=line_ids)
    inspection_ids = list(inspections.values_list("id", flat=True))
    follow_ups = QCFollowUp.objects.using(database).filter(po_line_id__in=line_ids)
    follow_up_ids = list(follow_ups.values_list("id", flat=True))
    receipts = InboundReceipt.objects.using(database).filter(po_line_id__in=line_ids)
    receipt_ids = list(receipts.values_list("id", flat=True))
    if InboundReceipt.objects.using(database).filter(delivery_activity_id__in=activity_ids).exclude(
        pk__in=receipt_ids
    ).exists() or QCFollowUp.objects.using(database).filter(delivery_activity_id__in=activity_ids).exclude(
        pk__in=follow_up_ids
    ).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: delivery target dirujuk transaksi di luar PO target.")
    received_qty = receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0")
    if received_qty != Decimal("427"):
        raise RuntimeError(f"Cleanup UAT dibatalkan: total Inbound berubah menjadi {received_qty} pcs.")

    normal_movements = InventoryMovement.objects.using(database).filter(inbound_receipt_id__in=receipt_ids)
    normal_movement_ids = list(normal_movements.values_list("id", flat=True))
    if set(normal_movements.values_list("inbound_receipt_id", flat=True)) != set(receipt_ids):
        raise RuntimeError("Cleanup UAT dibatalkan: Inbound Receipt tidak memiliki movement lengkap.")
    movement_filter = Q(pk__in=normal_movement_ids)
    if delivery_numbers:
        movement_filter |= Q(movement_type="REJECTED_IN", source_reference__in=delivery_numbers)
    movements = InventoryMovement.objects.using(database).filter(movement_filter)
    movement_ids = list(movements.values_list("id", flat=True))
    layers = FIFOLayer.objects.using(database).filter(
        Q(source_po_line_id__in=line_ids) | Q(opening_movement_id__in=normal_movement_ids)
    ).distinct()
    layer_ids = list(layers.values_list("id", flat=True))
    if set(layers.values_list("opening_movement_id", flat=True)) != set(normal_movement_ids):
        raise RuntimeError("Cleanup UAT dibatalkan: Inbound movement tidak memiliki FIFO layer lengkap.")
    layer_totals = layers.aggregate(original=Sum("original_qty"), remaining=Sum("remaining_qty"))
    if (layer_totals["original"] or 0) != Decimal("427") or (
        layer_totals["remaining"] or 0
    ) != Decimal("427"):
        raise RuntimeError("Cleanup UAT dibatalkan: FIFO layer target tidak lagi utuh 427 pcs.")
    if FIFOAllocation.objects.using(database).filter(
        Q(layer_id__in=layer_ids) | Q(outbound_movement_id__in=movement_ids)
    ).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: stock inbound sudah dialokasikan ke Sales/FIFO.")
    if IncomingMonthClose.objects.using(database).filter(month__gte=date(2026, 9, 1)).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: September sudah di-month-close.")

    exceptions = InventoryException.objects.using(database).filter(
        Q(movement_id__in=movement_ids) | Q(resolution_movement_id__in=movement_ids)
    )
    carryovers = IncomingCarryover.objects.using(database).filter(po_line_id__in=line_ids)
    revision_rows = PPICRequirementRevision.objects.using(database).filter(requirement_id__in=requirement_ids)
    rules = ProjectionRule.objects.using(database).filter(scenario_id=scenario.pk)
    projections = SalesProjection.objects.using(database).filter(scenario_id=scenario.pk)
    plans = IncomingPlan.objects.using(database).filter(scenario_id=scenario.pk)
    if IncomingPlan.objects.using(database).exclude(scenario_id=scenario.pk).filter(
        sales_projection__scenario_id=scenario.pk
    ).exists():
        raise RuntimeError("Cleanup UAT dibatalkan: Sales Projection target dipakai Scenario lain.")
    production_plans = ProductionPlan.objects.using(database).filter(production_order_id__in=production_order_ids)
    stages = ProductionStage.objects.using(database).filter(production_order_id__in=production_order_ids)
    trials = ProductionTrial.objects.using(database).filter(production_order_id__in=production_order_ids)
    finalizations = ProductionCogsFinalization.objects.using(database).filter(
        production_order_id__in=production_order_ids
    )
    follow_up_events = QCFollowUpEvent.objects.using(database).filter(follow_up_id__in=follow_up_ids)

    records = {
        "merchandising.ProjectionScenario": _snapshot(
            ProjectionScenario.objects.using(database).filter(pk=scenario.pk)
        ),
        "merchandising.ProjectionRule": _snapshot(rules),
        "merchandising.SalesProjection": _snapshot(projections),
        "merchandising.IncomingPlan": _snapshot(plans),
        "purchasing.PPICRequirement": _snapshot(requirements),
        "purchasing.PPICRequirementRevision": _snapshot(revision_rows),
        "purchasing.PurchaseOrder": _snapshot(PurchaseOrder.objects.using(database).filter(pk__in=po_ids)),
        "purchasing.PurchaseOrderLine": _snapshot(lines),
        "production.ProductionOrder": _snapshot(production_orders),
        "production.ProductionPlan": _snapshot(production_plans),
        "production.ProductionStage": _snapshot(stages),
        "production.ProductionTrial": _snapshot(trials),
        "production.ProductionDeliveryOrder": _snapshot(delivery_orders),
        "production.ProductionCogsFinalization": _snapshot(finalizations),
        "production.ProductionActivity": _snapshot(activities),
        "inventory.QCInspection": _snapshot(inspections),
        "inventory.QCFollowUp": _snapshot(follow_ups),
        "inventory.QCFollowUpEvent": _snapshot(follow_up_events),
        "inventory.InboundReceipt": _snapshot(receipts),
        "inventory.InventoryMovement": _snapshot(movements),
        "inventory.FIFOLayer": _snapshot(layers),
        "inventory.InventoryException": _snapshot(exceptions),
        "merchandising.IncomingCarryover": _snapshot(carryovers),
    }
    before_fingerprint = _inventory_fingerprint(InventoryMovement, database)
    AuditEvent.objects.using(database).create(
        actor=None,
        action="operation_uat_cleanup_committed",
        entity_type="purchasing.purchaseorder",
        entity_id=ARCHIVE_KEY,
        reason="Cleanup empat PO dan Scenario Operation hasil UAT atas persetujuan Adit.",
        before_values={"records": records},
        after_values={
            "po_count": 0,
            "scenario_count": 0,
            "inventory_qty_removed": "427",
        },
        metadata={
            "target_po_numbers": list(TARGET_POS.values()),
            "target_scenario_id": str(TARGET_SCENARIO_ID),
            "target_scenario_name": TARGET_SCENARIO_NAME,
            "before_inventory_fingerprint": before_fingerprint,
        },
    )

    exceptions.delete()
    layers.delete()
    movements.delete()
    receipts.delete()
    follow_up_events.delete()
    follow_ups.delete()
    inspections.delete()
    carryovers.delete()
    _delete_activity_tree(ProductionActivity, activity_ids, database)
    finalizations.delete()
    delivery_orders.delete()
    trials.delete()
    stages.delete()
    production_plans.delete()
    production_orders.delete()
    lines.delete()
    PurchaseOrder.objects.using(database).filter(pk__in=po_ids).delete()
    revision_rows.delete()
    requirements.delete()
    plans.delete()
    projections.delete()
    rules.delete()
    ProjectionScenario.objects.using(database).filter(pk=scenario.pk).delete()

    if PurchaseOrder.objects.using(database).filter(pk__in=TARGET_POS).exists():
        raise RuntimeError("Cleanup UAT gagal menghapus seluruh PO target.")
    if ProjectionScenario.objects.using(database).filter(pk=TARGET_SCENARIO_ID).exists():
        raise RuntimeError("Cleanup UAT gagal menghapus Scenario target.")
    event = AuditEvent.objects.using(database).filter(
        action="operation_uat_cleanup_committed", entity_id=ARCHIVE_KEY
    ).order_by("-occurred_at").first()
    AuditEvent.objects.using(database).filter(pk=event.pk).update(after_values={
        **event.after_values,
        "after_inventory_fingerprint": _inventory_fingerprint(InventoryMovement, database),
    })


def cleanup_operation_uat(apps, schema_editor):
    database = schema_editor.connection.alias
    if schema_editor.connection.vendor != "postgresql":
        return

    AuditEvent = apps.get_model("audit", "AuditEvent")
    try:
        with transaction.atomic(using=database):
            _cleanup_operation_uat(apps, schema_editor)
    except Exception as exc:
        AuditEvent.objects.using(database).create(
            actor=None,
            action="operation_uat_cleanup_blocked",
            entity_type="purchasing.purchaseorder",
            entity_id=ARCHIVE_KEY,
            reason=str(exc),
            metadata={"exception_type": type(exc).__name__},
        )


def restore_operation_uat(apps, schema_editor):
    database = schema_editor.connection.alias
    if schema_editor.connection.vendor != "postgresql":
        return
    AuditEvent = apps.get_model("audit", "AuditEvent")
    event = AuditEvent.objects.using(database).filter(
        action="operation_uat_cleanup_committed", entity_id=ARCHIVE_KEY
    ).order_by("-occurred_at").first()
    if not event:
        return

    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    ProjectionScenario = apps.get_model("merchandising", "ProjectionScenario")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    if PurchaseOrder.objects.using(database).filter(pk__in=TARGET_POS).exists() or (
        ProjectionScenario.objects.using(database).filter(pk=TARGET_SCENARIO_ID).exists()
    ):
        return
    expected_fingerprint = event.after_values.get("after_inventory_fingerprint")
    if not expected_fingerprint or _inventory_fingerprint(InventoryMovement, database) != expected_fingerprint:
        raise RuntimeError("Rollback cleanup UAT dibatalkan: ledger Inventory sudah berubah.")

    records = event.before_values.get("records", {})
    restore_order = (
        "merchandising.ProjectionScenario",
        "merchandising.ProjectionRule",
        "merchandising.SalesProjection",
        "merchandising.IncomingPlan",
        "purchasing.PPICRequirement",
        "purchasing.PPICRequirementRevision",
        "purchasing.PurchaseOrder",
        "purchasing.PurchaseOrderLine",
        "production.ProductionOrder",
        "production.ProductionPlan",
        "production.ProductionStage",
        "production.ProductionTrial",
        "production.ProductionDeliveryOrder",
        "production.ProductionCogsFinalization",
    )
    for label in restore_order:
        _restore_rows(apps.get_model(*label.split(".")), records.get(label, []), database)

    activity_label = "production.ProductionActivity"
    activity_model = apps.get_model("production", "ProductionActivity")
    pending = list(records.get(activity_label, []))
    while pending:
        ready = [
            row for row in pending
            if row.get("source_activity_id") is None
            or activity_model.objects.using(database).filter(pk=row["source_activity_id"]).exists()
        ]
        if not ready:
            raise RuntimeError("Rollback cleanup UAT gagal memulihkan hierarchy Production Activity.")
        _restore_rows(activity_model, ready, database)
        pending = [row for row in pending if row not in ready]

    for label in (
        "inventory.QCInspection",
        "inventory.QCFollowUp",
        "inventory.QCFollowUpEvent",
        "inventory.InboundReceipt",
        "inventory.InventoryMovement",
        "inventory.FIFOLayer",
        "inventory.InventoryException",
        "merchandising.IncomingCarryover",
    ):
        _restore_rows(apps.get_model(*label.split(".")), records.get(label, []), database)

    AuditEvent.objects.using(database).create(
        actor=None,
        action="operation_uat_cleanup_rolled_back",
        entity_type="purchasing.purchaseorder",
        entity_id=ARCHIVE_KEY,
        reason="Rollback migration cleanup Operation UAT 0009.",
        after_values={"restored_po_count": 4, "restored_inventory_qty": "427"},
    )


assert len(TARGET_POS) == 4
assert set(TARGET_POS.values()) == {f"PO-VOB-08/26-{number:03d}" for number in range(1, 5)}
assert sum(EXPECTED_LINE_COUNTS.values()) == 24


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("purchasing", "0008_restate_legacy_po_wip"),
        ("audit", "0001_initial"),
        ("inventory", "0015_restate_fifo_opening_from_stock_opname"),
        ("merchandising", "0008_alter_projectionscenario_status"),
        ("production", "0009_rejected_goods_delivery_activity"),
    ]

    operations = [migrations.RunPython(cleanup_operation_uat, restore_operation_uat)]
