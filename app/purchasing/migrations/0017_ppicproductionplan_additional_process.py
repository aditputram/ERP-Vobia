from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0016_ppicproductionmaterial_product"),
    ]

    operations = [
        migrations.AddField(
            model_name="ppicproductionplan",
            name="additional_process_cost",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=18),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="additional_process_name",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="control_additional_process",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="target_additional_process_end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ppicproductionplan",
            name="target_additional_process_start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="ppicproductionplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("additional_process_cost__gte", 0)),
                name="purchasing_plan_additional_cost_nonnegative",
            ),
        ),
    ]
