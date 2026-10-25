from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0019_production_cost_suppliers"),
    ]

    operations = [
        migrations.AddField(
            model_name="ppicproductionmaterial",
            name="material_type",
            field=models.CharField(
                choices=[("MAIN", "Main Material"), ("SUPPORTING", "Supporting Material")],
                default="MAIN",
                max_length=20,
            ),
        ),
    ]
