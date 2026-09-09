import importlib

from django.db import migrations


cleanup = importlib.import_module("purchasing.migrations.0009_cleanup_operation_uat")


class Migration(migrations.Migration):
    atomic = True

    dependencies = [("purchasing", "0010_audit_operation_uat_links")]

    operations = [
        migrations.RunPython(
            cleanup._cleanup_operation_uat,
            cleanup.restore_operation_uat,
        )
    ]
