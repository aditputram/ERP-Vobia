import csv
import io
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from openpyxl import Workbook

from accounts.models import User
from master_data.models import Category, MarketplaceProductMapping, Product, ProductStatus

from .models import TrafficImportBatch, TrafficPeriodState, TrafficProductMetric
from .services.historical import migrate_historical_traffic
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

    def test_tiktok_export_finds_late_header_and_uses_primary_traffic_columns(self):
        second = Product.objects.create(code="P-2", name="Second Product", status=self.product.status, category=self.product.category)
        MarketplaceProductMapping.objects.create(source="Tiktok", marketplace_product_code="TIKTOK-1", product=self.product)
        MarketplaceProductMapping.objects.create(source="Tiktok", marketplace_product_code="TIKTOK-1", product=second)
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Tanggal analisis: 01/08/2026~25/08/2026"])
        sheet.append([])
        sheet.append(["Semua"] * 8)
        sheet.append(["Nama", "ID Produk", "Impresi produk", "Klik produk", "Klik unik", "Impresi produk", "Klik produk", "Klik unik"])
        sheet.append(["Product", "TIKTOK-1", 100, 20, 10, 999, 888, 777])
        source = io.BytesIO()
        workbook.save(source)
        workbook.close()
        malformed = io.BytesIO()
        source.seek(0)
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(malformed, "w") as rewritten:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    content = content.replace(b'<dimension ref="A1:H5"/>', b'<dimension ref="A1"/>')
                rewritten.writestr(item, content)
        payload = malformed.getvalue()
        upload = SimpleUploadedFile("tiktok.xlsx", payload, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        batch = create_traffic_import(upload, "Tiktok", date(2026, 8, 1), date(2026, 8, 25), self.user)

        self.assertEqual(batch.status, TrafficImportBatch.Status.READY)
        row = batch.staged_rows.get()
        self.assertEqual(row.row_number, 5)
        self.assertEqual((row.views, row.clicks, row.visitors), (100, 20, 10))
        self.assertIsNone(row.product)
        self.assertTrue(batch.issues.filter(code="PRODUCT_MAPPING_AMBIGUOUS", severity="WARNING", is_blocking=False).exists())
        retry = create_traffic_import(
            SimpleUploadedFile("tiktok.xlsx", payload, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "Tiktok",
            date(2026, 8, 1),
            date(2026, 8, 25),
            self.user,
        )
        self.assertEqual(retry.id, batch.id)
        self.assertEqual(TrafficImportBatch.objects.filter(period_start=date(2026, 8, 1)).count(), 1)

    def test_shopee_parent_detail_uses_summary_row_and_skips_variations(self):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Kode Produk", "Produk", "Kode Variasi", "Jumlah Produk Dilihat", "Produk Diklik", "Pengunjung Produk (Kunjungan)"])
        writer.writerow(["12345", "Product", "-", 100, 20, 10])
        writer.writerow(["12345", "Product", "SKU-1", "-", "-", "-"])
        upload = SimpleUploadedFile("shopee.csv", output.getvalue().encode(), content_type="text/csv")

        batch = create_traffic_import(upload, "Shopee", date(2026, 8, 1), date(2026, 8, 25), self.user)

        self.assertEqual(batch.status, TrafficImportBatch.Status.READY)
        self.assertEqual((batch.total_rows, batch.ready_rows), (2, 1))
        row = batch.staged_rows.get()
        self.assertEqual((row.views, row.clicks, row.visitors), (100, 20, 10))

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

    def test_wide_tiktok_history_maps_product_names_and_deduplicates_identical_rows(self):
        MarketplaceProductMapping.objects.create(
            source="Tiktok",
            marketplace_product_code="TIKTOK-1",
            product=self.product,
        )
        second = Product.objects.create(
            code="P-2",
            parent_sku="PARENT-2",
            name="Product Without Mapping",
            status=self.product.status,
            category=self.product.category,
        )
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([
            "Product Name", "Jan Visitors", "Feb Visitors", "Jan Clicks",
            "Feb Clicks", "Jan Views", "Feb Views",
        ])
        sheet.append(["Product", 10, 20, 30, 40, 50, 60])
        sheet.append(["Product", 10, 20, 30, 40, 50, 60])
        sheet.append(["Product Without Mapping", 0, 2, 0, 4, 0, 6])
        source = Path(self.private.name) / "traffic-wide.xlsx"
        workbook.save(source)
        workbook.close()

        batches = migrate_historical_traffic(source, "Tiktok", self.user)

        self.assertEqual(len(batches), 2)
        self.assertEqual(TrafficProductMetric.objects.count(), 4)
        january = TrafficProductMetric.objects.get(source="Tiktok", period_start=date(2026, 1, 1), product=self.product)
        self.assertEqual((january.visitors, january.clicks, january.views), (10, 30, 50))
        self.assertEqual(january.traffic_product_key, f"PRODUCT::{self.product.id}")
        self.assertTrue(TrafficProductMetric.objects.filter(product=second, marketplace_product_code_snapshot="").exists())
        self.assertTrue(TrafficPeriodState.objects.get(source="Tiktok", month=date(2026, 2, 1)).is_complete)
