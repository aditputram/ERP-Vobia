from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0009_socialperiodmetric")]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="campaign_type",
            field=models.CharField(
                choices=[("GRAND", "Grand Campaign"), ("MINI", "Mini Campaign")],
                default="GRAND",
                max_length=10,
            ),
        ),
    ]
