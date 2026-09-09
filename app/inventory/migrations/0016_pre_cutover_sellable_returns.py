from datetime import date

from django.db import migrations, models
from django.utils import timezone


CUTOVER_DATE = date(2026, 7, 31)
REPAIR_REASON = "Retur sellable dari penjualan pra-cutover."
RESOLUTION_REASON = "Return In pra-cutover diposting memakai Frozen Unit COGS 31 Juli."
AUDIT_ACTION = "pre_cutover_sellable_return_repaired"


def post_missing_returns(apps, schema_editor):
    PhysicalReturnReceipt = apps.get_model("inventory", "PhysicalReturnReceipt")
    FIFOOpeningSnapshot = apps.get_model("inventory", "FIFOOpeningSnapshot")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")
    InventoryException = apps.get_model("inventory", "InventoryException")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    receipts = PhysicalReturnReceipt.objects.filter(
        condition="SELLABLE",
        movement__isnull=True,
        sales_line__order__affects_inventory=False,
        sales_line__order__order_date__lte=CUTOVER_DATE,
        sales_line__sku__isnull=False,
    ).select_related("sales_line__order", "sales_line__sku").order_by("created_at", "id")

    for receipt in receipts:
        snapshot = FIFOOpeningSnapshot.objects.filter(sku_id=receipt.sales_line.sku_id).first()
        if snapshot is None:
            raise RuntimeError(f"Frozen Unit COGS tidak ditemukan untuk {receipt.sales_line.sku.sku}.")
        order = receipt.sales_line.order
        source = order.source_label or order.source
        source_reference = f"{source}|{order.order_number}"
        key = f"RETURN|{source_reference}|{receipt.sales_line.sku.sku}|{receipt.id}"
        restored_cost = receipt.quantity * snapshot.frozen_unit_cogs
        movement = InventoryMovement.objects.create(
            movement_key=key,
            movement_date=receipt.received_date,
            movement_type="RETURN_IN",
            direction="IN",
            sku_id=receipt.sales_line.sku_id,
            warehouse_id=receipt.warehouse_id,
            quantity=receipt.quantity,
            allocated_cost=restored_cost,
            source_reference=source_reference,
            return_receipt_id=receipt.id,
            reason=REPAIR_REASON,
            posted_by_id=receipt.recorded_by_id,
        )
        FIFOLayer.objects.create(
            layer_key=key,
            sku_id=receipt.sales_line.sku_id,
            source_type="RETURN",
            source_reference=source_reference,
            receipt_date=receipt.received_date,
            original_qty=receipt.quantity,
            remaining_qty=receipt.quantity,
            unit_cost=snapshot.frozen_unit_cogs,
            opening_movement_id=movement.id,
        )
        exception = InventoryException.objects.filter(
            code="RETURN_SOURCE_MISSING",
            status="OPEN",
            sku_id=receipt.sales_line.sku_id,
            quantity=receipt.quantity,
            message="Sales Out asal belum memiliki FIFO allocation; Return In tidak diposting.",
            created_at__gte=receipt.created_at,
        ).order_by("created_at", "id").first()
        if exception:
            exception.status = "RESOLVED"
            exception.resolved_at = timezone.now()
            exception.resolved_by_id = receipt.recorded_by_id
            exception.resolution_movement_id = movement.id
            exception.resolution_reason = RESOLUTION_REASON
            exception.save(update_fields=[
                "status", "resolved_at", "resolved_by_id", "resolution_movement_id", "resolution_reason"
            ])
        AuditEvent.objects.create(
            actor_id=receipt.recorded_by_id,
            action=AUDIT_ACTION,
            entity_type="inventory.inventorymovement",
            entity_id=str(movement.id),
            reason=RESOLUTION_REASON,
            after_values={
                "receipt_id": str(receipt.id),
                "quantity": str(receipt.quantity),
                "restored_cost": str(restored_cost),
                "unit_cost_source": "fifo_opening_snapshot",
            },
        )


def reverse_missing_returns(apps, schema_editor):
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    InventoryException = apps.get_model("inventory", "InventoryException")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    for event in AuditEvent.objects.filter(action=AUDIT_ACTION).order_by("occurred_at", "id"):
        movement = InventoryMovement.objects.filter(pk=event.entity_id, reason=REPAIR_REASON).first()
        if movement is None:
            continue
        layer = movement.created_fifo_layer
        if layer.allocations.exists() or layer.remaining_qty != layer.original_qty:
            raise RuntimeError(f"Rollback diblokir: FIFO return layer {layer.layer_key} sudah terpakai.")
        InventoryException.objects.filter(
            resolution_movement_id=movement.id,
            resolution_reason=RESOLUTION_REASON,
        ).update(
            status="OPEN",
            resolved_at=None,
            resolved_by_id=None,
            resolution_movement_id=None,
            resolution_reason="",
        )
        layer.delete()
        movement.delete()
        event.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("inventory", "0015_restate_fifo_opening_from_stock_opname"),
        ("sales", "0011_unique_month_product_plan"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fifolayer",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("OPENING", "Opening"),
                    ("PURCHASE_ORDER", "Purchase Order"),
                    ("RETURN", "Pre-cutover Sales Return"),
                    ("ADJUSTMENT", "Approved Adjustment"),
                ],
                max_length=30,
            ),
        ),
        migrations.RunPython(post_missing_returns, reverse_missing_returns),
    ]
