from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import Category, Product, ProductStatus, ProductVariant, SKU, Subcategory


class MasterDataIntegrityTests(TestCase):
    def setUp(self):
        self.status = ProductStatus.objects.create(code="REGULAR", name="Regular")
        self.category = Category.objects.create(code="SHIRT", name="Shirt")
        self.subcategory = Subcategory.objects.create(
            category=self.category,
            code="CASUAL",
            name="Casual",
        )
        self.product = Product.objects.create(
            code="PROD-001",
            parent_sku="PARENT-001",
            article="ARTICLE-001",
            name="Vobia Test Product",
            status=self.status,
            category=self.category,
            subcategory=self.subcategory,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            name="Black",
            color="Black",
        )

    def test_sku_is_unique_and_marketplace_sized_code_remains_text(self):
        SKU.objects.create(
            sku="SKU-001",
            product_variant=self.variant,
            size="L",
            current_retail_price=Decimal("299000.00"),
            current_master_cogs=Decimal("99500.0000"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SKU.objects.create(sku="SKU-001", product_variant=self.variant, size="XL")

    def test_missing_cogs_and_retail_price_are_allowed_as_quality_flags(self):
        sku = SKU.objects.create(
            sku="SEASONAL-NEW-001",
            product_variant=self.variant,
            size="M",
            current_retail_price=None,
            current_master_cogs=None,
        )
        self.assertIsNone(sku.current_retail_price)
        self.assertIsNone(sku.current_master_cogs)

    def test_subcategory_must_belong_to_product_category(self):
        other_category = Category.objects.create(code="PANTS", name="Pants")
        product = Product(
            code="PROD-INVALID",
            name="Invalid Category Product",
            status=self.status,
            category=other_category,
            subcategory=self.subcategory,
        )
        with self.assertRaises(ValidationError):
            product.full_clean()

