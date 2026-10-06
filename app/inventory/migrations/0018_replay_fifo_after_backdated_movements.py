from django.db import migrations


REPLAY_REASON = (
    "Backfill FIFO setelah aktivasi replay movement backdate; hitung ulang alokasi Sales Out, "
    "remaining layer, allocated COGS, dan exception secara kronologis."
)


def replay_fifo(apps, schema_editor):
    """Repair persisted FIFO state once when the production migration runs."""
    if schema_editor.connection.vendor != "postgresql":
        return

    InventoryMovement = apps.get_model("inventory", "InventoryMovement")
    sku_ids = list(
        InventoryMovement.objects.exclude(movement_type="OPENING")
        .order_by()
        .values_list("sku_id", flat=True)
        .distinct()
    )
    if not sku_ids:
        return

    from inventory.services.fifo import rebuild_fifo_for_sku
    from master_data.models import SKU

    for sku in SKU.objects.filter(pk__in=sku_ids).order_by("sku"):
        rebuild_fifo_for_sku(
            sku=sku,
            actor=None,
            reason=REPLAY_REASON,
        )


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("accounts", "0004_user_tab_access"),
        ("chat", "0003_pushsubscription"),
        ("inventory", "0017_reset_active_po_wip_before_qc"),
        ("rnd", "0021_rndnotification_module"),
        ("sales", "0012_salesorderline_current_status_and_more"),
    ]

    operations = [migrations.RunPython(replay_fifo, migrations.RunPython.noop)]
