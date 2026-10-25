import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Sum


def migrate_legacy_process_costs(apps, schema_editor):
    Plan = apps.get_model("purchasing", "PPICProductionPlan")
    ProductionCost = apps.get_model("purchasing", "PPICProductionCost")
    for plan in Plan.objects.filter(production_method="CMT"):
        quantities = (
            plan.lines.values("requirement__sku__product_variant__product_id")
            .annotate(total=Sum("quantity"))
        )
        total_qty = sum((row["total"] for row in quantities), Decimal("0"))
        if not total_qty:
            continue
        cmt_unit_cost = (plan.cut_cost + plan.make_cost + plan.trim_cost) / total_qty
        additional_unit_cost = plan.additional_process_cost / total_qty
        for row in quantities:
            ProductionCost.objects.create(
                plan_id=plan.id,
                product_id=row["requirement__sku__product_variant__product_id"],
                cmt_unit_cost=cmt_unit_cost,
                additional_process_unit_cost=additional_unit_cost,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("master_data", "0005_reject_warehouse"),
        ("purchasing", "0017_ppicproductionplan_additional_process"),
    ]

    operations = [
        migrations.CreateModel(
            name="PPICProductionCost",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("cmt_unit_cost", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                (
                    "additional_process_unit_cost",
                    models.DecimalField(decimal_places=4, default=0, max_digits=18),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="process_costs",
                        to="purchasing.ppicproductionplan",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ppic_production_costs",
                        to="master_data.product",
                    ),
                ),
            ],
            options={"ordering": ("product__name",)},
        ),
        migrations.AddConstraint(
            model_name="ppicproductioncost",
            constraint=models.UniqueConstraint(
                fields=("plan", "product"),
                name="purchasing_unique_plan_product_cost",
            ),
        ),
        migrations.AddConstraint(
            model_name="ppicproductioncost",
            constraint=models.CheckConstraint(
                condition=models.Q(("cmt_unit_cost__gte", 0)),
                name="purchasing_plan_cmt_unit_cost_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="ppicproductioncost",
            constraint=models.CheckConstraint(
                condition=models.Q(("additional_process_unit_cost__gte", 0)),
                name="purchasing_plan_additional_unit_cost_nonnegative",
            ),
        ),
        migrations.RunPython(migrate_legacy_process_costs, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="ppicproductionplan",
            name="purchasing_plan_cut_cost_nonnegative",
        ),
        migrations.RemoveConstraint(
            model_name="ppicproductionplan",
            name="purchasing_plan_additional_cost_nonnegative",
        ),
        migrations.RemoveConstraint(
            model_name="ppicproductionplan",
            name="purchasing_plan_make_cost_nonnegative",
        ),
        migrations.RemoveConstraint(
            model_name="ppicproductionplan",
            name="purchasing_plan_trim_cost_nonnegative",
        ),
        migrations.RemoveField(model_name="ppicproductionplan", name="cut_cost"),
        migrations.RemoveField(model_name="ppicproductionplan", name="additional_process_cost"),
        migrations.RemoveField(model_name="ppicproductionplan", name="make_cost"),
        migrations.RemoveField(model_name="ppicproductionplan", name="trim_cost"),
    ]
