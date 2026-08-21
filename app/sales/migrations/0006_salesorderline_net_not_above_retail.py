from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0005_remove_salesorderline_sales_unique_order_sku_line_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="salesorderline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("retail_price_snapshot__isnull", True),
                    ("net_unit_price__lte", models.F("retail_price_snapshot")),
                    _connector="OR",
                ),
                name="sales_line_net_not_above_retail",
            ),
        ),
    ]
