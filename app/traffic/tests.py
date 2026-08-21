import csv
import io
import tempfile
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import User
from master_data.models import Category, MarketplaceProductMapping, Product, ProductStatus

from .models import TrafficImportBatch, TrafficPeriodState, TrafficProductMetric
from .services.ingestion import commit_batch, create_traffic_import, mark_period_complete, reopen_period


def traffic_csv(rows, name="traffic.csv"):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Kode Produk", "Produk", "Jumlah produk dilihat", "Produk diklik", "Pengunjung Produk"])
    writer.writerows(rows)
    return SimpleUploadedFile(name, output.getvalue().encode(), content_type="text/csv")


class TrafficWorkflowTests(TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.override = override_settings(PRIVATE_UPLOAD_ROOT=Path(self.private.name))
        self.override.enable()
        self.user = User.objects.create_user(username="traffic", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="APPAREL", name="Apparel")
        self.product = Product.objects.create(code="P-1", name="Product", status=status, category=category)
        MarketplaceProductMapping.objects.create(source="Shopee", marketplace_product_code="12345", product=self.product)

    def tearDown(self):
        self.override.disable()
        self.private.cleanup()

    def test_commit_and_mtd_reimport_update_same_month_product(self):
        first = create_traffic_import(traffic_csv([["12345", "Product", 100, 20, 10]], "first.csv"), "Shopee", date(2026, 7, 1), date(2026, 7, 15), self.user)
        self.assertEqual(first.status, TrafficImportBatch.Status.READY)
        commit_batch(first.id, self.user)
        second = create_traffic_import(traffic_csv([["12345", "Product", 180, 30, 15]], "second.csv"), "Shopee", date(2026, 7, 1), date(2026, 7, 31), self.user)
        commit_batch(second.id, self.user)
        self.assertEqual(TrafficProductMetric.objects.count(), 1)
        metric = TrafficProductMetric.objects.get()
        self.assertEqual(metric.views, 180)
        self.assertEqual(metric.period_end, date(2026, 7, 31))

    def test_duplicate_product_code_blocks_variation_double_count(self):
        batch = create_traffic_import(traffic_csv([["12345", "Main", 100, 20, 10], ["12345", "Variant", 50, 10, 5]]), "Shopee", date(2026, 7, 1), date(2026, 7, 31), self.user)
        self.assertEqual(batch.status, TrafficImportBatch.Status.BLOCKED)
        self.assertTrue(batch.issues.filter(code="DUPLICATE_PRODUCT_CODE").exists())

    def test_complete_requires_historical_import_and_reopen_reason(self):
        batch = create_traffic_import(traffic_csv([["12345", "Product", 100, 20, 10]]), "Shopee", date(2026, 7, 1), date(2026, 7, 31), self.user)
        commit_batch(batch.id, self.user)
        state = mark_period_complete("Shopee", date(2026, 7, 1), self.user)
        self.assertTrue(state.is_complete)
        with self.assertRaises(ValidationError):
            reopen_period("Shopee", date(2026, 7, 1), self.user, "")
        state = reopen_period("Shopee", date(2026, 7, 1), self.user, "source correction")
        self.assertFalse(state.is_complete)
        self.assertEqual(state.reopen_count, 1)
