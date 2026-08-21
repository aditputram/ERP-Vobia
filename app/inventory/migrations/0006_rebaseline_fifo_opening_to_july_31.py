from datetime import date

from django.db import migrations


OLD_DATE = date(2026, 8, 1)
NEW_DATE = date(2026, 7, 31)


def rebaseline_forward(apps, schema_editor):
    FIFOOpeningSnapshot = apps.get_model("inventory", "FIFOOpeningSnapshot")
    StagedFIFOOpeningRow = apps.get_model("inventory", "StagedFIFOOpeningRow")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")

    FIFOOpeningSnapshot.objects.filter(cutover_date=OLD_DATE).update(cutover_date=NEW_DATE)
    StagedFIFOOpeningRow.objects.filter(cutover_date=OLD_DATE).update(cutover_date=NEW_DATE)

    for movement in InventoryMovement.objects.filter(movement_type="OPENING", movement_date=OLD_DATE):
        movement.movement_date = NEW_DATE
        movement.movement_key = movement.movement_key.replace("OPENING|20260801|", "OPENING|20260731|", 1)
        movement.source_reference = "FIFO Opening EOD 2026-07-31"
        movement.save(update_fields=["movement_date", "movement_key", "source_reference"])

    for layer in FIFOLayer.objects.filter(source_type="OPENING", receipt_date=OLD_DATE):
        layer.receipt_date = NEW_DATE
        layer.layer_key = layer.layer_key.replace("OPENING|20260801|", "OPENING|20260731|", 1)
        layer.save(update_fields=["receipt_date", "layer_key"])


def rebaseline_backward(apps, schema_editor):
    FIFOOpeningSnapshot = apps.get_model("inventory", "FIFOOpeningSnapshot")
    StagedFIFOOpeningRow = apps.get_model("inventory", "StagedFIFOOpeningRow")
    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    FIFOLayer = apps.get_model("inventory", "FIFOLayer")

    FIFOOpeningSnapshot.objects.filter(cutover_date=NEW_DATE).update(cutover_date=OLD_DATE)
    StagedFIFOOpeningRow.objects.filter(cutover_date=NEW_DATE).update(cutover_date=OLD_DATE)

    for movement in InventoryMovement.objects.filter(movement_type="OPENING", movement_date=NEW_DATE):
        movement.movement_date = OLD_DATE
        movement.movement_key = movement.movement_key.replace("OPENING|20260731|", "OPENING|20260801|", 1)
        movement.source_reference = "FIFO Opening 2026-08-01"
        movement.save(update_fields=["movement_date", "movement_key", "source_reference"])

    for layer in FIFOLayer.objects.filter(source_type="OPENING", receipt_date=NEW_DATE):
        layer.receipt_date = OLD_DATE
        layer.layer_key = layer.layer_key.replace("OPENING|20260731|", "OPENING|20260801|", 1)
        layer.save(update_fields=["receipt_date", "layer_key"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0005_inboundreceipt_retail_price_snapshot")]
    operations = [migrations.RunPython(rebaseline_forward, rebaseline_backward)]
