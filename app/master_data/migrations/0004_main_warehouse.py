from django.db import migrations


def create_main_warehouse(apps, schema_editor):
    Warehouse = apps.get_model("master_data", "Warehouse")
    Warehouse.objects.update_or_create(
        code="MAIN",
        defaults={"name": "Main Warehouse", "is_active": True},
    )


class Migration(migrations.Migration):
    dependencies = [("master_data", "0003_marketplaceskumapping")]

    operations = [migrations.RunPython(create_main_warehouse, migrations.RunPython.noop)]
