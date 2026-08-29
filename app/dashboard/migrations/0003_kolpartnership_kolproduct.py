import django.core.validators
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0002_campaign_campaign_plan_url"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="KolPartnership", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("kol_name", models.CharField(max_length=180)),
            ("platform", models.CharField(choices=[("INSTAGRAM", "Instagram"), ("TIKTOK", "TikTok")], max_length=20)),
            ("budget", models.DecimalField(decimal_places=2, max_digits=20, validators=[django.core.validators.MinValueValidator(0)])),
            ("post_url", models.URLField(blank=True, max_length=500)),
            ("views", models.PositiveBigIntegerField(default=0)), ("likes", models.PositiveBigIntegerField(default=0)),
            ("comments", models.PositiveBigIntegerField(default=0)), ("saves", models.PositiveBigIntegerField(default=0)),
            ("shares", models.PositiveBigIntegerField(default=0)), ("metrics_updated_at", models.DateTimeField(blank=True, null=True)),
            ("metrics_error", models.CharField(blank=True, max_length=255)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="kol_partnerships", to="dashboard.campaign")),
            ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ], options={"ordering": ("-created_at",)}),
        migrations.CreateModel(name="KolProduct", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("quantity", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
            ("partnership", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="products", to="dashboard.kolpartnership")),
            ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="master_data.product")),
        ], options={"ordering": ("product__name",)}),
        migrations.AddConstraint(model_name="kolproduct", constraint=models.UniqueConstraint(fields=("partnership", "product"), name="dashboard_kol_product_unique")),
    ]
