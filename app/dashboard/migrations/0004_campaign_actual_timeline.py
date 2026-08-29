from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0003_kolpartnership_kolproduct")]

    operations = [
        migrations.AddField(model_name="campaign", name="actual_approval_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="actual_sample_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="actual_creative_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="actual_prelaunch_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="actual_launch_date", field=models.DateField(blank=True, null=True)),
    ]
