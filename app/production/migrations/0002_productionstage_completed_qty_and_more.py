from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations, models


def backfill_cmt_completed_qty(apps, schema_editor):
    ProductionOrder = apps.get_model("production", "ProductionOrder")
    ProductionStage = apps.get_model("production", "ProductionStage")
    PurchaseOrderLine = apps.get_model("purchasing", "PurchaseOrderLine")

    production_po = dict(ProductionOrder.objects.values_list("id", "po_id"))
    ordered_by_po = {
        row["po_id"]: row["total"] or Decimal("0")
        for row in PurchaseOrderLine.objects.values("po_id").annotate(total=models.Sum("ordered_qty"))
    }
    cmt_stages = ("CUT", "MAKE", "TRIM")
    for stage in ProductionStage.objects.filter(stage__in=cmt_stages).iterator():
        ordered_qty = ordered_by_po.get(production_po.get(stage.production_order_id), Decimal("0"))
        if stage.status == "COMPLETE":
            completed_qty = ordered_qty
        elif stage.progress_percent and ordered_qty:
            completed_qty = (ordered_qty * Decimal(stage.progress_percent) / Decimal("100")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            completed_qty = min(completed_qty, ordered_qty)
        else:
            completed_qty = Decimal("0")
        ProductionStage.objects.filter(pk=stage.pk).update(completed_qty=completed_qty)


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="productionstage",
            name="completed_qty",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=18),
        ),
        migrations.RunPython(backfill_cmt_completed_qty, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="productionstage",
            constraint=models.CheckConstraint(
                condition=models.Q(("completed_qty__gte", 0)),
                name="production_stage_completed_qty_nonnegative",
            ),
        ),
    ]
