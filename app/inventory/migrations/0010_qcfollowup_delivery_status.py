from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0009_qc_follow_up"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="qcfollowup",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("NOT_SHIPPED", "Belum Dikirim"),
                    ("IN_TRANSIT", "Sedang Dikirim"),
                    ("INBOUND", "Inbound"),
                ],
                default="NOT_SHIPPED",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="qcfollowup",
            name="delivery_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qcfollowup",
            name="delivery_updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="updated_rejected_deliveries",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
