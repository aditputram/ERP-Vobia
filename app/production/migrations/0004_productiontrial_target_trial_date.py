from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0003_merge_material_vendor_into_purchase"),
    ]

    operations = [
        migrations.AddField(
            model_name="productiontrial",
            name="target_trial_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
