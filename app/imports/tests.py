import csv
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from audit.models import AuditEvent
from master_data.models import MarketplaceProductMapping, SKU, SKUValueHistory

from .models import ImportValidationIssue, MasterImportBatch, RawFile
from .services.master_commit import approve_master_import
from .services.master_parser import CANONICAL_HEADERS, _identifier


def csv_upload(rows, name="bank-data.csv", headers=None):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers or CANONICAL_HEADERS)
    writer.writerows(rows)
    return SimpleUploadedFile(name, output.getvalue().encode("utf-8"), content_type="text/csv")


def valid_row(**overrides):
    values = {
        "SOURCE": "Vobia",
        "SKU": "SKU-001",
        "Parrent Sku": "PARENT-001",
        "ARTICLE": "Vobia Shirt Test",
        "CATEGORY": "Shirt",
        "SUB CATAGORY": "Casual Shirt",
        "VARIANT": "Black",
        "SUB VARIANT": "L",
        "STATUS PRODUCT": "Regular",
        "COGS": "100000",
        "Retail Price": "299000",
        "Kode Shopee": "1234567890123",
        "Kode Tiktok": "9876543210987654321",
    }
    values.update(overrides)
    return [values[header] for header in CANONICAL_HEADERS]


class MasterImportWorkflowTests(TestCase):
    def setUp(self):
        self.private_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            PRIVATE_UPLOAD_ROOT=Path(self.private_dir.name),
            MASTER_IMPORT_MAX_BYTES=5 * 1024 * 1024,
        )
        self.settings_override.enable()
        self.user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(self.user)

    def tearDown(self):
        self.settings_override.disable()
        self.private_dir.cleanup()

    def _upload(self, uploaded_file):
        return self.client.post(
            reverse("imports:master_upload"),
            {"file": uploaded_file},
        )

    def test_valid_csv_is_staged_then_committed_atomically(self):
        seasonal = valid_row(
            SKU="SKU-NEW-001",
            **{
                "Parrent Sku": "",
                "ARTICLE": "Vobia Seasonal Test",
                "STATUS PRODUCT": "Seasonal New",
                "COGS": "",
                "Retail Price": "",
                "Kode Shopee": "",
            },
        )
        response = self._upload(csv_upload([valid_row(), seasonal]))
        batch = MasterImportBatch.objects.get()

        self.assertRedirects(response, reverse("imports:master_detail", args=[batch.id]))
        self.assertEqual(batch.status, MasterImportBatch.Status.READY)
        self.assertEqual(batch.total_rows, 2)
        self.assertEqual(batch.new_rows, 2)
        self.assertEqual(batch.blocking_issue_count, 0)
        self.assertGreaterEqual(batch.warning_count, 3)
        self.assertTrue((Path(self.private_dir.name) / batch.raw_file.storage_path).exists())

        commit_response = self.client.post(reverse("imports:master_approve", args=[batch.id]))
        self.assertRedirects(commit_response, reverse("imports:master_detail", args=[batch.id]))
        batch.refresh_from_db()
        self.assertEqual(batch.status, MasterImportBatch.Status.COMMITTED)
        self.assertEqual(SKU.objects.count(), 2)
        self.assertEqual(SKUValueHistory.objects.count(), 2)
        self.assertTrue(
            MarketplaceProductMapping.objects.filter(
                source="Tiktok",
                marketplace_product_code="9876543210987654321",
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(action="master_import_committed", entity_id=str(batch.id)).exists()
        )

    def test_duplicate_sku_blocks_approval(self):
        duplicate = valid_row(**{"Retail Price": "399000"})
        self._upload(csv_upload([valid_row(), duplicate]))
        batch = MasterImportBatch.objects.get()
        self.assertEqual(batch.status, MasterImportBatch.Status.BLOCKED)
        self.assertEqual(batch.quality_summary["duplicate_sku_count"], 1)
        self.assertTrue(
            batch.issues.filter(code="DUPLICATE_SKU_IN_FILE", is_blocking=True).exists()
        )

    def test_missing_canonical_header_blocks_batch_before_staging(self):
        headers = CANONICAL_HEADERS[:-1]
        row = valid_row()[:-1]
        self._upload(csv_upload([row], headers=headers))
        batch = MasterImportBatch.objects.get()
        self.assertEqual(batch.status, MasterImportBatch.Status.BLOCKED)
        self.assertEqual(batch.total_rows, 0)
        self.assertTrue(batch.issues.filter(code="MISSING_CANONICAL_HEADERS").exists())

    def test_numeric_large_tiktok_code_in_xlsx_is_blocked(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "VOBIA"
        sheet.append(CANONICAL_HEADERS)
        row = valid_row()
        row[-1] = 9_876_543_210_987_654_321
        sheet.append(row)
        payload = io.BytesIO()
        workbook.save(payload)
        workbook.close()
        uploaded = SimpleUploadedFile(
            "bank-data.xlsx",
            payload.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        self._upload(uploaded)
        batch = MasterImportBatch.objects.get()
        self.assertEqual(batch.status, MasterImportBatch.Status.BLOCKED)
        self.assertTrue(
            batch.issues.filter(code="NUMERIC_IDENTIFIER_PRECISION_RISK", is_blocking=True).exists()
        )

    def test_safe_integer_valued_float_identifier_is_converted_to_text(self):
        value, issues = _identifier(15_812_205_524.0, "Kode Shopee")

        self.assertEqual(value, "15812205524")
        self.assertEqual(issues[0][1], "NUMERIC_IDENTIFIER_CONVERTED")
        self.assertFalse(issues[0][-1])

    def test_same_parent_can_contain_multiple_articles_and_statuses(self):
        second = valid_row(
            SKU="SKU-002",
            **{
                "Parrent Sku": "PARENT-001",
                "ARTICLE": "Vobia Shirt White",
                "VARIANT": "White",
                "STATUS PRODUCT": "Seasonal New",
            },
        )

        self._upload(csv_upload([valid_row(), second]))
        batch = MasterImportBatch.objects.get()

        self.assertEqual(batch.status, MasterImportBatch.Status.READY)
        self.assertFalse(batch.issues.filter(code="PRODUCT_GROUP_CONFLICT").exists())

    def test_same_parent_and_article_with_conflicting_subcategory_is_blocked(self):
        second = valid_row(
            SKU="SKU-002",
            **{
                "SUB CATAGORY": "Formal Shirt",
                "SUB VARIANT": "XL",
            },
        )

        self._upload(csv_upload([valid_row(), second]))
        batch = MasterImportBatch.objects.get()

        self.assertEqual(batch.status, MasterImportBatch.Status.BLOCKED)
        self.assertTrue(
            batch.issues.filter(
                code="PRODUCT_GROUP_CONFLICT",
                field_name="subcategory",
                is_blocking=True,
            ).exists()
        )

    def test_identical_raw_file_is_rejected_without_second_copy(self):
        rows = [valid_row()]
        self._upload(csv_upload(rows))
        response = self._upload(csv_upload(rows))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "File identik sudah pernah diunggah")
        self.assertEqual(RawFile.objects.count(), 1)
        self.assertEqual(MasterImportBatch.objects.count(), 1)

    def test_database_error_rolls_back_entire_commit(self):
        self._upload(csv_upload([valid_row()]))
        batch = MasterImportBatch.objects.get()
        with patch(
            "imports.services.master_commit._ensure_mapping",
            side_effect=RuntimeError("simulated failure"),
        ):
            with self.assertRaises(RuntimeError):
                approve_master_import(batch.id, self.user)

        self.assertEqual(SKU.objects.count(), 0)
        self.assertEqual(SKUValueHistory.objects.count(), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.status, MasterImportBatch.Status.READY)

    def test_validation_issue_counts_are_exposed_on_detail(self):
        self._upload(csv_upload([valid_row(**{"Parrent Sku": ""})]))
        batch = MasterImportBatch.objects.get()
        response = self.client.get(reverse("imports:master_detail", args=[batch.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MISSING_PARENT_SKU")
        self.assertEqual(
            ImportValidationIssue.objects.filter(batch=batch, severity="WARNING").count(),
            batch.warning_count,
        )

    def test_ready_master_import_can_be_cancelled_without_reason(self):
        self._upload(csv_upload([valid_row()]))
        batch = MasterImportBatch.objects.get()

        response = self.client.post(reverse("imports:master_cancel", args=[batch.id]))

        self.assertRedirects(response, reverse("imports:master_detail", args=[batch.id]))
        batch.refresh_from_db()
        self.assertEqual(batch.status, MasterImportBatch.Status.REJECTED)
        self.assertEqual(SKU.objects.count(), 0)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="master_import_cancelled",
                entity_id=str(batch.id),
                actor=self.user,
            ).exists()
        )
        detail = self.client.get(reverse("imports:master_detail", args=[batch.id]))
        self.assertContains(detail, "Import dibatalkan")
        self.assertNotContains(detail, "Approve &amp; Commit")

    def test_committed_master_import_cannot_be_cancelled(self):
        self._upload(csv_upload([valid_row()]))
        batch = MasterImportBatch.objects.get()
        approve_master_import(batch.id, self.user)

        self.client.post(reverse("imports:master_cancel", args=[batch.id]))

        batch.refresh_from_db()
        self.assertEqual(batch.status, MasterImportBatch.Status.COMMITTED)

    def test_second_batch_detects_update_and_preserves_value_history(self):
        self._upload(csv_upload([valid_row()], name="initial.csv"))
        first_batch = MasterImportBatch.objects.get()
        approve_master_import(first_batch.id, self.user)

        changed = valid_row(**{"Retail Price": "329000"})
        self._upload(csv_upload([changed], name="changed.csv"))
        second_batch = MasterImportBatch.objects.order_by("-created_at").first()
        self.assertEqual(second_batch.status, MasterImportBatch.Status.READY)
        self.assertEqual(second_batch.changed_rows, 1)
        self.assertEqual(second_batch.new_rows, 0)

        approve_master_import(second_batch.id, self.user)
        sku = SKU.objects.get(sku="SKU-001")
        self.assertEqual(str(sku.current_retail_price), "329000.00")
        self.assertEqual(SKUValueHistory.objects.filter(sku=sku).count(), 2)

    def test_rupiah_thousand_separator_is_not_misread_as_decimal(self):
        row = valid_row(**{"COGS": "100.000", "Retail Price": "299.000"})
        self._upload(csv_upload([row]))
        staged = MasterImportBatch.objects.get().staged_rows.get()
        self.assertEqual(str(staged.cogs), "100000.0000")
        self.assertEqual(str(staged.retail_price), "299000.00")

    def test_parser_handles_verified_canonical_volume(self):
        rows = [
            valid_row(
                SKU=f"SKU-{index:04d}",
                **{
                    "Parrent Sku": f"PARENT-{index:04d}",
                    "ARTICLE": f"Vobia Product {index:04d}",
                    "Kode Shopee": f"SHOPEE-{index:04d}",
                    "Kode Tiktok": f"TIKTOK-{index:04d}",
                },
            )
            for index in range(1, 747)
        ]
        self._upload(csv_upload(rows, name="canonical-746.csv"))
        batch = MasterImportBatch.objects.get()
        self.assertEqual(batch.status, MasterImportBatch.Status.READY)
        self.assertEqual(batch.total_rows, 746)
        self.assertEqual(batch.new_rows, 746)
        self.assertEqual(batch.blocking_issue_count, 0)
