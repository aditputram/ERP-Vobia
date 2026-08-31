import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class Campaign(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=180)
    description = models.TextField()
    campaign_plan_url = models.URLField(max_length=500, blank=True)
    creative_asset_url = models.URLField(max_length=500, blank=True)
    cover = models.FileField(upload_to="campaign_covers/", blank=True)
    approval_date = models.DateField()
    sample_date = models.DateField()
    creative_date = models.DateField()
    prelaunch_date = models.DateField()
    launch_date = models.DateField()
    actual_approval_date = models.DateField(null=True, blank=True)
    actual_sample_date = models.DateField(null=True, blank=True)
    actual_creative_date = models.DateField(null=True, blank=True)
    actual_prelaunch_date = models.DateField(null=True, blank=True)
    actual_launch_date = models.DateField(null=True, blank=True)
    budget = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(0)])
    actual_spent = models.DecimalField(max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-launch_date", "-created_at")

    def __str__(self):
        return self.name


class CampaignProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="products")
    product = models.ForeignKey("master_data.Product", on_delete=models.PROTECT)
    target_qty = models.PositiveIntegerField()
    retail_price_snapshot = models.DecimalField(max_digits=18, decimal_places=2)
    target_gross_sales = models.DecimalField(max_digits=20, decimal_places=2)

    class Meta:
        ordering = ("product__name",)
        constraints = [models.UniqueConstraint(fields=("campaign", "product"), name="dashboard_campaign_product_unique")]


class CampaignExpense(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="expenses")
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    description = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)


class CampaignCreative(models.Model):
    class Platform(models.TextChoices):
        INSTAGRAM = "INSTAGRAM", "Instagram"
        TIKTOK = "TIKTOK", "TikTok"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="creatives")
    platform = models.CharField(max_length=20, choices=Platform.choices)
    post_url = models.URLField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("platform", "created_at")
        constraints = [models.UniqueConstraint(fields=("campaign", "post_url"), name="dashboard_campaign_creative_unique")]


class KolPartnership(models.Model):
    class Platform(models.TextChoices):
        INSTAGRAM = "INSTAGRAM", "Instagram"
        TIKTOK = "TIKTOK", "TikTok"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="kol_partnerships")
    kol_name = models.CharField(max_length=180)
    platform = models.CharField(max_length=20, choices=Platform.choices)
    budget = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(0)])
    post_url = models.URLField(max_length=500, blank=True)
    views = models.PositiveBigIntegerField(default=0)
    likes = models.PositiveBigIntegerField(default=0)
    comments = models.PositiveBigIntegerField(default=0)
    saves = models.PositiveBigIntegerField(default=0)
    shares = models.PositiveBigIntegerField(default=0)
    metrics_updated_at = models.DateTimeField(null=True, blank=True)
    metrics_error = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def total_engagement(self):
        return self.likes + self.comments + self.saves + self.shares

    @property
    def engagement_rate(self):
        return self.total_engagement / self.views * 100 if self.views else None

    @property
    def cpm(self):
        return self.budget / self.views * 1000 if self.views else None


class KolProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partnership = models.ForeignKey(KolPartnership, on_delete=models.CASCADE, related_name="products")
    product = models.ForeignKey("master_data.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ("product__name",)
        constraints = [models.UniqueConstraint(fields=("partnership", "product"), name="dashboard_kol_product_unique")]


class SocialDailyMetric(models.Model):
    class Platform(models.TextChoices):
        INSTAGRAM = "INSTAGRAM", "Instagram"
        TIKTOK = "TIKTOK", "TikTok"

    platform = models.CharField(max_length=20, choices=Platform.choices)
    account = models.CharField(max_length=100)
    date = models.DateField()
    reach = models.PositiveBigIntegerField(null=True, blank=True)
    impressions = models.PositiveBigIntegerField(null=True, blank=True)
    total_engagement = models.PositiveBigIntegerField(null=True, blank=True)
    accounts_engaged = models.PositiveBigIntegerField(null=True, blank=True)
    profile_visits = models.PositiveBigIntegerField(null=True, blank=True)
    website_clicks = models.PositiveBigIntegerField(null=True, blank=True)
    likes = models.PositiveBigIntegerField(null=True, blank=True)
    comments = models.PositiveBigIntegerField(null=True, blank=True)
    shares = models.PositiveBigIntegerField(null=True, blank=True)
    new_followers = models.PositiveBigIntegerField(null=True, blank=True)
    lost_followers = models.PositiveBigIntegerField(null=True, blank=True)
    synced_at = models.DateTimeField()

    class Meta:
        ordering = ("platform", "account", "date")
        constraints = [
            models.UniqueConstraint(
                fields=("platform", "account", "date"),
                name="dashboard_social_daily_platform_account_date_unique",
            )
        ]


class SocialSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    idempotency_key = models.CharField(max_length=120, unique=True)
    platform = models.CharField(max_length=20, choices=SocialDailyMetric.Platform.choices)
    account = models.CharField(max_length=100)
    source = models.CharField(max_length=30)
    actor = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    cutoff = models.DateField()
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    snapshot_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-started_at",)
