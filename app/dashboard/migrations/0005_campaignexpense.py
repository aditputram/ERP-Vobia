import django.core.validators
import django.db.models.deletion
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


def preserve_existing_spent(apps, schema_editor):
    Campaign = apps.get_model("dashboard", "Campaign")
    CampaignExpense = apps.get_model("dashboard", "CampaignExpense")
    for campaign in Campaign.objects.filter(actual_spent__gt=0):
        CampaignExpense.objects.create(
            campaign=campaign, amount=campaign.actual_spent,
            description="Saldo Actual Spent sebelum rincian biaya", created_by=campaign.created_by,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_campaign_actual_timeline"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CampaignExpense",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0.01"))])),
                ("description", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="expenses", to="dashboard.campaign")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.RunPython(preserve_existing_spent, migrations.RunPython.noop),
    ]
