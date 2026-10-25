from django.db import migrations, models


def set_existing_process_controls(apps, schema_editor):
    ProductionPlan = apps.get_model("purchasing", "PPICProductionPlan")
    ProductionPlan.objects.filter(production_method="FPP").update(
        control_material_purchase=False,
        control_cut=False,
    )
    ProductionPlan.objects.filter(target_trial_date__isnull=False).update(control_trial=True)


class Migration(migrations.Migration):

    dependencies = [
        ("purchasing", "0014_remove_ppicproductionplan_purchasing_plan_fpp_cost_nonnegative_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_material_purchase",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_trial",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_cut",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_make",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_trim",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_qc",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_inbound",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(set_existing_process_controls, migrations.RunPython.noop),
    ]
