from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rnd", "0017_reset_larik_development")]

    operations = [
        migrations.AddField(
            model_name="developmentproductmaterial",
            name="notes",
            field=models.TextField(blank=True),
        ),
    ]
