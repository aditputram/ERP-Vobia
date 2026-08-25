from django.db import migrations


def create_reject_warehouse(apps, schema_editor):
    Warehouse = apps.get_model("master_data", "Warehouse")
    Warehouse.objects.update_or_create(
        code="REJECT",
        defaults={"name": "Reject Warehouse", "is_active": True},
    )


class Migration(migrations.Migration):
    dependencies = [("master_data", "0004_main_warehouse")]

    operations = [migrations.RunPython(create_reject_warehouse, migrations.RunPython.noop)]
