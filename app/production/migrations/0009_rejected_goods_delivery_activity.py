from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("production", "0008_productioncogsfinalization")]

    operations = [
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
                    ("MAKE", "Make · Jahit"),
                    ("TRIM", "Trim · Finishing"),
                    ("QC", "Quality Control"),
                    ("WAREHOUSE_DELIVERY", "Deliver to Warehouse"),
                    ("REJECTED_WAREHOUSE_DELIVERY", "Deliver Rejected Goods"),
                ],
                max_length=30,
            ),
        )
    ]
