import uuid

from django.conf import settings
from django.db import models


class TrafficPeriodState(models.Model):
    class Source(models.TextChoices):
        SHOPEE = "Shopee", "Shopee"
        TIKTOK = "Tiktok", "TikTok"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=20, choices=Source.choices)
    month = models.DateField()
    is_complete = models.BooleanField(default=False)
    last_successful_import_at = models.DateTimeField(null=True, blank=True)
    last_data_end = models.DateField(null=True, blank=True)
    reopen_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("month", "source")
        constraints = [models.UniqueConstraint(fields=["source", "month"], name="traffic_unique_source_month_state")]


class TrafficImportBatch(models.Model):
    class Status(models.TextChoices):
        PARSING = "PARSING", "Parsing"
        READY = "READY", "Ready for approval"
        BLOCKED = "BLOCKED", "Blocked"
        COMMITTED = "COMMITTED", "Committed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_file = models.ForeignKey("imports.RawFile", on_delete=models.PROTECT, related_name="traffic_batches")
    source = models.CharField(max_length=20, choices=TrafficPeriodState.Source.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    parser_version = models.CharField(max_length=30, default="traffic-v1")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PARSING)
    total_rows = models.PositiveIntegerField(default=0)
    ready_rows = models.PositiveIntegerField(default=0)
    blocking_issue_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    quality_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)


class StagedTrafficRow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(TrafficImportBatch, on_delete=models.CASCADE, related_name="staged_rows")
    row_number = models.PositiveIntegerField()
    marketplace_product_code = models.CharField(max_length=160)
    product_name = models.CharField(max_length=255, blank=True)
    product = models.ForeignKey("master_data.Product", null=True, blank=True, on_delete=models.PROTECT)
    views = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    visitors = models.PositiveBigIntegerField(default=0)
    selected_source_data = models.JSONField(default=dict)

    class Meta:
        ordering = ("row_number",)
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="traffic_unique_batch_row")]


class TrafficImportIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "ERROR", "Error"
        WARNING = "WARNING", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(TrafficImportBatch, on_delete=models.CASCADE, related_name="issues")
    staged_row = models.ForeignKey(StagedTrafficRow, null=True, blank=True, on_delete=models.CASCADE, related_name="issues")
    severity = models.CharField(max_length=10, choices=Severity.choices)
    code = models.CharField(max_length=80)
    field_name = models.CharField(max_length=100, blank=True)
    message = models.TextField()
    is_blocking = models.BooleanField(default=False)

    class Meta:
        ordering = ("-is_blocking", "staged_row__row_number", "code")


class TrafficProductMetric(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=20, choices=TrafficPeriodState.Source.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    product = models.ForeignKey(
        "master_data.Product",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="traffic_metrics",
    )
    traffic_product_key = models.CharField(max_length=200, blank=True, default="")
    marketplace_product_code_snapshot = models.CharField(max_length=160)
    product_name_snapshot = models.CharField(max_length=255, blank=True)
    category_snapshot = models.CharField(max_length=100, blank=True)
    subcategory_snapshot = models.CharField(max_length=100, blank=True)
    is_historical_migration = models.BooleanField(default=False)
    views = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    visitors = models.PositiveBigIntegerField(default=0)
    source_batch = models.ForeignKey(TrafficImportBatch, on_delete=models.PROTECT, related_name="committed_metrics")
    committed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("period_start", "source", "product__name")
        constraints = [
            models.UniqueConstraint(fields=["source", "period_start", "traffic_product_key"], name="traffic_unique_period_product_key")
        ]
