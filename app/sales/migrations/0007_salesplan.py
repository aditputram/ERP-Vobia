import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sales", "0006_salesorderline_net_not_above_retail"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesPlan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("month", models.DateField()),
                ("source_label", models.CharField(max_length=80)),
                ("gross_sales_target", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("net_sales_target", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("quantity_target", models.PositiveIntegerField(default=0)),
                ("order_target", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("APPROVED", "Approved")], default="DRAFT", max_length=20)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_sales_plans", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_plans", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-month", "source_label"),
                "permissions": [("approve_sales_plan", "Can approve monthly sales plan")],
            },
        ),
        migrations.AddConstraint(
            model_name="salesplan",
            constraint=models.UniqueConstraint(fields=("month", "source_label"), name="sales_unique_month_source_plan"),
        ),
        migrations.AddConstraint(
            model_name="salesplan",
            constraint=models.CheckConstraint(condition=models.Q(("gross_sales_target__gte", 0)), name="sales_plan_gross_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="salesplan",
            constraint=models.CheckConstraint(condition=models.Q(("net_sales_target__gte", 0)), name="sales_plan_net_nonnegative"),
        ),
    ]
