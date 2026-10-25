from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0010_renumber_delivery_orders_annually"),
    ]

    operations = [
        migrations.AddField(
            model_name="productionplan",
            name="additional_process_name",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="productionplan",
            name="target_additional_process_end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productionplan",
            name="target_additional_process_start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="productionactivity",
            name="activity_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("MATERIAL_PURCHASE", "Pembelian Material"),
                    ("MATERIAL_ARRIVAL", "Material Tiba di Tempat Produksi"),
                    ("TRIAL_SUBMIT", "Submit Trial Production"),
                    ("TRIAL_APPROVE", "Approve Trial Production"),
                    ("TRIAL_REVISION", "Revision Required Trial Production"),
                    ("CUT", "Cut · Potong"),
                    ("ADDITIONAL_PROCESS", "Additional Process"),
                    ("MAKE", "Make · Jahit"),
                    ("TRIM", "Trim · Finishing"),
                    ("QC", "Quality Control"),
                    ("WAREHOUSE_DELIVERY", "Deliver to Warehouse"),
                    ("REJECTED_WAREHOUSE_DELIVERY", "Deliver Rejected Goods"),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="productionstage",
            name="stage",
            field=models.CharField(
                choices=[
                    ("MATERIAL_PURCHASE", "Pembelian Material"),
                    ("CUT", "Cut · Potong"),
                    ("ADDITIONAL_PROCESS", "Additional Process"),
                    ("MAKE", "Make · Jahit"),
                    ("TRIM", "Trim · Finishing"),
                ],
                max_length=30,
            ),
        ),
    ]
