from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("rnd", "0012_collection_workflow_statuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="collection",
            name="marketing_previewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="collection",
            name="marketing_previewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="rnd_collections_previewed_to_marketing",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
