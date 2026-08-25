from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("merchandising", "0003_incomingmonthclose_incomingcarryover_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="projectionrule",
            name="scope_type",
            field=models.CharField(
                choices=[
                    ("ALL_PRODUCTS", "All Products"),
                    ("PRODUCT_STATUS", "Product Status"),
                    ("CATEGORY", "Category"),
                    ("PRODUCT", "Product"),
                ],
                max_length=30,
            ),
        ),
    ]
