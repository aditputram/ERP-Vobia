from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from master_data.models import UUIDTimestampedModel


class Collection(UUIDTimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        DEVELOPMENT = "DEVELOPMENT", "Development"
        MARKETING_REVIEW = "MARKETING_REVIEW", "Marketing Review"
        COMMERCIAL_APPROVED = "COMMERCIAL_APPROVED", "Commercial Approved"

    code = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=180)
    objective = models.TextField(blank=True)
    target_launch_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_collections_created",
    )
    handed_over_at = models.DateTimeField(null=True, blank=True)
    handed_over_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_collections_handed_over",
    )
    commercial_approved_at = models.DateTimeField(null=True, blank=True)
    commercial_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_collections_commercially_approved",
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.code} · {self.name}"


class DesignAsset(UUIDTimestampedModel):
    image = models.FileField(upload_to="rnd/designing/")
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_designs_uploaded",
    )
    recommended_at = models.DateTimeField(null=True, blank=True)
    recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_designs_recommended",
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.original_name


class DevelopmentProduct(UUIDTimestampedModel):
    class DocumentStatus(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Menunggu Approval"
        APPROVED = "APPROVED", "Approved"
        REVISION_REQUESTED = "REVISION_REQUESTED", "Revisi Diminta"

    class Status(models.TextChoices):
        CONCEPT = "CONCEPT", "Concept"
        SAMPLING = "SAMPLING", "Sampling"
        REVISION = "REVISION", "Revision"
        COSTING = "COSTING", "Costing"
        FINAL_APPROVED = "FINAL_APPROVED", "Final R&D"

    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="products")
    working_code = models.CharField(max_length=100)
    name = models.CharField(max_length=180)
    category = models.CharField(max_length=100, blank=True)
    product_story = models.TextField(blank=True)
    target_customer = models.TextField(blank=True)
    target_retail_price = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    estimated_cogs = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    final_sample_url = models.URLField(blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.CONCEPT)
    notes = models.TextField(blank=True)
    product_cover = models.FileField(upload_to="rnd/product_covers/", blank=True)
    mockup = models.FileField(upload_to="rnd/mockups/", blank=True)
    technical_drawing = models.FileField(upload_to="rnd/technical_drawings/", blank=True)
    document_status = models.CharField(
        max_length=20,
        choices=DocumentStatus.choices,
        default=DocumentStatus.DRAFT,
    )
    document_revision = models.PositiveSmallIntegerField(default=0)
    submitted_document = models.FileField(upload_to="rnd/submitted_documents/", blank=True)
    approved_document = models.FileField(upload_to="rnd/approved_documents/", blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_products_submitted",
    )
    rnd_approved_at = models.DateTimeField(null=True, blank=True)
    rnd_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_products_approved",
    )

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("collection", "working_code"),
                name="rnd_unique_working_code_per_collection",
            )
        ]

    def __str__(self):
        return self.name


class DevelopmentProductDocumentRevision(UUIDTimestampedModel):
    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Menunggu Approval"
        APPROVED = "APPROVED", "Approved"
        REVISION_REQUESTED = "REVISION_REQUESTED", "Revisi Diminta"

    class RevisionTarget(models.TextChoices):
        MOCKUP = "MOCKUP", "MDR / Mockup"
        TECHNICAL_DRAWING = "TECHNICAL_DRAWING", "Technical Drawing"
        BOTH = "BOTH", "MDR / Mockup dan Technical Drawing"

    product = models.ForeignKey(
        DevelopmentProduct,
        on_delete=models.CASCADE,
        related_name="document_revisions",
    )
    revision = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices)
    submitted_document = models.FileField(upload_to="rnd/document_revisions/submitted/")
    approved_document = models.FileField(upload_to="rnd/document_revisions/approved/", blank=True)
    submitted_at = models.DateTimeField()
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_document_revisions_submitted",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_document_revisions_approved",
    )
    revision_requested_at = models.DateTimeField(null=True, blank=True)
    revision_note = models.TextField(blank=True)
    revision_target = models.CharField(
        max_length=30,
        choices=RevisionTarget.choices,
        blank=True,
    )
    revision_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_document_revisions_requested",
    )

    class Meta:
        ordering = ("revision",)
        constraints = [
            models.UniqueConstraint(
                fields=("product", "revision"),
                name="rnd_unique_document_revision_per_product",
            )
        ]

    def __str__(self):
        return f"{self.product} · Rev {self.revision:03d}"


class DevelopmentProductMaterial(UUIDTimestampedModel):
    product = models.ForeignKey(
        DevelopmentProduct,
        on_delete=models.CASCADE,
        related_name="materials",
    )
    material = models.CharField(max_length=180)
    requirement = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(0.0001)],
    )
    eom = models.CharField(max_length=40)

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"{self.material} · {self.requirement} {self.eom}"


class MarketingRecommendation(UUIDTimestampedModel):
    class Decision(models.TextChoices):
        GO = "GO", "GO"
        REVISE = "REVISE", "Revise"
        HOLD = "HOLD", "Hold"
        DROP = "DROP", "Drop"

    product = models.OneToOneField(
        DevelopmentProduct,
        on_delete=models.PROTECT,
        related_name="marketing_recommendation",
    )
    recommendation = models.CharField(max_length=20, choices=Decision.choices)
    recommended_quantity = models.PositiveIntegerField(null=True, blank=True)
    rationale = models.TextField()
    recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_marketing_recommendations",
    )
    recommended_at = models.DateTimeField()
    official_decision = models.CharField(max_length=20, choices=Decision.choices, blank=True)
    official_quantity = models.PositiveIntegerField(null=True, blank=True)
    official_note = models.TextField(blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rnd_official_decisions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("product__created_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(recommendation="GO", recommended_quantity__gt=0)
                    | (~Q(recommendation="GO") & Q(recommended_quantity__isnull=True))
                ),
                name="rnd_recommendation_go_quantity_rule",
            ),
            models.CheckConstraint(
                condition=(
                    Q(official_decision="", official_quantity__isnull=True)
                    | Q(official_decision="GO", official_quantity__gt=0)
                    | (
                        ~Q(official_decision__in=("", "GO"))
                        & Q(official_quantity__isnull=True)
                    )
                ),
                name="rnd_official_go_quantity_rule",
            ),
        ]

    def __str__(self):
        return f"{self.product} · {self.recommendation}"
