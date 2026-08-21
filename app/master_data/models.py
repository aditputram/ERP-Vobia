import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class UUIDTimestampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ProductStatus(UUIDTimestampedModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    planning_guardrails = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "Product statuses"

    def __str__(self):
        return self.name


class Category(UUIDTimestampedModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Subcategory(UUIDTimestampedModel):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="subcategories")
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("category__name", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["category", "code"],
                name="master_unique_subcategory_code_per_category",
            )
        ]

    def __str__(self):
        return f"{self.category.name} / {self.name}"


class Product(UUIDTimestampedModel):
    code = models.CharField(max_length=160, unique=True)
    parent_sku = models.CharField(max_length=100, blank=True, db_index=True)
    article = models.CharField(max_length=120, blank=True, db_index=True)
    name = models.CharField(max_length=255)
    status = models.ForeignKey(ProductStatus, on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    subcategory = models.ForeignKey(
        Subcategory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="products",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.subcategory_id and self.category_id:
            if self.subcategory.category_id != self.category_id:
                raise ValidationError(
                    {"subcategory": "Subcategory harus berada di dalam category yang dipilih."}
                )


class ProductVariant(UUIDTimestampedModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="variants")
    name = models.CharField(max_length=150)
    color = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("product__name", "name", "color")
        constraints = [
            models.UniqueConstraint(
                fields=["product", "name", "color"],
                name="master_unique_variant_per_product",
            )
        ]

    def __str__(self):
        return f"{self.product.name} — {self.name}"


class SKU(UUIDTimestampedModel):
    sku = models.CharField(max_length=100, unique=True)
    product_variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        related_name="skus",
    )
    size = models.CharField(max_length=80, blank=True)
    current_retail_price = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    current_master_cogs = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("sku",)
        constraints = [
            models.CheckConstraint(
                condition=Q(current_retail_price__gte=0) | Q(current_retail_price__isnull=True),
                name="master_sku_retail_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(current_master_cogs__gte=0) | Q(current_master_cogs__isnull=True),
                name="master_sku_cogs_nonnegative",
            ),
        ]

    def __str__(self):
        return self.sku


class MarketplaceProductMapping(UUIDTimestampedModel):
    class Source(models.TextChoices):
        SHOPEE = "Shopee", "Shopee"
        TIKTOK = "Tiktok", "TikTok"

    source = models.CharField(max_length=20, choices=Source.choices)
    marketplace_product_code = models.CharField(max_length=160)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="marketplace_mappings")
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("source", "marketplace_product_code")
        constraints = [
            models.UniqueConstraint(
                fields=["source", "marketplace_product_code", "product", "valid_from"],
                name="master_unique_marketplace_mapping_version",
            )
        ]

    def __str__(self):
        return f"{self.source} · {self.marketplace_product_code}"


class MarketplaceSKUMapping(UUIDTimestampedModel):
    class Source(models.TextChoices):
        SHOPEE = "Shopee", "Shopee"
        TIKTOK = "Tiktok", "TikTok"

    source = models.CharField(max_length=20, choices=Source.choices)
    marketplace_sku_id = models.CharField(max_length=180)
    marketplace_seller_sku = models.CharField(max_length=180, blank=True)
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name="marketplace_sku_mappings")
    product_name_evidence = models.CharField(max_length=255, blank=True)
    variation_evidence = models.CharField(max_length=120, blank=True)
    evidence_reference = models.CharField(max_length=255)
    confirmed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("source", "marketplace_sku_id")
        constraints = [
            models.UniqueConstraint(
                fields=["source", "marketplace_sku_id"],
                name="master_unique_marketplace_sku_mapping",
            )
        ]

    def __str__(self):
        return f"{self.source} · {self.marketplace_sku_id} → {self.sku.sku}"


class Supplier(UUIDTimestampedModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=180)
    contact_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Warehouse(UUIDTimestampedModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=180)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class SKUValueHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name="value_history")
    effective_at = models.DateTimeField(auto_now_add=True)
    retail_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    master_cogs = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    product_status = models.ForeignKey(ProductStatus, on_delete=models.PROTECT)
    source_batch_id = models.UUIDField()
    changed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="sku_value_changes",
    )
    changes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-effective_at",)
        indexes = [models.Index(fields=["sku", "effective_at"])]

    def __str__(self):
        return f"{self.sku.sku} @ {self.effective_at:%Y-%m-%d %H:%M}"
