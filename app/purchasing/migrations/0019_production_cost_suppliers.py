import django.db.models.deletion
from django.db import migrations, models


def copy_legacy_plan_supplier(apps, schema_editor):
    Material = apps.get_model("purchasing", "PPICProductionMaterial")
    ProductionCost = apps.get_model("purchasing", "PPICProductionCost")
    for material in Material.objects.select_related("plan"):
        material.supplier_id = material.plan.supplier_id
        material.save(update_fields=["supplier"])
    for cost in ProductionCost.objects.select_related("plan"):
        cost.cmt_supplier_id = cost.plan.supplier_id
        if cost.additional_process_unit_cost:
            cost.additional_process_supplier_id = cost.plan.supplier_id
            cost.additional_process_name = cost.plan.additional_process_name
        cost.save(update_fields=[
            "cmt_supplier",
            "additional_process_supplier",
            "additional_process_name",
        ])


class Migration(migrations.Migration):
    dependencies = [
        ("master_data", "0005_reject_warehouse"),
        ("purchasing", "0018_product_specific_production_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="ppicproductionmaterial",
            name="supplier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ppic_production_materials",
                to="master_data.supplier",
            ),
        ),
        migrations.AddField(
            model_name="ppicproductioncost",
            name="cmt_supplier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ppic_cmt_costs",
                to="master_data.supplier",
            ),
        ),
        migrations.AddField(
            model_name="ppicproductioncost",
            name="additional_process_name",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="ppicproductioncost",
            name="additional_process_supplier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ppic_additional_process_costs",
                to="master_data.supplier",
            ),
        ),
        migrations.RunPython(copy_legacy_plan_supplier, migrations.RunPython.noop),
    ]
