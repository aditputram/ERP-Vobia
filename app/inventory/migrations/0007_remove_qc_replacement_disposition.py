from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0006_rebaseline_fifo_opening_to_july_31")]

    operations = [
        migrations.AlterField(
            model_name="qcinspection",
            name="failed_disposition",
            field=models.CharField(
                blank=True,
                choices=[
                    ("WAITING_DECISION", "Waiting Decision"),
                    ("REWORK", "Rework"),
                    ("REJECTED", "Rejected"),
                    ("ACCEPTED_WITH_EXCEPTION", "Accepted with Exception"),
                ],
                max_length=30,
            ),
        ),
    ]
