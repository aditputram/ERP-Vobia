import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0007_production_delivery_order"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductionCogsFinalization",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("line_snapshot", models.JSONField(default=list)),
                ("total_po_cost", models.DecimalField(decimal_places=4, max_digits=20)),
                ("total_final_cost", models.DecimalField(decimal_places=4, max_digits=20)),
                ("approved_at", models.DateTimeField(auto_now_add=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approved_production_cogs_finalizations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "production_order",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cogs_finalization",
                        to="production.productionorder",
                    ),
                ),
            ],
            options={
                "ordering": ("-approved_at",),
                "permissions": [
                    (
                        "approve_cogs_finalization",
                        "Can approve production quantity and COGS finalization",
                    )
                ],
            },
        ),
    ]
