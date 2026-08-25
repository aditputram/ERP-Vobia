from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0011_inboundreceipt_delivery_activity"),
        ("master_data", "0004_main_warehouse"),
        ("production", "0009_rejected_goods_delivery_activity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="qcfollowup",
            name="delivery_activity",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rejected_follow_ups",
                to="production.productionactivity",
            ),
        ),
        migrations.AddField(
            model_name="qcfollowup",
            name="received_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="received_rejected_goods",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="qcfollowup",
            name="received_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qcfollowup",
            name="received_warehouse",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="received_rejected_goods",
                to="master_data.warehouse",
            ),
        ),
    ]
