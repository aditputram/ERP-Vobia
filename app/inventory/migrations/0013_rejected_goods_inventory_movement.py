from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0012_rejected_goods_warehouse_receipt"),
        ("master_data", "0005_reject_warehouse"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inventorymovement",
            name="movement_type",
            field=models.CharField(
                choices=[
                    ("OPENING", "Opening"),
                    ("INCOMING", "Incoming"),
                    ("SALES_OUT", "Sales Out"),
                    ("RETURN_IN", "Return In"),
                    ("REJECTED_IN", "Rejected Goods In"),
                    ("ADJUSTMENT_IN", "Adjustment In"),
                    ("ADJUSTMENT_OUT", "Adjustment Out"),
                ],
                max_length=30,
            ),
        )
    ]
