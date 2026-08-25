from django.db import migrations, models


def merge_material_vendor_stage(apps, schema_editor):
    ProductionStage = apps.get_model("production", "ProductionStage")

    vendor_stages = ProductionStage.objects.filter(stage="MATERIAL_VENDOR").order_by("production_order_id")
    for vendor in vendor_stages.iterator():
        purchase = ProductionStage.objects.filter(
            production_order_id=vendor.production_order_id,
            stage="MATERIAL_PURCHASE",
        ).first()
        if purchase is None:
            continue

        arrival_date = vendor.actual_end_date or vendor.actual_start_date
        update_fields = []
        if arrival_date and not purchase.material_arrival_date:
            purchase.material_arrival_date = arrival_date
            update_fields.append("material_arrival_date")
        if vendor.status != "NOT_STARTED":
            purchase.status = "IN_PROGRESS" if vendor.status == "COMPLETE" and not arrival_date else vendor.status
            purchase.progress_percent = min(vendor.progress_percent, 99) if not arrival_date else vendor.progress_percent
            purchase.actual_start_date = purchase.actual_start_date or vendor.actual_start_date
            purchase.actual_end_date = vendor.actual_end_date
            purchase.updated_by_id = vendor.updated_by_id or purchase.updated_by_id
            vendor_note = vendor.notes.strip()
            if vendor_note:
                purchase.notes = "\n".join(filter(None, [purchase.notes.strip(), f"Material arrival: {vendor_note}"]))
            update_fields.extend(
                [
                    "status",
                    "progress_percent",
                    "actual_start_date",
                    "actual_end_date",
                    "updated_by_id",
                    "notes",
                ]
            )
        if update_fields:
            purchase.save(update_fields=list(dict.fromkeys(update_fields)))

    vendor_stages.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0002_productionstage_completed_qty_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="productionstage",
            name="material_arrival_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(merge_material_vendor_stage, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="productionstage",
            name="stage",
            field=models.CharField(
                choices=[
                    ("MATERIAL_PURCHASE", "Pembelian Material"),
                    ("CUT", "Cut · Potong"),
                    ("MAKE", "Make · Jahit"),
                    ("TRIM", "Trim · Finishing"),
                ],
                max_length=30,
            ),
        ),
    ]
