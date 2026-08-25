from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0007_remove_qc_replacement_disposition")]

    operations = [
        migrations.AlterField(
            model_name="qcinspection",
            name="failed_disposition",
            field=models.CharField(
                blank=True,
                choices=[
                    ("REWORK", "Rework"),
                    ("REJECTED", "Rejected"),
                    ("ACCEPTED_WITH_EXCEPTION", "Accepted with Exception"),
                ],
                max_length=30,
            ),
        ),
    ]
