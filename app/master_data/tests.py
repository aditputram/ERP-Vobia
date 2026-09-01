import csv
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .models import (
    Category,
    MarketplaceProductMapping,
    Product,
    ProductStatus,
    ProductVariant,
    SKU,
    Subcategory,
)


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


class MasterDataOverviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="adit", password="test")
        self.client.force_login(self.user)
        status = ProductStatus.objects.create(code="REGULAR", name="Regular")
        category = Category.objects.create(code="SHIRT", name="Shirt")
        product = Product.objects.create(
            code="PARENT-001::ARTICLE::Sembara",
            parent_sku="PARENT-001",
            article="Sembara",
            name="Flannel Shirt - Sembara",
            status=status,
            category=category,
        )
        variant = ProductVariant.objects.create(product=product, name="Black", color="Black")
        self.sku = SKU.objects.create(
            sku="VOBSH01.L",
            product_variant=variant,
            size="L",
            current_retail_price=Decimal("275000.00"),
            current_master_cogs=Decimal("125000.0000"),
        )
        MarketplaceProductMapping.objects.create(
            source=MarketplaceProductMapping.Source.SHOPEE,
            marketplace_product_code="123456789",
            product=product,
        )
        MarketplaceProductMapping.objects.create(
            source=MarketplaceProductMapping.Source.TIKTOK,
            marketplace_product_code="1736877864034403729",
            product=product,
        )

    def test_overview_defaults_to_complete_sku_view_and_searches_child_sku(self):
        response = self.client.get(reverse("master_data:overview"), {"q": "VOBSH01"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["grain"], "sku")
        self.assertEqual(response.context["rows"], [self.sku])
        self.assertContains(response, "Flannel Shirt - Sembara")
        self.assertContains(response, "Rp 125.000")
        self.assertContains(response, "Rp 275.000")
        self.assertContains(response, "123456789")
        self.assertContains(response, "1736877864034403729")

    def test_overview_can_switch_to_parent_sku_summary(self):
        response = self.client.get(
            reverse("master_data:overview"), {"grain": "parent", "q": "VOBSH01"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["grain"], "parent")
        self.assertEqual(response.context["rows"], [self.sku.product_variant.product])
        self.assertContains(response, "1 Parent SKU ditemukan")
        self.assertContains(response, "COGS / SKU")

    def test_export_matches_canonical_import_contract(self):
        response = self.client.get(reverse("master_data:export_bank_data"))

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(
            rows[0],
            [
                "SOURCE", "SKU", "Parrent Sku", "ARTICLE", "CATEGORY",
                "SUB CATAGORY", "VARIANT", "SUB VARIANT", "STATUS PRODUCT",
                "COGS", "Retail Price", "Kode Shopee", "Kode Tiktok",
            ],
        )
        self.assertEqual(
            rows[1],
            [
                "Vobia", "VOBSH01.L", "PARENT-001", "Sembara", "Shirt", "",
                "Black", "L", "Regular", "125000.0000", "275000.00",
                "123456789", "1736877864034403729",
            ],
        )
