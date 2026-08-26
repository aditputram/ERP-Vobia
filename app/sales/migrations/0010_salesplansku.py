import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("master_data", "0005_reject_warehouse"),
        ("sales", "0009_sales_plan_by_product"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesPlanSKU",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("gross_sales_target", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("quantity_target", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sku_targets", to="sales.salesplan")),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_plan_targets", to="master_data.sku")),
            ],
            options={"ordering": ("plan", "sku__sku")},
        ),
        migrations.AddConstraint(
            model_name="salesplansku",
            constraint=models.UniqueConstraint(fields=("plan", "sku"), name="sales_unique_plan_sku_target"),
        ),
        migrations.AddConstraint(
            model_name="salesplansku",
            constraint=models.CheckConstraint(condition=models.Q(("gross_sales_target__gte", 0)), name="sales_plan_sku_gross_nonnegative"),
        ),
    ]
