from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("merchandising", "0006_salesprojection_planning_price_snapshot")]

    operations = [
        migrations.AddField(
            model_name="incomingmonthlyactual",
            name="projected_cogs",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=22),
        ),
        migrations.AddField(
            model_name="incomingmonthlyactual",
            name="projected_ending_qty",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=18),
        ),
        migrations.AddField(
            model_name="incomingmonthlyactual",
            name="projected_ending_cogs",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=22),
        ),
        migrations.AddField(
            model_name="incomingmonthlyactual",
            name="actual_ending_qty",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=18),
        ),
        migrations.AddField(
            model_name="incomingmonthlyactual",
            name="actual_ending_cogs",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=22),
        ),
    ]
