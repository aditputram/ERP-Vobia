from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0008_alter_rawfile_dataset_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="masterimportbatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("PARSING", "Parsing"),
                    ("READY", "Ready for approval"),
                    ("BLOCKED", "Blocked"),
                    ("COMMITTED", "Committed"),
                    ("REJECTED", "Dibatalkan"),
                ],
                default="PARSING",
                max_length=20,
            ),
        ),
    ]
