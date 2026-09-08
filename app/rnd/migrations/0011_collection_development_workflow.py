from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("rnd", "0010_update_revision_target_labels"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="collection",
            name="development_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="collection",
            name="development_started_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="rnd_collections_development_started",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="developmentproduct",
            name="development_stage",
            field=models.CharField(
                choices=[
                    ("NOT_STARTED", "Belum Dimulai"),
                    ("MATERIAL_PURCHASE", "Pembelian Material Development"),
                    ("SAMPLING", "Sampling"),
                    ("PROTOTYPE", "Prototype"),
                    ("RESAMPLING", "Resampling"),
                    ("FINAL", "Final Development"),
                ],
                default="NOT_STARTED",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="developmentproduct",
            name="prototype_number",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="developmentproduct",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        development_stage__in=(
                            "NOT_STARTED",
                            "MATERIAL_PURCHASE",
                            "SAMPLING",
                        ),
                        prototype_number=0,
                    )
                    | models.Q(
                        development_stage__in=("PROTOTYPE", "RESAMPLING", "FINAL"),
                        prototype_number__gte=1,
                    )
                ),
                name="rnd_development_stage_prototype_number_rule",
            ),
        ),
    ]
