from django.db import migrations, models


def check_duplicate_targets(apps, schema_editor):
    plans = apps.get_model("sales", "SalesPlan").objects.using(schema_editor.connection.alias)
    duplicates = plans.order_by().values("month", "product_id").annotate(
        count=models.Count("id"),
    ).filter(count__gt=1)
    if duplicates.exists():
        raise RuntimeError(
            "Sales Planning memiliki Product-bulan duplikat. "
            "Rekonsiliasi data terlebih dahulu; migration tidak menghapus target apa pun."
        )


class Migration(migrations.Migration):
    dependencies = [("sales", "0010_salesplansku")]

    operations = [
        migrations.RunPython(check_duplicate_targets, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="salesplan",
            name="sales_unique_scenario_month_product_plan",
        ),
        migrations.AddConstraint(
            model_name="salesplan",
            constraint=models.UniqueConstraint(
                fields=("month", "product"),
                name="sales_unique_month_product_plan",
            ),
        ),
    ]
