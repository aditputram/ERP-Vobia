import django.db.models.deletion
from django.db import migrations, models


def clear_source_plans(apps, schema_editor):
    # Source-based drafts belong to the replaced design and cannot map safely to Product.
    apps.get_model("sales", "SalesPlan").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("master_data", "0005_reject_warehouse"),
        ("sales", "0008_sales_planning_scenario"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="salesplan",
            name="sales_unique_scenario_month_source_plan",
        ),
        migrations.RemoveConstraint(
            model_name="salesplan",
            name="sales_plan_net_nonnegative",
        ),
        migrations.AddField(
            model_name="salesplan",
            name="product",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_plans",
                to="master_data.product",
            ),
        ),
        migrations.RunPython(clear_source_plans, migrations.RunPython.noop),
        migrations.RemoveField(model_name="salesplan", name="source_label"),
        migrations.RemoveField(model_name="salesplan", name="net_sales_target"),
        migrations.RemoveField(model_name="salesplan", name="order_target"),
        migrations.AlterField(
            model_name="salesplan",
            name="product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_plans",
                to="master_data.product",
            ),
        ),
        migrations.AlterModelOptions(
            name="salesplan",
            options={
                "ordering": ("scenario", "month", "product__name"),
                "permissions": [("approve_sales_plan", "Can approve monthly sales plan")],
            },
        ),
        migrations.AddConstraint(
            model_name="salesplan",
            constraint=models.UniqueConstraint(
                fields=("scenario", "month", "product"),
                name="sales_unique_scenario_month_product_plan",
            ),
        ),
    ]
