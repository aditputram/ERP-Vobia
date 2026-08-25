import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0010_qcfollowup_delivery_status"),
        ("production", "0006_warehouse_delivery_activity"),
    ]

    operations = [
        migrations.AddField(
            model_name="inboundreceipt",
            name="delivery_activity",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="inbound_receipts",
                to="production.productionactivity",
            ),
        ),
    ]
