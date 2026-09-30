from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0010_campaign_campaign_type")]

    operations = [
        migrations.AddField(
            model_name="socialdailymetric",
            name="quality_note",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="socialdailymetric",
            name="quality_status",
            field=models.CharField(
                choices=[("COMPLETE", "Complete"), ("PARTIAL", "Partial")],
                default="COMPLETE",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="socialsyncrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("RUNNING", "Running"),
                    ("COMPLETED", "Completed"),
                    ("PARTIAL", "Partial"),
                    ("FAILED", "Failed"),
                ],
                max_length=20,
            ),
        ),
    ]
