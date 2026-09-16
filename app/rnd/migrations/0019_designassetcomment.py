import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("rnd", "0018_developmentproductmaterial_notes"),
    ]

    operations = [
        migrations.CreateModel(
            name="DesignAssetComment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        default=uuid.uuid4,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("body", models.TextField(max_length=2000)),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rnd_design_comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "design",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="rnd.designasset",
                    ),
                ),
            ],
            options={"ordering": ("created_at",)},
        ),
    ]
