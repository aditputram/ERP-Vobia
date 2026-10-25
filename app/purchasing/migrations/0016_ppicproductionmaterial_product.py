import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("master_data", "0005_reject_warehouse"),
        ("purchasing", "0015_ppicproductionplan_process_controls"),
    ]

    operations = [
        migrations.AddField(
            model_name="ppicproductionmaterial",
            name="product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ppic_production_materials",
                to="master_data.product",
            ),
        ),
    ]
