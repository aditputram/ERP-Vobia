from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("purchasing", "0002_purchaseordernumbersequence")]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="required_arrival",
            field=models.DateField(blank=True, null=True),
        ),
    ]
