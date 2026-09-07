from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rnd", "0009_designasset")]

    operations = [
        migrations.AlterField(
            model_name="developmentproductdocumentrevision",
            name="revision_target",
            field=models.CharField(
                blank=True,
                choices=[
                    ("MOCKUP", "Mockup"),
                    ("TECHNICAL_DRAWING", "Technical Drawing"),
                    ("BOTH", "Mockup dan Technical Drawing"),
                ],
                max_length=30,
            ),
        ),
    ]
