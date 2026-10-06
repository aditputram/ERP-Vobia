from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rnd", "0020_rndnotification"),
    ]

    operations = [
        migrations.AddField(
            model_name="rndnotification",
            name="module",
            field=models.CharField(
                choices=[
                    ("sales", "Sales"),
                    ("operation", "Operation"),
                    ("rnd", "R&D"),
                    ("marketing", "Marketing"),
                    ("finance", "Finance"),
                    ("hrga", "HRGA"),
                ],
                db_index=True,
                default="rnd",
                max_length=20,
            ),
        ),
    ]
