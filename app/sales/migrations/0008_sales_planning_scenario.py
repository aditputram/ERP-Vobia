import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def move_existing_plans_into_scenarios(apps, schema_editor):
    SalesPlan = apps.get_model("sales", "SalesPlan")
    SalesPlanningScenario = apps.get_model("sales", "SalesPlanningScenario")
    for month in SalesPlan.objects.order_by().values_list("month", flat=True).distinct():
        plans = SalesPlan.objects.filter(month=month).order_by("created_at")
        first = plans.first()
        approved = plans.exclude(status="APPROVED").exists() is False
        scenario = SalesPlanningScenario.objects.create(
            name=f"Migrated Sales Planning {month:%b %Y}",
            start_month=month,
            end_month=month,
            status="APPROVED" if approved else "DRAFT",
            created_by_id=first.created_by_id,
            approved_by_id=first.approved_by_id if approved else None,
            approved_at=first.approved_at if approved else None,
        )
        plans.update(scenario=scenario)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sales", "0007_salesplan"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesPlanningScenario",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=180)),
                ("start_month", models.DateField()),
                ("end_month", models.DateField()),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("APPROVED", "Approved")], default="DRAFT", max_length=20)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_sales_planning_scenarios", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_planning_scenarios", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddField(
            model_name="salesplan",
            name="scenario",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="projections", to="sales.salesplanningscenario"),
        ),
        migrations.RunPython(move_existing_plans_into_scenarios, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="salesplan",
            name="scenario",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="projections", to="sales.salesplanningscenario"),
        ),
        migrations.RemoveConstraint(model_name="salesplan", name="sales_unique_month_source_plan"),
        migrations.RemoveField(model_name="salesplan", name="approved_at"),
        migrations.RemoveField(model_name="salesplan", name="approved_by"),
        migrations.RemoveField(model_name="salesplan", name="created_by"),
        migrations.RemoveField(model_name="salesplan", name="status"),
        migrations.AlterModelOptions(
            name="salesplan",
            options={
                "ordering": ("scenario", "month", "source_label"),
                "permissions": [("approve_sales_plan", "Can approve monthly sales plan")],
            },
        ),
        migrations.AddConstraint(
            model_name="salesplan",
            constraint=models.UniqueConstraint(fields=("scenario", "month", "source_label"), name="sales_unique_scenario_month_source_plan"),
        ),
    ]
