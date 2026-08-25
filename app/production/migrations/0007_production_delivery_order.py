import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0006_warehouse_delivery_activity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductionDeliveryOrder",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("number", models.CharField(max_length=30, unique=True)),
                ("issue_month", models.DateField()),
                ("sequence", models.PositiveIntegerField()),
                ("delivery_date", models.DateField()),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("production_order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="delivery_orders", to="production.productionorder")),
            ],
            options={"ordering": ("-delivery_date", "-sequence")},
        ),
        migrations.AddField(
            model_name="productionactivity",
            name="delivery_order",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="activities", to="production.productiondeliveryorder"),
        ),
        migrations.AddConstraint(
            model_name="productiondeliveryorder",
            constraint=models.UniqueConstraint(fields=("issue_month", "sequence"), name="production_unique_delivery_order_month_sequence"),
        ),
    ]
