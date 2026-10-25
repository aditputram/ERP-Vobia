from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0018_replay_fifo_after_backdated_movements"),
    ]

    operations = [
        migrations.AlterField(
            model_name="qcfollowup",
            name="status",
            field=models.CharField(
                choices=[
                    ("AWAITING_REWORK", "Menunggu Rework"),
                    ("READY_RE_QC", "Menunggu Re-QC"),
                    ("REJECTED", "Rejected"),
                    ("VENDOR_REJECTED", "Rejected / Not Received"),
                    ("ACCEPTED_EXCEPTION", "Accepted with Exception"),
                    ("RESOLVED", "Lolos Re-QC"),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="qcinspection",
            name="failed_disposition",
            field=models.CharField(
                blank=True,
                choices=[
                    ("REWORK", "Rework"),
                    ("REJECTED", "Rejected"),
                    ("VENDOR_REJECTED", "Rejected / Not Received"),
                    ("ACCEPTED_WITH_EXCEPTION", "Accepted with Exception"),
                ],
                max_length=30,
            ),
        ),
    ]
