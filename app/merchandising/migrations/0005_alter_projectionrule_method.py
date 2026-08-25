from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("merchandising", "0004_alter_projectionrule_scope_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectionrule",
            name="method",
            field=models.CharField(
                choices=[
                    ("INCREASE_PERCENT", "Increase by %"),
                    ("DECREASE_PERCENT", "Decrease by %"),
                    ("SAME_AS_LAST_MONTH", "Sama dengan Bulan Lalu"),
                    ("TARGET_STOCK_RATIO", "Target Stock Ratio"),
                    ("SELL_OUT_ENDING_MONTHS", "Ending Stock Habis dalam X Bulan"),
                ],
                max_length=30,
            ),
        ),
    ]
