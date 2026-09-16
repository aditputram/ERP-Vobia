from django.db import migrations


MODULES = ("sales", "operation", "rnd", "marketing", "finance")


def seed_module_rooms(apps, schema_editor):
    ChatThread = apps.get_model("chat", "ChatThread")
    for module in MODULES:
        ChatThread.objects.get_or_create(
            key=f"module:{module}",
            defaults={"kind": "MODULE", "module": module},
        )


class Migration(migrations.Migration):
    dependencies = [("chat", "0001_initial")]

    operations = [migrations.RunPython(seed_module_rooms, migrations.RunPython.noop)]
