from decimal import Decimal

from django.db import migrations, models
from django.db.models import Q


def snapshot_existing_projection_prices(apps, schema_editor):
    SalesProjection = apps.get_model("merchandising", "SalesProjection")
    for projection in SalesProjection.objects.select_related("sku").iterator():
        projection.cogs_snapshot = projection.sku.current_master_cogs or Decimal("0")
        projection.retail_price_snapshot = projection.sku.current_retail_price or Decimal("0")
        projection.net_rate_snapshot = Decimal("0.97")
        projection.save(
            update_fields=["cogs_snapshot", "retail_price_snapshot", "net_rate_snapshot"]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("merchandising", "0005_alter_projectionrule_method"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesprojection",
            name="cogs_snapshot",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=22),
        ),
        migrations.AddField(
            model_name="salesprojection",
            name="retail_price_snapshot",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=22),
        ),
        migrations.AddField(
            model_name="salesprojection",
            name="net_rate_snapshot",
            field=models.DecimalField(decimal_places=4, default=Decimal("0.97"), max_digits=6),
        ),
        migrations.RunPython(snapshot_existing_projection_prices, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="salesprojection",
            constraint=models.CheckConstraint(
                condition=Q(cogs_snapshot__gte=0),
                name="merch_projection_cogs_snapshot_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesprojection",
            constraint=models.CheckConstraint(
                condition=Q(retail_price_snapshot__gte=0),
                name="merch_projection_retail_snapshot_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesprojection",
            constraint=models.CheckConstraint(
                condition=Q(net_rate_snapshot__gte=0) & Q(net_rate_snapshot__lte=1),
                name="merch_projection_net_rate_snapshot_range",
            ),
        ),
    ]
