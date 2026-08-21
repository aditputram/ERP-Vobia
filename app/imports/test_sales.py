import csv
import io
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from master_data.models import Category, MarketplaceSKUMapping, Product, ProductStatus, ProductVariant, SKU
from inventory.models import FIFOOpeningImportBatch, InventoryMovement
from audit.models import AuditEvent
from .models import RawFile
from sales.models import SalesOrder, SalesOrderLine

from .models import SalesImportBatch
from .services.sales_commit import approve_sales_import
from .services.sales_override import override_staged_order_as_pure_cancel
from .services.sales_void import void_sales_import
from .services.storage import create_sales_import


SHOPEE_HEADERS = [
    "No. Pesanan",
    "Status Pesanan",
    "Nomor Referensi SKU",
    "Jumlah",
    "Harga Setelah Diskon",
    "Waktu Pesanan Dibuat",
    "Waktu Pengiriman Diatur",
]
TIKTOK_HEADERS = [
    "Order ID",
    "Order Status",
    "Seller SKU",
    "Quantity",
    "SKU Unit Original Price",
    "SKU Seller Discount",
    "Created Time",
    "Shipped Time",
]


def make_csv(headers, rows, filename):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return SimpleUploadedFile(filename, output.getvalue().encode("utf-8"), content_type="text/csv")


class SalesImportWorkflowTests(TestCase):
    def setUp(self):
        self.private_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            PRIVATE_UPLOAD_ROOT=Path(self.private_dir.name),
            SALES_IMPORT_COMMIT_ENABLED=True,
        )
        self.settings_override.enable()
        self.user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="Aman-Sales-Import-2026!",
        )
        status = ProductStatus.objects.create(code="REGULAR", name="Regular")
        category = Category.objects.create(code="SHIRT", name="Shirt")
        product = Product.objects.create(
            code="PARENT-001",
            parent_sku="PARENT-001",
            article="Vobia Test Shirt",
            name="Vobia Test Shirt",
            status=status,
            category=category,
        )
        variant = ProductVariant.objects.create(product=product, name="Black", color="Black")
        self.sku = SKU.objects.create(
            sku="SKU-001",
            product_variant=variant,
            size="L",
            current_retail_price="299000",
            current_master_cogs="99500",
        )
        opening_raw = RawFile.objects.create(
            dataset_type=RawFile.DatasetType.FIFO_OPENING,
            original_filename="test-opening.csv",
            storage_path="tests/test-opening.csv",
            checksum_sha256="a" * 64,
            byte_size=1,
            detected_format="csv",
            uploaded_by=self.user,
        )
        FIFOOpeningImportBatch.objects.create(
            raw_file=opening_raw,
            status=FIFOOpeningImportBatch.Status.COMMITTED,
            total_rows=1,
            ready_rows=1,
        )

    def tearDown(self):
        self.settings_override.disable()
        self.private_dir.cleanup()

    def shopee_row(self, **overrides):
        values = {
            "No. Pesanan": "SHOPEE-ORDER-001",
            "Status Pesanan": "Selesai",
            "Nomor Referensi SKU": "SKU-001",
            "Jumlah": "2",
            "Harga Setelah Diskon": "299.000",
            "Waktu Pesanan Dibuat": "2026-08-18 10:30",
            "Waktu Pengiriman Diatur": "2026-08-18 12:00",
        }
        values.update(overrides)
        return [values[header] for header in SHOPEE_HEADERS]

    def test_shopee_commit_uses_vobia_gpm_rate_definition(self):
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "shopee.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertEqual(batch.status, SalesImportBatch.Status.READY)
        approve_sales_import(batch.id, self.user)

        line = SalesOrderLine.objects.get()
        self.assertEqual(str(line.total_gross_sales), "598000.00")
        self.assertEqual(str(line.total_net_sales), "598000.0000")
        self.assertEqual(str(line.total_cogs), "199000.0000")
        self.assertEqual(str(line.gpm), "399000.0000")
        self.assertEqual(str(line.gpm_rate), "0.66722408")

    def test_sales_commit_is_blocked_until_fifo_opening_is_committed(self):
        FIFOOpeningImportBatch.objects.all().delete()
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "shopee-before-opening.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )

        with self.assertRaisesMessage(ValidationError, "FIFO Opening 1 Agustus"):
            approve_sales_import(batch.id, self.user)

        self.assertEqual(SalesOrder.objects.count(), 0)
        self.assertEqual(SalesOrderLine.objects.count(), 0)

    def test_uncommitted_batch_can_be_voided_and_is_hidden_from_active_history(self):
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "assistant-upload.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        staged_count = batch.staged_rows.count()
        raw_file_id = batch.raw_file_id

        void_sales_import(batch.id, self.user, "Diupload oleh asisten; user akan upload sendiri.")
        batch.refresh_from_db()

        self.assertEqual(batch.status, SalesImportBatch.Status.VOIDED)
        self.assertFalse(batch.can_approve)
        self.assertEqual(batch.staged_rows.count(), staged_count)
        self.assertTrue(RawFile.objects.filter(pk=raw_file_id).exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                action="sales_import_voided",
                entity_id=str(batch.id),
            ).exists()
        )
        with self.assertRaisesMessage(ValidationError, "belum siap"):
            approve_sales_import(batch.id, self.user)

        self.client.force_login(self.user)
        response = self.client.get(reverse("imports:sales_list"))
        self.assertNotContains(response, "assistant-upload.csv")

    def test_committed_batch_cannot_be_voided(self):
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "committed.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        approve_sales_import(batch.id, self.user)

        with self.assertRaisesMessage(ValidationError, "sudah committed"):
            void_sales_import(batch.id, self.user, "Tidak boleh.")

    def test_pre_cutover_row_is_evidence_only_and_never_posts_sales_or_stock(self):
        old_row = self.shopee_row(
            **{
                "Nomor Referensi SKU": "LEGACY-SKU-NOT-IN-MASTER",
                "Waktu Pesanan Dibuat": "2026-07-31 23:59",
                "Waktu Pengiriman Diatur": "2026-07-31 23:59",
            }
        )
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [old_row], "pre-cutover.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )

        self.assertEqual(batch.status, SalesImportBatch.Status.READY)
        self.assertEqual(batch.out_of_scope_rows, 1)
        self.assertEqual(batch.blocking_issue_count, 0)
        self.assertEqual(batch.staged_rows.get().proposed_action, "OUT_OF_SCOPE")

        approve_sales_import(batch.id, self.user)
        self.assertEqual(SalesOrder.objects.count(), 0)
        self.assertEqual(SalesOrderLine.objects.count(), 0)

    def test_pre_cutover_historical_order_updates_status_only_without_rewriting_financials(self):
        historical_order = SalesOrder.objects.create(
            source=SalesOrder.Source.SHOPEE,
            source_label="Shopee",
            order_number="HIST-SHOPEE-001",
            order_datetime=timezone.make_aware(datetime(2026, 7, 15, 12, 0)),
            order_date=datetime(2026, 7, 15).date(),
            current_status="Sedang Dikirim",
            source_status="Sedang Dikirim",
            is_final=False,
            is_pure_cancelled=False,
            import_origin=SalesOrder.ImportOrigin.HISTORICAL,
            affects_inventory=False,
            first_seen_batch_id="11111111-1111-1111-1111-111111111111",
            latest_batch_id="11111111-1111-1111-1111-111111111111",
        )
        historical_line = SalesOrderLine.objects.create(
            order=historical_order,
            sku=self.sku,
            sku_code_snapshot=self.sku.sku,
            product_status_snapshot="Regular",
            category_snapshot="Shirt",
            product_name_snapshot="Vobia Test Shirt",
            variant_name_snapshot="L",
            quantity=2,
            net_unit_price=Decimal("200000"),
            retail_price_snapshot=Decimal("299000"),
            sales_cogs_snapshot=Decimal("99500"),
            total_gross_sales=Decimal("598000"),
            total_net_sales=Decimal("400000"),
            total_cogs=Decimal("199000"),
            gpm=Decimal("201000"),
            is_counted=True,
        )
        original_financials = {
            "quantity": historical_line.quantity,
            "net_unit_price": historical_line.net_unit_price,
            "retail_price_snapshot": historical_line.retail_price_snapshot,
            "total_net_sales": historical_line.total_net_sales,
            "total_cogs": historical_line.total_cogs,
        }
        raw_row = self.shopee_row(
            **{
                "No. Pesanan": historical_order.order_number,
                "Status Pesanan": "Selesai",
                "Jumlah": "7",
                "Harga Setelah Diskon": "250.000",
                "Waktu Pesanan Dibuat": "2026-07-15 10:30",
                "Waktu Pengiriman Diatur": "2026-07-16 12:00",
            }
        )

        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [raw_row], "historical-status-audit.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )

        self.assertEqual(batch.status, SalesImportBatch.Status.READY)
        self.assertEqual(batch.out_of_scope_rows, 0)
        self.assertEqual(batch.status_update_rows, 1)
        self.assertEqual(batch.quality_summary["historical_status_audit_orders"], 1)
        self.assertEqual(batch.staged_rows.get().proposed_action, "STATUS_UPDATE")

        _, counts = approve_sales_import(batch.id, self.user)
        historical_order.refresh_from_db()
        historical_line.refresh_from_db()
        self.assertEqual(historical_order.current_status, "Selesai")
        self.assertTrue(historical_order.is_final)
        self.assertFalse(historical_order.affects_inventory)
        self.assertEqual(counts["historical_status_updates"], 1)
        self.assertEqual(historical_order.status_history.count(), 1)
        self.assertEqual(InventoryMovement.objects.count(), 0)
        for field, expected in original_financials.items():
            self.assertEqual(getattr(historical_line, field), expected)

    def test_historical_final_status_cannot_regress_to_nonfinal(self):
        historical_order = SalesOrder.objects.create(
            source=SalesOrder.Source.SHOPEE,
            source_label="Shopee",
            order_number="HIST-FINAL-001",
            order_datetime=timezone.make_aware(datetime(2026, 7, 20, 12, 0)),
            order_date=datetime(2026, 7, 20).date(),
            current_status="Selesai",
            source_status="Selesai",
            is_final=True,
            is_pure_cancelled=False,
            import_origin=SalesOrder.ImportOrigin.HISTORICAL,
            affects_inventory=False,
            first_seen_batch_id="22222222-2222-2222-2222-222222222222",
            latest_batch_id="22222222-2222-2222-2222-222222222222",
        )
        SalesOrderLine.objects.create(
            order=historical_order,
            sku=self.sku,
            sku_code_snapshot=self.sku.sku,
            quantity=1,
            net_unit_price=Decimal("200000"),
            retail_price_snapshot=Decimal("299000"),
            total_gross_sales=Decimal("299000"),
            total_net_sales=Decimal("200000"),
            is_counted=True,
        )
        raw_row = self.shopee_row(
            **{
                "No. Pesanan": historical_order.order_number,
                "Status Pesanan": "Sedang Dikirim",
                "Waktu Pesanan Dibuat": "2026-07-20 10:30",
                "Waktu Pengiriman Diatur": "2026-07-20 12:00",
            }
        )

        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [raw_row], "historical-regression.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertTrue(
            batch.issues.filter(code="HISTORICAL_FINAL_STATUS_REGRESSION_IGNORED").exists()
        )
        self.assertEqual(batch.staged_rows.get().proposed_action, "UNCHANGED")

        approve_sales_import(batch.id, self.user)
        historical_order.refresh_from_db()
        self.assertEqual(historical_order.current_status, "Selesai")
        self.assertEqual(historical_order.source_status, "Selesai")
        self.assertTrue(historical_order.is_final)
        self.assertEqual(InventoryMovement.objects.count(), 0)

    def test_tiktok_discount_is_allocated_per_quantity(self):
        row = [
            "TIKTOK-ORDER-001",
            "Selesai",
            "SKU-001",
            "2",
            "299000",
            "100000",
            "18/08/2026 23:12:43",
            "19/08/2026 09:00:00",
        ]
        batch = create_sales_import(
            make_csv(TIKTOK_HEADERS, [row], "tiktok.csv"),
            SalesImportBatch.Source.TIKTOK,
            self.user,
        )
        self.assertEqual(str(batch.staged_rows.get().net_unit_price), "249000.0000")
        approve_sales_import(batch.id, self.user)
        self.assertEqual(str(SalesOrderLine.objects.get().total_net_sales), "498000.0000")

    def test_live_marketplace_cancel_override_preserves_raw_and_prevents_sales_commit(self):
        row = [
            "TIKTOK-LIVE-CANCEL-001",
            "Perlu dikirim",
            "SKU-001",
            "12",
            "299000",
            "0",
            "19/08/2026 20:06:13",
            "",
        ]
        batch = create_sales_import(
            make_csv(TIKTOK_HEADERS, [row], "tiktok-live-cancel.csv"),
            SalesImportBatch.Source.TIKTOK,
            self.user,
        )
        raw_checksum = batch.raw_file.checksum_sha256

        override_staged_order_as_pure_cancel(
            batch_id=batch.id,
            order_number="TIKTOK-LIVE-CANCEL-001",
            actor=self.user,
            reason="Adit memverifikasi status Batal pada marketplace live.",
        )
        batch.refresh_from_db()
        staged = batch.staged_rows.get()

        self.assertEqual(staged.source_status, "Batal")
        self.assertEqual(staged.normalized_status, "Batal")
        self.assertEqual(staged.proposed_action, "PURE_CANCEL")
        self.assertTrue(staged.is_final)
        self.assertTrue(staged.is_pure_cancelled)
        self.assertEqual(staged.selected_source_data["manual_status_override"]["original_source_status"], "Perlu dikirim")
        self.assertEqual(batch.new_rows, 0)
        self.assertEqual(batch.ignored_cancel_rows, 1)
        self.assertEqual(batch.raw_file.checksum_sha256, raw_checksum)
        self.assertTrue(batch.issues.filter(code="MANUAL_STATUS_OVERRIDE").exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                action="sales_import_status_overridden",
                entity_id=str(batch.id),
            ).exists()
        )

        approve_sales_import(batch.id, self.user)
        self.assertEqual(SalesOrder.objects.count(), 0)
        self.assertEqual(SalesOrderLine.objects.count(), 0)

    def test_tiktok_blank_seller_sku_uses_confirmed_marketplace_sku_mapping(self):
        MarketplaceSKUMapping.objects.create(
            source=MarketplaceSKUMapping.Source.TIKTOK,
            marketplace_sku_id="TIKTOK-SKU-ID-001",
            sku=self.sku,
            product_name_evidence="Vobia Test Shirt",
            variation_evidence="L",
            evidence_reference="Adit confirmed test mapping",
            confirmed_by=self.user,
        )
        headers = [*TIKTOK_HEADERS, "SKU ID"]
        row = [
            "TIKTOK-MAPPED-001",
            "Selesai",
            "",
            "1",
            "299000",
            "0",
            "18/08/2026 23:12:43",
            "19/08/2026 09:00:00",
            "TIKTOK-SKU-ID-001",
        ]
        batch = create_sales_import(
            make_csv(headers, [row], "tiktok-mapped.csv"),
            SalesImportBatch.Source.TIKTOK,
            self.user,
        )

        staged = batch.staged_rows.get()
        self.assertEqual(batch.status, SalesImportBatch.Status.READY)
        self.assertEqual(batch.quality_summary["resolved_marketplace_sku_rows"], 1)
        self.assertEqual(staged.sku, self.sku)
        self.assertEqual(staged.sku_text, self.sku.sku)
        self.assertEqual(staged.selected_source_data["source_seller_sku"], "")
        self.assertEqual(staged.selected_source_data["marketplace_sku_id"], "TIKTOK-SKU-ID-001")

    def test_new_pure_cancellation_is_evidence_but_not_canonical_sales(self):
        row = self.shopee_row(
            **{"Status Pesanan": "Batal", "Waktu Pengiriman Diatur": ""}
        )
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [row], "cancel.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertEqual(batch.status, SalesImportBatch.Status.READY)
        self.assertEqual(batch.ignored_cancel_rows, 1)
        approve_sales_import(batch.id, self.user)
        self.assertEqual(SalesOrder.objects.count(), 0)
        self.assertEqual(SalesOrderLine.objects.count(), 0)

    def test_cancelled_after_shipment_is_normalized_to_return(self):
        row = self.shopee_row(**{"Status Pesanan": "Batal"})
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [row], "return.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        staged = batch.staged_rows.get()
        self.assertEqual(staged.normalized_status, "Retur")
        self.assertTrue(staged.is_final)
        self.assertFalse(staged.is_pure_cancelled)

    def test_second_import_updates_status_without_rewriting_snapshot(self):
        first_row = self.shopee_row(**{"Status Pesanan": "Sedang Dikirim"})
        first = create_sales_import(
            make_csv(SHOPEE_HEADERS, [first_row], "first.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        approve_sales_import(first.id, self.user)
        original_line = SalesOrderLine.objects.get()
        original_gpm = original_line.gpm

        second = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "second.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertEqual(second.status_update_rows, 1)
        approve_sales_import(second.id, self.user)
        original_line.refresh_from_db()
        self.assertEqual(original_line.gpm, original_gpm)
        self.assertEqual(original_line.order.current_status, "Selesai")
        self.assertEqual(original_line.order.status_history.count(), 2)

    def test_financial_change_on_committed_key_is_blocked(self):
        first = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "first.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        approve_sales_import(first.id, self.user)
        changed = self.shopee_row(**{"Harga Setelah Diskon": "250.000"})
        second = create_sales_import(
            make_csv(SHOPEE_HEADERS, [changed], "changed.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertEqual(second.status, SalesImportBatch.Status.BLOCKED)
        self.assertTrue(second.issues.filter(code="IMMUTABLE_SNAPSHOT_CONFLICT").exists())

    def test_net_above_master_retail_adjusts_only_transaction_snapshot_and_notifies(self):
        row = self.shopee_row(**{"Harga Setelah Diskon": "350.000", "Jumlah": "1"})
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [row], "special-retail.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        staged = batch.staged_rows.get()
        self.assertEqual(batch.status, SalesImportBatch.Status.READY)
        self.assertEqual(batch.warning_count, 1)
        self.assertEqual(batch.quality_summary["retail_price_special_case_rows"], 1)
        self.assertEqual(str(staged.retail_price_snapshot), "350000.00")
        self.assertEqual(staged.selected_source_data["master_retail_price"], "299000.00")
        self.assertTrue(
            batch.issues.filter(
                code="RETAIL_PRICE_SNAPSHOT_SPECIAL_CASE",
                severity="WARNING",
                is_blocking=False,
            ).exists()
        )
        self.client.force_login(self.user)
        detail = self.client.get(reverse("imports:sales_detail", args=[batch.id]))
        self.assertContains(detail, "NOTIFIKASI SPECIAL CASE HARGA")
        self.assertContains(detail, "Retail Price master tetap")

        approve_sales_import(batch.id, self.user)
        line = SalesOrderLine.objects.get()
        self.sku.refresh_from_db()
        self.assertEqual(str(self.sku.current_retail_price), "299000.00")
        self.assertEqual(str(line.retail_price_snapshot), "350000.00")
        self.assertEqual(line.total_gross_sales, line.total_net_sales)

    def test_master_value_change_does_not_rewrite_or_block_status_only_update(self):
        first = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row(**{"Status Pesanan": "Sedang Dikirim"})], "before-master-change.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        approve_sales_import(first.id, self.user)
        original = SalesOrderLine.objects.get()
        self.sku.current_retail_price = "325000"
        self.sku.current_master_cogs = "120000"
        self.sku.save()
        second = create_sales_import(
            make_csv(SHOPEE_HEADERS, [self.shopee_row()], "after-master-change.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertEqual(second.status, SalesImportBatch.Status.READY)
        approve_sales_import(second.id, self.user)
        original.refresh_from_db()
        self.assertEqual(str(original.retail_price_snapshot), "299000.00")
        self.assertEqual(str(original.sales_cogs_snapshot), "99500.0000")

    def test_duplicate_business_key_blocks_batch(self):
        row = self.shopee_row()
        batch = create_sales_import(
            make_csv(SHOPEE_HEADERS, [row, row], "duplicate.csv"),
            SalesImportBatch.Source.SHOPEE,
            self.user,
        )
        self.assertEqual(batch.status, SalesImportBatch.Status.BLOCKED)
        self.assertEqual(batch.quality_summary["duplicate_business_key_count"], 1)

    def test_tiktok_xlsx_description_row_is_skipped(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "OrderSKUList"
        sheet.append(TIKTOK_HEADERS)
        sheet.append(["Platform unique order ID.", "Current order status."])
        sheet.append(
            [
                "TIKTOK-ORDER-XLSX",
                "Selesai",
                "SKU-001",
                "1",
                "299000",
                "0",
                "18/08/2026 23:12:43",
                "",
            ]
        )
        payload = io.BytesIO()
        workbook.save(payload)
        workbook.close()
        uploaded = SimpleUploadedFile(
            "tiktok.xlsx",
            payload.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        batch = create_sales_import(uploaded, SalesImportBatch.Source.TIKTOK, self.user)
        self.assertEqual(batch.total_rows, 1)
        self.assertEqual(batch.new_rows, 1)

    def test_upload_view_requires_matching_master_and_shows_preview(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("imports:sales_upload"),
            {
                "source": SalesImportBatch.Source.SHOPEE,
                "file": make_csv(SHOPEE_HEADERS, [self.shopee_row()], "view.csv"),
            },
        )
        batch = SalesImportBatch.objects.get()
        self.assertRedirects(response, reverse("imports:sales_detail", args=[batch.id]))
        detail = self.client.get(reverse("imports:sales_detail", args=[batch.id]))
        self.assertContains(detail, "SHOPEE-ORDER-001")
