import tempfile
from io import BytesIO
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject
from reportlab.pdfgen import canvas

from audit.models import AuditEvent
from audit.services import record_audit
from master_data.models import Product

from .models import (
    Collection,
    DesignAsset,
    DesignAssetComment,
    DevelopmentProduct,
    DevelopmentProductDocumentRevision,
    DevelopmentProductMaterial,
    DevelopmentProductStageAttachment,
    DevelopmentProductStageDate,
    DevelopmentProductStageMaterial,
    MarketingRecommendation,
    RndNotification,
)


class RndWorkflowTests(TestCase):
    def setUp(self):
        self.upload_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.upload_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.upload_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        users = get_user_model().objects
        self.rnd_editor = users.create_user(
            username="rnd.editor",
            password="test-password",
            module_access={"rnd": "edit", "marketing": "none"},
        )
        self.rnd_approver = users.create_user(
            username="rnd.approver",
            password="test-password",
            module_access={"rnd": "approve", "marketing": "none"},
        )
        self.marketing = users.create_user(
            username="marketing.editor",
            password="test-password",
            module_access={"rnd": "none", "marketing": "edit"},
        )
        self.admin = users.create_superuser(username="adit", password="test-password")

    def _collection(self, code="COL-001"):
        return Collection.objects.create(code=code, name="Local Test Collection", created_by=self.rnd_editor)

    def _product(self, collection, code="P-001", status=DevelopmentProduct.Status.CONCEPT):
        return DevelopmentProduct.objects.create(
            collection=collection,
            working_code=code,
            name=f"Product {code}",
            status=status,
            document_status=(
                DevelopmentProduct.DocumentStatus.APPROVED
                if status == DevelopmentProduct.Status.FINAL_APPROVED
                else DevelopmentProduct.DocumentStatus.DRAFT
            ),
            rnd_approved_at=timezone.now() if status == DevelopmentProduct.Status.FINAL_APPROVED else None,
            rnd_approved_by=self.rnd_approver if status == DevelopmentProduct.Status.FINAL_APPROVED else None,
        )

    def _pdf(self, name, label):
        output = BytesIO()
        document = canvas.Canvas(output, pagesize=(841.89, 595.276))
        document.drawString(20, 570, label)
        document.drawString(515, 537, "Submitted Date :")
        document.drawString(515, 519, "Rev :")
        document.drawString(515, 501, "Approval Date :")
        document.drawString(744, 537, "Approved By,")
        document.save()
        return SimpleUploadedFile(name, output.getvalue(), content_type="application/pdf")

    def _pdf_with_array_contents(self, name, label):
        source = self._pdf(name, label)
        reader = PdfReader(BytesIO(source.read()))
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        page = writer.pages[0]
        page[NameObject("/Contents")] = ArrayObject((page.raw_get("/Contents"),))
        output = BytesIO()
        writer.write(output)
        return SimpleUploadedFile(name, output.getvalue(), content_type="application/pdf")

    def _image(self, name="design.png", size=(40, 40)):
        output = BytesIO()
        Image.new("RGB", size, "#536b2e").save(output, format="PNG")
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")

    def _product_payload(self, product, status):
        return {
            "name": product.name,
            "category": "Bag",
            "status": status,
            **self._empty_material_formset(),
        }

    def _empty_material_formset(self):
        return {
            "materials-TOTAL_FORMS": "1",
            "materials-INITIAL_FORMS": "0",
            "materials-MIN_NUM_FORMS": "0",
            "materials-MAX_NUM_FORMS": "1000",
        }

    def test_editor_can_create_collection_and_add_multiple_products(self):
        self.client.force_login(self.rnd_editor)
        response = self.client.post(
            reverse("rnd:collection_create"),
            {"code": "fw-27", "name": "Future Wear", "objective": "New market", "target_launch_date": "2027-01-15"},
        )
        collection = Collection.objects.get(code="FW-27")
        self.assertRedirects(response, reverse("rnd:collection_detail", args=[collection.id]))
        self.assertIsNone(collection.target_launch_date)

        for number in (1, 2):
            response = self.client.post(
                reverse("rnd:collection_detail", args=[collection.id]),
                {
                    "working_code": f"sku-{number}",
                    "name": f"Product {number}",
                    "status": "CONCEPT",
                    **self._empty_material_formset(),
                },
            )
            self.assertRedirects(response, reverse("rnd:collection_detail", args=[collection.id]))
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.DRAFT)
        self.assertEqual(collection.products.count(), 2)
        self.assertEqual(collection.products.values("working_code").distinct().count(), 2)
        self.assertTrue(all(product.working_code.startswith("RND-") for product in collection.products.all()))
        self.assertEqual(Product.objects.count(), 0)

    def test_collection_detail_orders_approved_products_first(self):
        collection = self._collection()
        waiting = self._product(collection, code="P-WAITING")
        approved = self._product(
            collection,
            code="P-APPROVED",
            status=DevelopmentProduct.Status.FINAL_APPROVED,
        )

        self.client.force_login(self.rnd_editor)
        response = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))

        self.assertEqual(list(response.context["products"]), [approved, waiting])

    def test_collection_dashboard_uses_cards_with_four_product_covers(self):
        collection = self._collection()
        products = []
        for number in range(5):
            product = self._product(collection, code=f"P-{number}")
            product.product_cover = self._image(f"cover-{number}.png")
            product.save(update_fields=["product_cover"])
            products.append(product)

        self.client.force_login(self.rnd_editor)
        response = self.client.get(reverse("rnd:dashboard"))

        self.assertContains(response, 'class="rnd-product-card rnd-collection-card"')
        self.assertContains(response, 'class="rnd-collection-cover-item"', count=4)
        self.assertNotContains(response, reverse("rnd:product_file", args=[products[4].id, "product-cover"]))

    def test_designing_upload_and_approve_only_recommendation(self):
        self.client.force_login(self.rnd_editor)
        page = self.client.get(reverse("rnd:designing"))
        self.assertContains(page, 'data-rnd-panel-toggle')
        self.assertContains(page, 'id="upload-design-panel" hidden')

        invalid = self.client.post(
            reverse("rnd:designing"),
            {"image": SimpleUploadedFile("invalid.pdf", b"%PDF-1.4", content_type="application/pdf")},
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'id="upload-design-panel">')

        uploaded = self.client.post(
            reverse("rnd:designing"),
            {"image": self._image("draft-flannel.png", size=(2600, 20))},
        )
        self.assertRedirects(uploaded, reverse("rnd:designing"))
        design = DesignAsset.objects.get()
        self.assertEqual(design.original_name, "draft-flannel.webp")
        self.assertTrue(design.image.name.endswith(".webp"))
        design.image.open("rb")
        stored_image = Image.open(design.image)
        self.assertEqual(stored_image.format, "WEBP")
        self.assertLessEqual(max(stored_image.size), 2400)
        design.image.close()
        page = self.client.get(reverse("rnd:designing"))
        self.assertContains(page, "Desain Terbaru")
        self.assertContains(page, "Direkomendasikan untuk Collection Baru")
        self.assertContains(page, design.original_name)
        detail = self.client.get(reverse("rnd:design_detail", args=[design.id]))
        self.assertContains(detail, "history.back()")
        self.assertContains(detail, "Uploader:")
        self.assertContains(detail, self.rnd_editor.username)
        self.assertContains(detail, "Notes &amp; Komentar (0)")
        self.assertNotContains(detail, "Rekomendasikan untuk Collection Baru</button>")
        commented = self.client.post(
            reverse("rnd:design_detail", args=[design.id]),
            {"body": "Warna sudah cocok, coba kerah dibuat lebih kecil."},
        )
        self.assertRedirects(
            commented,
            f'{reverse("rnd:design_detail", args=[design.id])}#design-comments',
        )

        comment = DesignAssetComment.objects.get(design=design)
        self.assertEqual(comment.author, self.rnd_editor)
        self.assertEqual(comment.body, "Warna sudah cocok, coba kerah dibuat lebih kecil.")
        denied = self.client.post(reverse("rnd:design_recommend", args=[design.id]))
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.rnd_approver)
        second_comment = self.client.post(
            reverse("rnd:design_detail", args=[design.id]),
            {"body": "Setuju, lanjutkan eksplorasi warna kedua."},
        )
        self.assertEqual(second_comment.status_code, 302)
        detail = self.client.get(reverse("rnd:design_detail", args=[design.id]))
        self.assertContains(detail, "Notes &amp; Komentar (2)")
        self.assertContains(detail, "Warna sudah cocok, coba kerah dibuat lebih kecil.")
        self.assertContains(detail, "Setuju, lanjutkan eksplorasi warna kedua.")
        self.assertContains(detail, "data-design-recommendation-form")
        self.assertContains(detail, 'headers: {"X-Requested-With": "XMLHttpRequest"}')
        self.assertContains(detail, "location.reload()")
        recommended = self.client.post(
            reverse("rnd:design_recommend", args=[design.id]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(recommended.status_code, 204)
        design.refresh_from_db()
        self.assertIsNotNone(design.recommended_at)
        self.assertEqual(design.recommended_by, self.rnd_approver)
        self.assertEqual(Collection.objects.count(), 0)
        self.assertEqual(DevelopmentProduct.objects.count(), 0)
        page = self.client.get(reverse("rnd:designing"))
        self.assertContains(page, "Direkomendasikan")

        cancelled = self.client.post(
            reverse("rnd:design_unrecommend", args=[design.id]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(cancelled.status_code, 204)
        design.refresh_from_db()
        self.assertIsNone(design.recommended_at)

        DesignAsset.objects.filter(pk=design.pk).update(created_at=timezone.now() - timedelta(days=8))
        archive = self.client.get(reverse("rnd:designing"))
        self.assertContains(archive, "Design Lainnya")

        file_response = self.client.get(reverse("rnd:design_file", args=[design.id]))
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response["Cache-Control"], "private, no-store")
        card_response = self.client.get(
            f'{reverse("rnd:design_file", args=[design.id])}?size=card&v=1'
        )
        self.assertEqual(card_response["Content-Type"], "image/webp")
        self.assertEqual(
            card_response["Cache-Control"],
            "private, max-age=28800, immutable",
        )
        self.client.logout()
        self.assertEqual(self.client.get(reverse("rnd:design_file", args=[design.id])).status_code, 302)

    def test_rnd_notifications_exclude_actor_and_support_read_actions(self):
        design = DesignAsset.objects.create(
            image=self._image(),
            original_name="design.png",
            uploaded_by=self.rnd_approver,
        )
        with self.captureOnCommitCallbacks(execute=True):
            record_audit(
                actor=self.rnd_approver,
                action="rnd_design_uploaded",
                entity_type="rnd.design_asset",
                entity_id=design.id,
                after_values={"original_name": design.original_name},
            )

        notification = RndNotification.objects.get(recipient=self.rnd_editor)
        self.assertEqual(notification.target_url, reverse("rnd:design_detail", args=[design.id]))
        self.assertFalse(RndNotification.objects.filter(recipient=self.rnd_approver).exists())
        self.assertFalse(RndNotification.objects.filter(recipient=self.marketing).exists())
        self.assertTrue(RndNotification.objects.filter(recipient=self.admin).exists())

        self.client.force_login(self.rnd_editor)
        page = self.client.get(reverse("rnd:designing"))
        self.assertContains(page, 'data-rnd-notification-open')
        self.assertContains(page, 'data-rnd-notification-badge')
        live_status = self.client.get(reverse("dashboard:live_status")).json()
        self.assertEqual(live_status["rnd_unread_count"], 1)
        self.assertEqual(live_status["rnd_notifications"][0]["title"], "Desain baru")

        opened = self.client.get(reverse("rnd:notification_open", args=[notification.id]))
        self.assertRedirects(opened, notification.target_url)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

        RndNotification.objects.create(
            recipient=self.rnd_editor,
            actor=self.rnd_approver,
            source_key="manual:test",
            title="Test",
            message="Test notification",
            target_url=reverse("rnd:dashboard"),
        )
        marked = self.client.post(
            reverse("rnd:notifications_mark_all_read"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(marked.status_code, 204)
        self.assertFalse(
            RndNotification.objects.filter(recipient=self.rnd_editor, read_at__isnull=True).exists()
        )

        product = self._product(self._collection(code="COL-APPROVAL"), code="P-APPROVAL")
        product.document_status = DevelopmentProduct.DocumentStatus.SUBMITTED
        product.submitted_at = timezone.now()
        product.submitted_by = self.rnd_editor
        product.save(update_fields=("document_status", "submitted_at", "submitted_by", "updated_at"))
        self.client.force_login(self.admin)
        approval_page = self.client.get(reverse("rnd:dashboard"))
        self.assertContains(approval_page, 'data-rnd-notification-tab="approval"')
        self.assertContains(approval_page, "Approve dokumen")
        self.assertContains(approval_page, product.name)
        approval_status = self.client.get(reverse("dashboard:live_status")).json()
        self.assertEqual(approval_status["rnd_approval_count"], 1)
        self.assertEqual(approval_status["rnd_approvals"][0]["title"], "Approve dokumen")

    def test_design_gallery_navigation_expiry_and_delete(self):
        self.client.force_login(self.rnd_editor)
        for number in range(3):
            self.client.post(
                reverse("rnd:designing"),
                {"image": self._image(f"design-{number}.png")},
            )
        newest, middle, oldest = DesignAsset.objects.order_by("-created_at", "-id")

        detail = self.client.get(reverse("rnd:design_detail", args=[middle.id]))
        self.assertContains(detail, reverse("rnd:design_detail", args=[newest.id]))
        self.assertContains(detail, reverse("rnd:design_detail", args=[oldest.id]))
        self.assertContains(detail, 'id="previous-design"')
        self.assertContains(detail, 'id="next-design"')
        self.assertContains(detail, 'event.key === "ArrowLeft"')
        self.assertContains(detail, "Tersedia sampai")

        denied = self.client.post(reverse("rnd:design_delete", args=[middle.id]))
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(DesignAsset.objects.filter(pk=middle.pk).exists())

        self.client.force_login(self.rnd_approver)
        image_storage = middle.image.storage
        image_name = middle.image.name
        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.post(reverse("rnd:design_delete", args=[middle.id]))
        self.assertRedirects(deleted, reverse("rnd:designing"))
        self.assertFalse(DesignAsset.objects.filter(pk=middle.pk).exists())
        self.assertFalse(image_storage.exists(image_name))
        self.assertTrue(
            AuditEvent.objects.filter(action="rnd_design_deleted", entity_id=str(middle.id)).exists()
        )

    def test_designing_purges_files_older_than_180_days(self):
        expired = DesignAsset.objects.create(
            image=self._image("expired.png"),
            original_name="expired.png",
            uploaded_by=self.rnd_editor,
        )
        DesignAsset.objects.filter(pk=expired.pk).update(
            created_at=timezone.now() - timedelta(days=181)
        )
        expired.refresh_from_db()
        image_storage = expired.image.storage
        image_name = expired.image.name

        self.client.force_login(self.rnd_editor)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.get(reverse("rnd:designing"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DesignAsset.objects.filter(pk=expired.pk).exists())
        self.assertFalse(image_storage.exists(image_name))
        audit = AuditEvent.objects.get(action="rnd_design_expired", entity_id=str(expired.id))
        self.assertIsNone(audit.actor)
        self.assertEqual(audit.before_values["original_name"], "expired.png")

    def test_product_form_is_minimal_and_accepts_private_design_files(self):
        collection = self._collection()
        self.client.force_login(self.rnd_editor)

        page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertContains(page, 'data-rnd-panel-toggle')
        self.assertContains(page, 'id="add-product-panel" hidden')
        self.assertContains(page, "Bill of Material (BOM)")
        self.assertContains(page, "Material")
        self.assertContains(page, "Kebutuhan")
        self.assertContains(page, "EOM / Satuan")
        self.assertContains(page, "Notes")
        self.assertContains(page, "+ Tambah Material")
        self.assertContains(page, "Upload Product Cover")
        self.assertContains(page, "Upload Mockup")
        self.assertContains(page, "Upload Technical Drawing")
        for removed_label in (
            "Working Code",
            "Product Story",
            "Target Customer",
            "Target Retail Price",
            "Estimated COGS",
            "Final Sample",
        ):
            self.assertNotContains(page, removed_label)

        response = self.client.post(
            reverse("rnd:collection_detail", args=[collection.id]),
            {
                "name": "Commuter Bag",
                "category": "Bag",
                "status": DevelopmentProduct.Status.CONCEPT,
                "product_cover": self._image("cover.png", size=(1800, 1200)),
                "mockup": SimpleUploadedFile(
                    "mockup.pdf",
                    b"%PDF-1.4\nlocal mockup",
                    content_type="application/pdf",
                ),
                "technical_drawing": SimpleUploadedFile(
                    "technical-drawing.png",
                    b"\x89PNG\r\n\x1a\nlocal drawing",
                    content_type="image/png",
                ),
                "materials-TOTAL_FORMS": "2",
                "materials-INITIAL_FORMS": "0",
                "materials-MIN_NUM_FORMS": "0",
                "materials-MAX_NUM_FORMS": "1000",
                "materials-0-material": "Canvas 12 oz",
                "materials-0-requirement": "1.2",
                "materials-0-eom": "meter",
                "materials-0-notes": "Bahan utama badan tas",
                "materials-1-material": "Resleting YKK 30 cm",
                "materials-1-requirement": "1",
                "materials-1-eom": "pcs",
                "materials-1-notes": "Warna hitam",
            },
        )
        self.assertRedirects(response, reverse("rnd:collection_detail", args=[collection.id]))
        product = collection.products.get(name="Commuter Bag")
        self.assertTrue(product.product_cover.name.startswith("rnd/product_covers/"))
        self.assertTrue(product.product_cover.name.endswith(".webp"))
        with Image.open(product.product_cover.path) as cover:
            self.assertLessEqual(max(cover.size), 1600)
        self.assertTrue(product.mockup.name.startswith("rnd/mockups/"))
        self.assertTrue(product.technical_drawing.name.startswith("rnd/technical_drawings/"))
        self.assertEqual(product.materials.count(), 2)
        canvas = product.materials.get(material="Canvas 12 oz")
        self.assertEqual(canvas.requirement, Decimal("1.2000"))
        self.assertEqual(canvas.eom, "meter")
        self.assertEqual(canvas.notes, "Bahan utama badan tas")

        product_page = self.client.get(f'{reverse("rnd:product_detail", args=[product.id])}?edit=1')
        self.assertContains(product_page, 'value="1.2"')
        self.assertContains(product_page, 'step="0.1"')
        self.assertContains(product_page, "File saat ini")
        self.assertContains(product_page, product.product_cover.name)
        self.assertContains(product_page, product.mockup.name)
        self.assertContains(product_page, product.technical_drawing.name)

        for kind in ("product-cover", "mockup", "technical-drawing"):
            file_response = self.client.get(reverse("rnd:product_file", args=[product.id, kind]))
            self.assertEqual(file_response.status_code, 200)
            self.assertEqual(file_response["X-Content-Type-Options"], "nosniff")

        cover_card = self.client.get(
            f'{reverse("rnd:product_file", args=[product.id, "product-cover"])}?size=card&v=1'
        )
        self.assertEqual(cover_card["Content-Type"], "image/webp")
        self.assertEqual(
            cover_card["Cache-Control"],
            "private, max-age=28800, immutable",
        )

        self.client.logout()
        denied = self.client.get(reverse("rnd:product_file", args=[product.id, "mockup"]))
        self.assertEqual(denied.status_code, 302)

    def test_collection_uses_product_cards_and_draft_opens_one_combined_preview(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR PAGE")
        product.technical_drawing = self._pdf("drawing.pdf", "TECHNICAL DRAWING PAGE")
        product.product_cover = self._image("cover.png")
        product.save(update_fields=("product_cover", "mockup", "technical_drawing", "updated_at"))
        self.client.force_login(self.rnd_editor)

        collection_page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertContains(collection_page, 'class="rnd-product-card"')
        self.assertContains(collection_page, reverse("rnd:product_detail", args=[product.id]))
        self.assertContains(
            collection_page,
            reverse("rnd:product_file", args=[product.id, "product-cover"]),
        )
        self.assertContains(collection_page, f'alt="Product cover {product.name}"')
        self.assertNotContains(collection_page, "<th>Product</th>")

        product_page = self.client.get(reverse("rnd:product_detail", args=[product.id]))
        preview_url = reverse("rnd:product_file", args=[product.id, "combined-preview"])
        self.assertContains(product_page, "Edit Product")
        self.assertNotContains(product_page, "Simpan Product")
        self.assertContains(product_page, preview_url)
        self.assertContains(product_page, f'{preview_url}?v=')
        self.assertContains(product_page, "Preview Mockup Development Request")
        self.assertContains(product_page, "Klik area PDF untuk scroll dan mengatur zoom.")
        self.assertContains(product_page, 'scrolling="yes"')
        self.assertContains(product_page, 'tabindex="0"')
        self.assertContains(product_page, "document.referrer.startsWith(this.href)")
        self.assertContains(product_page, "history.back()")

        edit_page = self.client.get(f'{reverse("rnd:product_detail", args=[product.id])}?edit=1')
        self.assertContains(edit_page, "Tutup Edit")
        self.assertContains(edit_page, "Simpan Product")

        preview = self.client.get(preview_url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "application/pdf")
        self.assertEqual(preview["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(preview["Cache-Control"], "private, no-store")
        cached_preview = self.client.get(f"{preview_url}?v=1")
        self.assertEqual(cached_preview["Cache-Control"], "private, max-age=28800, immutable")
        combined = b"".join(preview.streaming_content)
        reader = PdfReader(BytesIO(combined))
        self.assertEqual(len(reader.pages), 3)
        text = " ".join(page.extract_text() for page in reader.pages)
        self.assertIn("MDR PAGE", text)
        self.assertIn("TECHNICAL DRAWING PAGE", text)
        self.assertIn("BILL OF MATERIAL", reader.pages[2].extract_text())
        self.assertNotIn("000", text)
        self.assertNotIn("DIGITAL APPROVED", text)

    def test_product_upload_rejects_spoofed_file(self):
        collection = self._collection()
        self.client.force_login(self.rnd_editor)
        response = self.client.post(
            reverse("rnd:collection_detail", args=[collection.id]),
            {
                "name": "Invalid File Product",
                "status": DevelopmentProduct.Status.CONCEPT,
                "mockup": SimpleUploadedFile(
                    "mockup.pdf",
                    b"not-a-pdf",
                    content_type="application/pdf",
                ),
                **self._empty_material_formset(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "File harus berupa PDF, JPG, PNG, atau WebP yang valid.")
        self.assertContains(response, 'id="add-product-panel">')
        self.assertFalse(collection.products.filter(name="Invalid File Product").exists())

    def test_product_cover_rejects_pdf(self):
        collection = self._collection()
        self.client.force_login(self.rnd_editor)
        response = self.client.post(
            reverse("rnd:collection_detail", args=[collection.id]),
            {
                "name": "Invalid Cover Product",
                "status": DevelopmentProduct.Status.CONCEPT,
                "product_cover": self._pdf("cover.pdf", "NOT AN IMAGE COVER"),
                **self._empty_material_formset(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Product Cover harus berupa JPG, PNG, atau WebP.")
        self.assertFalse(collection.products.filter(name="Invalid Cover Product").exists())

    def test_marketing_can_open_design_file_only_after_handover(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = SimpleUploadedFile(
            "mockup.pdf",
            b"%PDF-1.4\nlocal mockup",
            content_type="application/pdf",
        )
        product.save(update_fields=("mockup", "updated_at"))
        file_url = reverse("dashboard:upcoming_collection_product_file", args=[product.id, "mockup"])
        self.client.force_login(self.marketing)

        self.assertEqual(self.client.get(file_url).status_code, 404)
        collection.status = Collection.Status.MARKETING_REVIEW
        collection.save(update_fields=("status", "updated_at"))
        response = self.client.get(file_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_editor_cannot_approve_or_handover_collection(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR")
        product.technical_drawing = self._pdf("drawing.pdf", "TECHNICAL DRAWING")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))
        self.client.force_login(self.rnd_editor)

        submitted = self.client.post(reverse("rnd:product_submit", args=[product.id]))
        self.assertRedirects(submitted, reverse("rnd:product_detail", args=[product.id]))
        finalize = self.client.post(reverse("rnd:product_approve", args=[product.id]))
        self.assertEqual(finalize.status_code, 403)
        product.refresh_from_db()
        self.assertEqual(product.status, DevelopmentProduct.Status.CONCEPT)

        handover = self.client.post(reverse("rnd:collection_handover", args=[collection.id]))
        self.assertEqual(handover.status_code, 403)
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.DOCUMENT_APPROVAL)

    def test_approver_can_publish_idempotent_marketing_preview_without_workflow_change(self):
        collection = self._collection()
        product = self._product(collection)
        product.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        product.approved_document = self._pdf("approved.pdf", "APPROVED MDR")
        product.save(update_fields=("document_status", "approved_document", "updated_at"))
        self.client.force_login(self.rnd_approver)

        endpoint = reverse("rnd:collection_marketing_preview", args=[collection.id])
        first = self.client.post(endpoint)
        self.assertRedirects(first, reverse("rnd:collection_detail", args=[collection.id]))
        collection.refresh_from_db()
        self.assertIsNotNone(collection.marketing_previewed_at)
        self.assertEqual(collection.marketing_previewed_by, self.rnd_approver)
        self.assertEqual(collection.status, Collection.Status.DRAFT)
        self.assertIsNone(collection.handed_over_at)

        self.client.post(endpoint)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="rnd_collection_marketing_preview_published",
                entity_id=str(collection.id),
            ).count(),
            1,
        )

    def test_marketing_preview_is_read_only_and_shows_only_approved_products(self):
        collection = self._collection()
        approved = self._product(collection, code="P-APPROVED")
        approved.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        approved.approved_document = self._pdf("approved.pdf", "APPROVED MDR")
        approved.product_cover = self._image("approved.png")
        approved.category = "Flannel Shirt"
        approved.save(
            update_fields=(
                "document_status",
                "approved_document",
                "product_cover",
                "category",
                "updated_at",
            )
        )
        draft = self._product(collection, code="P-DRAFT")
        collection.marketing_previewed_at = timezone.now()
        collection.marketing_previewed_by = self.rnd_approver
        collection.save(update_fields=("marketing_previewed_at", "marketing_previewed_by", "updated_at"))
        self.client.force_login(self.marketing)

        listing = self.client.get(reverse("dashboard:upcoming_collection_list"))
        self.assertContains(listing, collection.name)
        self.assertContains(listing, "R&amp;D Preview", html=False)
        self.assertContains(listing, "rnd-collection-card")
        self.assertNotIn(b"<table", listing.content)
        detail = self.client.get(
            reverse("dashboard:upcoming_collection_detail", args=[collection.id])
        )
        self.assertContains(detail, approved.name)
        self.assertNotContains(detail, "Nama Product")
        self.assertNotContains(detail, "Nama Article")
        self.assertNotContains(detail, approved.category)
        self.assertNotContains(detail, draft.name)
        self.assertContains(detail, "rnd-preview-cover-card")
        self.assertNotIn(b"<table", detail.content)
        self.assertNotContains(detail, "Beri rekomendasi")
        self.assertNotContains(detail, "Official Decision")
        self.assertFalse(MarketingRecommendation.objects.exists())
        approved_file = reverse(
            "dashboard:upcoming_collection_product_file",
            args=[approved.id, "approved-document"],
        )
        self.assertNotContains(detail, approved_file)
        self.assertEqual(self.client.get(approved_file).status_code, 404)
        cover_file = reverse(
            "dashboard:upcoming_collection_product_file",
            args=[approved.id, "product-cover"],
        )
        self.assertContains(detail, f'href="{cover_file}?v=')
        self.assertEqual(self.client.get(cover_file).status_code, 200)
        raw_mockup = reverse(
            "dashboard:upcoming_collection_product_file",
            args=[approved.id, "mockup"],
        )
        self.assertEqual(self.client.get(raw_mockup).status_code, 404)

    def test_editor_cannot_publish_marketing_preview(self):
        collection = self._collection()
        product = self._product(collection)
        product.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        product.save(update_fields=("document_status", "updated_at"))
        self.client.force_login(self.rnd_editor)

        response = self.client.post(
            reverse("rnd:collection_marketing_preview", args=[collection.id])
        )
        self.assertEqual(response.status_code, 403)
        collection.refresh_from_db()
        self.assertIsNone(collection.marketing_previewed_at)

    def test_submit_and_superadmin_approval_generate_one_audited_pdf(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR PAGE")
        product.technical_drawing = self._pdf("drawing.pdf", "TECHNICAL DRAWING PAGE")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))
        DevelopmentProductMaterial.objects.create(
            product=product,
            material="Katun Flannel",
            requirement="1.5",
            eom="Yard",
            notes="Motif sesuai mockup",
        )
        product.mockup.open("rb")
        source_mockup = product.mockup.read()
        product.mockup.close()

        self.client.force_login(self.rnd_editor)
        submitted = self.client.post(reverse("rnd:product_submit", args=[product.id]))
        self.assertRedirects(submitted, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        collection.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.SUBMITTED)
        self.assertEqual(collection.status, Collection.Status.DOCUMENT_APPROVAL)
        self.assertIsNotNone(product.submitted_at)
        self.assertEqual(product.submitted_by, self.rnd_editor)
        submitted_pdf = PdfReader(product.submitted_document.path)
        self.assertEqual(len(submitted_pdf.pages), 3)
        submitted_page_texts = [page.extract_text() for page in submitted_pdf.pages]
        submitted_text = " ".join(submitted_page_texts)
        self.assertIn("MDR PAGE", submitted_text)
        self.assertIn("TECHNICAL DRAWING PAGE", submitted_text)
        self.assertIn("BILL OF MATERIAL", submitted_page_texts[2])
        self.assertIn("Katun Flannel", submitted_page_texts[2])
        self.assertIn("1,5", submitted_page_texts[2])
        self.assertIn("Yard", submitted_page_texts[2])
        self.assertIn("Motif sesuai mockup", submitted_page_texts[2])
        self.assertTrue(all("000" in text for text in submitted_page_texts))
        self.assertNotIn("DIGITAL APPROVED", submitted_text)
        revision = product.document_revisions.get(revision=0)
        self.assertEqual(revision.status, DevelopmentProductDocumentRevision.Status.SUBMITTED)
        self.assertEqual(revision.submitted_document.name, product.submitted_document.name)

        locked = self.client.post(
            reverse("rnd:product_detail", args=[product.id]),
            self._product_payload(product, DevelopmentProduct.Status.REVISION),
        )
        self.assertEqual(locked.status_code, 403)

        self.admin.first_name = "Aditya"
        self.admin.last_name = "Saputra"
        self.admin.save(update_fields=("first_name", "last_name"))
        self.client.force_login(self.admin)
        approved = self.client.post(reverse("rnd:product_approve", args=[product.id]))
        self.assertRedirects(approved, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        collection.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.APPROVED)
        self.assertEqual(collection.status, Collection.Status.READY_FOR_DEVELOPMENT)
        self.assertEqual(product.status, DevelopmentProduct.Status.CONCEPT)
        self.assertEqual(product.rnd_approved_by, self.admin)
        revision.refresh_from_db()
        self.assertEqual(revision.status, DevelopmentProductDocumentRevision.Status.APPROVED)
        self.assertEqual(revision.approved_document.name, product.approved_document.name)
        final_pdf = PdfReader(product.approved_document.path)
        self.assertEqual(len(final_pdf.pages), 3)
        first_page_text = final_pdf.pages[0].extract_text()
        second_page_text = final_pdf.pages[1].extract_text()
        third_page_text = final_pdf.pages[2].extract_text()
        self.assertNotIn("DIGITAL APPROVED", first_page_text)
        self.assertIn("Aditya Saputra", first_page_text)
        self.assertIn("Aditya Saputra", second_page_text)
        self.assertNotIn("DIGITAL APPROVED", second_page_text)
        self.assertIn("Aditya Saputra", third_page_text)
        self.assertNotIn("DIGITAL APPROVED", third_page_text)
        for page in final_pdf.pages:
            xobjects = page["/Resources"]["/XObject"].get_object()
            self.assertTrue(
                any(item.get_object().get("/Subtype") == "/Image" for item in xobjects.values())
            )
        product.mockup.open("rb")
        self.assertEqual(product.mockup.read(), source_mockup)
        product.mockup.close()

    def test_submit_preserves_pdf_pages_with_array_content_streams(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR PAGE")
        product.technical_drawing = self._pdf_with_array_contents(
            "drawing.pdf",
            "TECH PACK ARRAY PAGE",
        )
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))
        self.client.force_login(self.rnd_editor)

        self.client.post(reverse("rnd:product_submit", args=[product.id]))
        product.refresh_from_db()
        submitted_pdf = PdfReader(product.submitted_document.path)

        self.assertEqual(len(submitted_pdf.pages), 3)
        self.assertIn("MDR PAGE", submitted_pdf.pages[0].extract_text())
        self.assertIn("TECH PACK ARRAY PAGE", submitted_pdf.pages[1].extract_text())

        live_preview = self.client.get(
            reverse("rnd:product_revision_file", args=[product.id, 0])
        )
        live_pdf = PdfReader(BytesIO(b"".join(live_preview.streaming_content)))
        self.assertEqual(len(live_pdf.pages), 3)
        self.assertIn("TECH PACK ARRAY PAGE", live_pdf.pages[1].extract_text())

    def test_superadmin_can_reject_and_editor_can_edit_then_resubmit_same_revision(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR REJECT TEST")
        product.technical_drawing = self._pdf("drawing.pdf", "DRAWING REJECT TEST")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))

        self.client.force_login(self.rnd_editor)
        self.client.post(reverse("rnd:product_submit", args=[product.id]))
        denied = self.client.post(reverse("rnd:product_reject", args=[product.id]))
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.admin)
        rejected = self.client.post(reverse("rnd:product_reject", args=[product.id]))
        self.assertRedirects(rejected, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.REJECTED)
        self.assertEqual(product.document_revision, 0)
        revision = product.document_revisions.get(revision=0)
        self.assertEqual(revision.status, DevelopmentProductDocumentRevision.Status.REJECTED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="rnd_product_document_rejected",
                entity_id=str(product.id),
            ).exists()
        )

        self.client.force_login(self.rnd_editor)
        page = self.client.get(reverse("rnd:product_detail", args=[product.id]))
        self.assertContains(page, "Edit Product")
        self.assertContains(page, "Delete Product")
        self.assertContains(page, "Submit Ulang Approval")
        edited = self.client.post(
            reverse("rnd:product_detail", args=[product.id]),
            self._product_payload(product, DevelopmentProduct.Status.CONCEPT),
        )
        self.assertRedirects(edited, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.category, "Bag")
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.REJECTED)
        self.assertEqual(product.document_revision, 0)

        resubmitted = self.client.post(reverse("rnd:product_submit", args=[product.id]))
        self.assertRedirects(resubmitted, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        revision.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.SUBMITTED)
        self.assertEqual(product.document_revision, 0)
        self.assertEqual(product.document_revisions.count(), 1)
        self.assertEqual(revision.status, DevelopmentProductDocumentRevision.Status.SUBMITTED)

    def test_editor_can_delete_only_a_rejected_product(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("delete-rejected.pdf", "DELETE REJECTED")
        product.technical_drawing = self._pdf("delete-drawing.pdf", "DELETE DRAWING")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))
        mockup_name = product.mockup.name
        storage = product.mockup.storage

        self.client.force_login(self.rnd_editor)
        blocked = self.client.post(
            reverse("rnd:product_delete", args=[product.id]),
            follow=True,
        )
        self.assertContains(blocked, "Product hanya dapat dihapus setelah dokumennya di-reject.")
        self.assertTrue(DevelopmentProduct.objects.filter(pk=product.id).exists())
        self.client.post(reverse("rnd:product_submit", args=[product.id]))

        self.client.force_login(self.admin)
        self.client.post(reverse("rnd:product_reject", args=[product.id]))
        self.client.force_login(self.rnd_editor)
        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.post(reverse("rnd:product_delete", args=[product.id]))
        self.assertRedirects(deleted, reverse("rnd:collection_detail", args=[collection.id]))
        self.assertFalse(DevelopmentProduct.objects.filter(pk=product.id).exists())
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.DRAFT)
        self.assertFalse(storage.exists(mockup_name))
        self.assertTrue(
            AuditEvent.objects.filter(
                action="rnd_rejected_product_deleted",
                entity_id=str(product.id),
            ).exists()
        )

    def test_editor_can_duplicate_product_as_independent_draft_with_bom(self):
        collection = self._collection()
        product = self._product(collection)
        product.name = "Kelabu"
        product.category = "Shirt"
        product.product_cover = self._image("kelabu-cover.png")
        product.mockup = self._pdf("kelabu-mockup.pdf", "KELABU MOCKUP")
        product.technical_drawing = self._pdf("kelabu-techpack.pdf", "KELABU TECHPACK")
        product.document_status = DevelopmentProduct.DocumentStatus.SUBMITTED
        product.submitted_at = timezone.now()
        product.submitted_by = self.rnd_editor
        product.save()
        DevelopmentProductMaterial.objects.create(
            product=product,
            material="Katun Flannel",
            requirement=Decimal("1.5000"),
            eom="Yard",
            notes="Gunakan warna utama",
        )
        self.client.force_login(self.rnd_editor)

        page = self.client.get(reverse("rnd:product_detail", args=[product.id]))
        self.assertContains(page, "Duplicate Product")
        response = self.client.post(reverse("rnd:product_duplicate", args=[product.id]))

        duplicate = DevelopmentProduct.objects.exclude(pk=product.pk).get()
        self.assertRedirects(
            response,
            f'{reverse("rnd:product_detail", args=[duplicate.id])}?edit=1#edit-product',
        )
        self.assertEqual(duplicate.collection, collection)
        self.assertEqual(duplicate.name, "Kelabu Copy")
        self.assertEqual(duplicate.category, product.category)
        self.assertEqual(duplicate.document_status, DevelopmentProduct.DocumentStatus.DRAFT)
        self.assertEqual(duplicate.document_revision, 0)
        self.assertEqual(duplicate.status, DevelopmentProduct.Status.CONCEPT)
        self.assertIsNone(duplicate.submitted_at)
        self.assertIsNone(duplicate.submitted_by)
        self.assertEqual(duplicate.document_revisions.count(), 0)
        self.assertEqual(
            list(duplicate.materials.values_list("material", "requirement", "eom", "notes")),
            [("Katun Flannel", Decimal("1.5000"), "Yard", "Gunakan warna utama")],
        )
        for field_name in ("product_cover", "mockup", "technical_drawing"):
            source_file = getattr(product, field_name)
            duplicate_file = getattr(duplicate, field_name)
            self.assertNotEqual(source_file.name, duplicate_file.name)
            source_file.open("rb")
            duplicate_file.open("rb")
            self.assertEqual(source_file.read(), duplicate_file.read())
            source_file.close()
            duplicate_file.close()
        self.assertTrue(
            AuditEvent.objects.filter(
                action="rnd_product_duplicated",
                entity_id=str(duplicate.id),
            ).exists()
        )

    def test_product_duplicate_requires_rnd_edit_and_unlocked_collection(self):
        collection = self._collection()
        product = self._product(collection)
        self.client.force_login(self.marketing)
        self.assertEqual(
            self.client.post(reverse("rnd:product_duplicate", args=[product.id])).status_code,
            403,
        )
        collection.development_started_at = timezone.now()
        collection.save(update_fields=("development_started_at", "updated_at"))
        self.client.force_login(self.rnd_editor)
        response = self.client.post(
            reverse("rnd:product_duplicate", args=[product.id]),
            follow=True,
        )
        self.assertContains(response, "Product tidak dapat diduplikat setelah Development dimulai")
        self.assertEqual(collection.products.count(), 1)

    def test_document_revision_history_is_selectable_and_old_pdf_is_preserved(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup-000.pdf", "MDR REVISION ZERO")
        product.technical_drawing = self._pdf("drawing-000.pdf", "DRAWING REVISION ZERO")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))

        self.client.force_login(self.rnd_editor)
        self.client.post(reverse("rnd:product_submit", args=[product.id]))
        denied = self.client.post(reverse("rnd:product_request_revision", args=[product.id]))
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.admin)
        submitted_page = self.client.get(reverse("rnd:product_detail", args=[product.id]))
        self.assertContains(submitted_page, "Approve Dokumen")
        self.assertContains(submitted_page, "Reject Dokumen")
        self.assertContains(submitted_page, "Minta Revisi")
        self.assertContains(submitted_page, "Notes revisi")
        missing_note = self.client.post(
            reverse("rnd:product_request_revision", args=[product.id]),
            {"revision_target": DevelopmentProductDocumentRevision.RevisionTarget.MOCKUP},
            follow=True,
        )
        self.assertContains(missing_note, "Notes revisi wajib diisi.")
        product.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.SUBMITTED)

        missing_target = self.client.post(
            reverse("rnd:product_request_revision", args=[product.id]),
            {"revision_note": "Perbaiki ukuran kerah."},
            follow=True,
        )
        self.assertContains(missing_target, "Pilih bagian dokumen yang harus direvisi.")
        product.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.SUBMITTED)

        requested = self.client.post(
            reverse("rnd:product_request_revision", args=[product.id]),
            {
                "revision_note": "Perbaiki ukuran kerah dan posisi kancing.",
                "revision_target": DevelopmentProductDocumentRevision.RevisionTarget.MOCKUP,
            },
        )
        self.assertRedirects(requested, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.REVISION_REQUESTED)
        self.assertEqual(product.status, DevelopmentProduct.Status.REVISION)
        revision_zero = product.document_revisions.get(revision=0)
        original_technical_drawing = product.technical_drawing.name
        self.assertEqual(
            revision_zero.status,
            DevelopmentProductDocumentRevision.Status.REVISION_REQUESTED,
        )
        self.assertEqual(revision_zero.revision_note, "Perbaiki ukuran kerah dan posisi kancing.")
        self.assertEqual(
            revision_zero.revision_target,
            DevelopmentProductDocumentRevision.RevisionTarget.MOCKUP,
        )
        revision_page = self.client.get(reverse("rnd:product_detail", args=[product.id]))
        self.assertContains(revision_page, "Target revisi:")
        self.assertContains(revision_page, "Mockup")
        self.assertContains(revision_page, "Notes revisi:")
        self.assertContains(revision_page, "Perbaiki ukuran kerah dan posisi kancing.")

        self.client.force_login(self.rnd_editor)
        missing_revised_mockup = self.client.post(
            reverse("rnd:product_detail", args=[product.id]),
            {
                "name": product.name,
                "category": "Shirt",
                "status": DevelopmentProduct.Status.REVISION,
                **self._empty_material_formset(),
            },
        )
        self.assertContains(
            missing_revised_mockup,
            "Upload Mockup baru sesuai permintaan revisi.",
        )
        product.refresh_from_db()
        self.assertEqual(product.document_revision, 0)

        revised = self.client.post(
            reverse("rnd:product_detail", args=[product.id]),
            {
                "name": product.name,
                "category": "Shirt",
                "status": DevelopmentProduct.Status.REVISION,
                "mockup": self._pdf("mockup-001.pdf", "MDR REVISION ONE"),
                **self._empty_material_formset(),
            },
        )
        self.assertRedirects(revised, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.document_revision, 1)
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.DRAFT)
        self.assertEqual(product.technical_drawing.name, original_technical_drawing)
        self.assertFalse(product.submitted_document)
        self.assertEqual(product.document_revisions.count(), 1)

        self.client.post(reverse("rnd:product_submit", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.document_revisions.count(), 2)
        page = self.client.get(reverse("rnd:product_detail", args=[product.id]))
        revision_zero_url = reverse("rnd:product_revision_file", args=[product.id, 0])
        revision_one_url = reverse("rnd:product_revision_file", args=[product.id, 1])
        self.assertContains(page, f'?revision=0')
        self.assertContains(page, revision_one_url)
        self.assertContains(page, ">000</a>", html=False)
        self.assertContains(page, ">001</a>", html=False)

        old_page = self.client.get(f'{reverse("rnd:product_detail", args=[product.id])}?revision=0')
        self.assertContains(old_page, revision_zero_url)
        old_file = self.client.get(revision_zero_url)
        self.assertEqual(old_file["Cache-Control"], "private, no-store")
        old_pdf = PdfReader(BytesIO(b"".join(old_file.streaming_content)))
        old_text = " ".join(pdf_page.extract_text() for pdf_page in old_pdf.pages)
        self.assertIn("MDR REVISION ZERO", old_text)
        self.assertNotIn("MDR REVISION ONE", old_text)

    def test_submit_requires_both_design_documents(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR")
        product.save(update_fields=("mockup", "updated_at"))
        self.client.force_login(self.rnd_editor)

        response = self.client.post(
            reverse("rnd:product_submit", args=[product.id]),
            follow=True,
        )
        self.assertContains(response, "Mockup dan Technical Drawing wajib tersedia")
        product.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.DRAFT)
        self.assertFalse(product.submitted_document)

    def test_superadmin_can_add_note_to_legacy_revision_request(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR")
        product.technical_drawing = self._pdf("drawing.pdf", "DRAWING")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))
        self.client.force_login(self.rnd_editor)
        self.client.post(reverse("rnd:product_submit", args=[product.id]))
        revision = product.document_revisions.get(revision=0)
        revision.status = DevelopmentProductDocumentRevision.Status.REVISION_REQUESTED
        revision.save(update_fields=("status", "updated_at"))
        product.document_status = DevelopmentProduct.DocumentStatus.REVISION_REQUESTED
        product.status = DevelopmentProduct.Status.REVISION
        product.save(update_fields=("document_status", "status", "updated_at"))

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("rnd:product_request_revision", args=[product.id]),
            {
                "revision_note": "Perbaiki panjang lengan.",
                "revision_target": DevelopmentProductDocumentRevision.RevisionTarget.TECHNICAL_DRAWING,
            },
        )
        self.assertRedirects(response, reverse("rnd:product_detail", args=[product.id]))
        revision.refresh_from_db()
        self.assertEqual(revision.revision_note, "Perbaiki panjang lengan.")
        self.assertEqual(
            revision.revision_target,
            DevelopmentProductDocumentRevision.RevisionTarget.TECHNICAL_DRAWING,
        )

    def test_handover_requires_every_product_final_and_locks_rnd(self):
        collection = self._collection()
        first = self._product(collection, "P-001", DevelopmentProduct.Status.FINAL_APPROVED)
        second = self._product(collection, "P-002")
        self.client.force_login(self.rnd_approver)

        blocked = self.client.post(reverse("rnd:collection_handover", args=[collection.id]), follow=True)
        self.assertContains(blocked, "Collection wajib menyelesaikan alur Development")
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.DRAFT)

        second.status = DevelopmentProduct.Status.FINAL_APPROVED
        second.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        second.rnd_approved_at = timezone.now()
        second.rnd_approved_by = self.admin
        second.save(
            update_fields=(
                "status",
                "document_status",
                "rnd_approved_at",
                "rnd_approved_by",
                "updated_at",
            )
        )
        started = self.client.post(reverse("rnd:collection_start_development", args=[collection.id]))
        self.assertRedirects(started, reverse("rnd:development_detail", args=[collection.id]))
        self.assertFalse(
            collection.products.exclude(
                development_stage=DevelopmentProduct.DevelopmentStage.MATERIAL_PURCHASE,
                prototype_number=0,
            ).exists()
        )
        collection.products.update(
            development_stage=DevelopmentProduct.DevelopmentStage.FINAL,
            prototype_number=1,
        )
        handed = self.client.post(reverse("rnd:collection_handover", args=[collection.id]))
        self.assertRedirects(handed, reverse("rnd:collection_detail", args=[collection.id]))
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.MARKETING_REVIEW)
        self.assertEqual(collection.handed_over_by, self.rnd_approver)

        locked = self.client.post(
            reverse("rnd:product_detail", args=[first.id]),
            self._product_payload(first, DevelopmentProduct.Status.REVISION),
        )
        self.assertEqual(locked.status_code, 403)

    def test_collection_enters_development_only_after_all_documents_are_approved(self):
        collection = self._collection()
        first = self._product(collection, "P-001")
        second = self._product(collection, "P-002")
        first.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        first.status = DevelopmentProduct.Status.SAMPLING
        first.save(update_fields=("document_status", "status", "updated_at"))
        self.client.force_login(self.rnd_editor)

        page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertContains(page, "1 / 2 Approved")
        self.assertContains(page, "Lanjut Development")
        blocked = self.client.post(
            reverse("rnd:collection_start_development", args=[collection.id]),
            follow=True,
        )
        self.assertContains(blocked, "Seluruh dokumen Product wajib Approved")
        collection.refresh_from_db()
        self.assertIsNone(collection.development_started_at)

        second.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        second.status = DevelopmentProduct.Status.SAMPLING
        second.save(update_fields=("document_status", "status", "updated_at"))
        started = self.client.post(reverse("rnd:collection_start_development", args=[collection.id]))
        self.assertRedirects(started, reverse("rnd:development_detail", args=[collection.id]))
        collection.refresh_from_db()
        self.assertEqual(collection.development_started_by, self.rnd_editor)
        self.assertIsNotNone(collection.development_started_at)
        self.assertEqual(collection.status, Collection.Status.DEVELOPMENT)
        product_page = self.client.get(reverse("rnd:product_detail", args=[first.id]))
        self.assertContains(product_page, "Kembali ke Product Development")
        self.assertContains(product_page, reverse("rnd:development_detail", args=[collection.id]))
        self.assertNotContains(product_page, "Kembali ke Collection")
        self.assertFalse(
            collection.products.exclude(
                development_stage=DevelopmentProduct.DevelopmentStage.MATERIAL_PURCHASE
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action="rnd_collection_development_started",
                entity_id=str(collection.id),
            ).exists()
        )
        development_page = self.client.get(reverse("rnd:development_list"))
        self.assertContains(development_page, collection.name)
        collection_page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertNotContains(collection_page, ">Tambah Product</button>")
        blocked_product = self.client.post(
            reverse("rnd:collection_detail", args=[collection.id]),
            {"name": "Late Product", **self._empty_material_formset()},
        )
        self.assertEqual(blocked_product.status_code, 403)
        self.assertFalse(collection.products.filter(name="Late Product").exists())

        self.client.post(reverse("rnd:collection_start_development", args=[collection.id]))
        self.assertEqual(
            AuditEvent.objects.filter(
                action="rnd_collection_development_started",
                entity_id=str(collection.id),
            ).count(),
            1,
        )

    def test_development_product_loops_through_numbered_prototypes(self):
        collection = self._collection()
        product = self._product(collection)
        product.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        product.status = DevelopmentProduct.Status.SAMPLING
        product.save(update_fields=("document_status", "status", "updated_at"))
        self.client.force_login(self.rnd_editor)
        self.client.post(reverse("rnd:collection_start_development", args=[collection.id]))
        development_page = self.client.get(reverse("rnd:development_detail", args=[collection.id]))
        self.assertContains(
            development_page,
            reverse("rnd:development_product_detail", args=[product.id]),
        )
        self.assertContains(development_page, "Buka Timeline")
        self.assertContains(development_page, "Isi Pembelian Material")

        blocked_material = self.client.post(
            reverse("rnd:product_development_transition", args=[product.id]),
            {"action": "material_completed"},
            follow=True,
        )
        self.assertContains(blocked_material, "Isi minimal satu material dan harga beli")
        material_stage = DevelopmentProductStageDate.objects.create(
            product=product,
            stage_key="material_purchase",
            updated_by=self.rnd_editor,
        )
        DevelopmentProductStageMaterial.objects.create(
            stage=material_stage,
            material="Material Awal",
            purchase_price=Decimal("125000"),
        )

        for action, expected_stage in (
            ("material_completed", DevelopmentProduct.DevelopmentStage.SAMPLING),
            ("sampling_completed", DevelopmentProduct.DevelopmentStage.PROTOTYPE),
        ):
            response = self.client.post(
                reverse("rnd:product_development_transition", args=[product.id]),
                {"action": action},
            )
            self.assertRedirects(response, reverse("rnd:development_detail", args=[collection.id]))
            product.refresh_from_db()
            self.assertEqual(product.development_stage, expected_stage)
        self.assertEqual(product.prototype_number, 1)
        self.assertEqual(product.development_stage_label, "Prototype 1")

        denied = self.client.post(
            reverse("rnd:product_development_transition", args=[product.id]),
            {"action": "prototype_resampling"},
        )
        self.assertEqual(denied.status_code, 403)
        product.refresh_from_db()
        self.assertEqual(product.development_stage, DevelopmentProduct.DevelopmentStage.PROTOTYPE)

        self.client.force_login(self.rnd_approver)
        self.client.post(
            reverse("rnd:product_development_transition", args=[product.id]),
            {"action": "prototype_resampling"},
        )
        product.refresh_from_db()
        self.assertEqual(product.development_stage, DevelopmentProduct.DevelopmentStage.RESAMPLING)
        self.assertEqual(product.development_stage_label, "Resampling menuju Prototype 2")

        self.client.force_login(self.rnd_editor)
        self.client.post(
            reverse("rnd:product_development_transition", args=[product.id]),
            {"action": "resampling_completed"},
        )
        product.refresh_from_db()
        self.assertEqual(product.development_stage_label, "Prototype 2")
        timeline_page = self.client.get(
            reverse("rnd:development_product_detail", args=[product.id])
        )
        self.assertEqual(
            [step["label"] for step in timeline_page.context["timeline"]],
            [
                "Pembelian Material",
                "Sampling",
                "Prototype 1",
                "Revisi Prototype 1",
                "Resampling 1",
                "Prototype 2",
                "Final Development",
            ],
        )
        self.assertEqual(timeline_page.context["timeline"][-2]["state"], "active")
        self.assertNotContains(timeline_page, "Mockup + Technical Drawing")
        self.assertContains(timeline_page, "Target dan alur kerja")
        self.assertContains(timeline_page, "Belum ada notes.")
        self.assertContains(timeline_page, "+ Tambah Material")
        dated = self.client.post(
            reverse("rnd:development_product_detail", args=[product.id]),
            {
                "stage_key": "prototype_2",
                "target_date": "2026-09-20",
                "actual_date": "2026-09-22",
                "notes": "Sample sudah sesuai warna.",
                "image": self._image("prototype-2.png"),
            },
        )
        self.assertRedirects(
            dated,
            f'{reverse("rnd:development_product_detail", args=[product.id])}#development-timeline',
        )
        stage_date = DevelopmentProductStageDate.objects.get(
            product=product,
            stage_key="prototype_2",
        )
        self.assertEqual(stage_date.target_date.isoformat(), "2026-09-20")
        self.assertEqual(stage_date.actual_date.isoformat(), "2026-09-22")
        self.assertEqual(stage_date.notes, "Sample sudah sesuai warna.")
        self.assertEqual(stage_date.updated_by, self.rnd_editor)
        attachment = DevelopmentProductStageAttachment.objects.get(stage=stage_date)
        self.assertTrue(attachment.image.name.endswith(".webp"))
        self.assertEqual(
            self.client.get(
                reverse("rnd:development_stage_attachment_file", args=[attachment.id])
            ).status_code,
            200,
        )
        material_response = self.client.post(
            reverse("rnd:development_product_detail", args=[product.id]),
            {
                "action": "add_material",
                "stage_key": "material_purchase",
                "material": "Flannel 12 oz",
                "purchase_price": "125000",
            },
        )
        self.assertEqual(material_response.status_code, 302)
        material = DevelopmentProductStageMaterial.objects.get(
            stage__product=product,
            stage__stage_key="material_purchase",
            material="Flannel 12 oz",
        )
        self.assertEqual(material.material, "Flannel 12 oz")
        self.assertEqual(material.purchase_price, Decimal("125000"))
        dated_page = self.client.get(reverse("rnd:development_product_detail", args=[product.id]))
        self.assertContains(dated_page, "20 Sep 2026")
        self.assertContains(dated_page, "22 Sep 2026")
        self.assertContains(dated_page, "Sample sudah sesuai warna.")
        self.assertContains(dated_page, "Flannel 12 oz")

        self.client.force_login(self.marketing)
        self.assertEqual(
            self.client.get(
                reverse("rnd:development_stage_attachment_file", args=[attachment.id])
            ).status_code,
            403,
        )

        self.client.force_login(self.rnd_approver)
        self.client.post(
            reverse("rnd:product_development_transition", args=[product.id]),
            {"action": "prototype_final"},
        )
        product.refresh_from_db()
        collection.refresh_from_db()
        self.assertEqual(product.development_stage, DevelopmentProduct.DevelopmentStage.FINAL)
        self.assertEqual(product.status, DevelopmentProduct.Status.FINAL_APPROVED)
        self.assertEqual(collection.status, Collection.Status.FINAL_DEVELOPMENT)

    def test_development_collection_summary_counts_material_entries_and_current_stages(self):
        collection = self._collection()
        collection.development_started_at = timezone.now()
        collection.save(update_fields=("development_started_at", "updated_at"))
        stages = (
            (DevelopmentProduct.DevelopmentStage.MATERIAL_PURCHASE, 0),
            (DevelopmentProduct.DevelopmentStage.SAMPLING, 0),
            (DevelopmentProduct.DevelopmentStage.PROTOTYPE, 1),
            (DevelopmentProduct.DevelopmentStage.RESAMPLING, 1),
            (DevelopmentProduct.DevelopmentStage.FINAL, 2),
        )
        products = []
        for number, (stage, prototype_number) in enumerate(stages, start=1):
            product = self._product(collection, f"P-{number:03d}")
            product.development_stage = stage
            product.prototype_number = prototype_number
            product.save(update_fields=("development_stage", "prototype_number", "updated_at"))
            products.append(product)
        for product in products[:2]:
            material_stage = DevelopmentProductStageDate.objects.create(
                product=product,
                stage_key="material_purchase",
                updated_by=self.rnd_editor,
            )
            DevelopmentProductStageMaterial.objects.create(
                stage=material_stage,
                material="Material Awal",
                purchase_price=Decimal("125000"),
            )

        self.client.force_login(self.rnd_editor)
        response = self.client.get(reverse("rnd:development_detail", args=[collection.id]))

        self.assertEqual(response.context["material_count"], 2)
        self.assertEqual(response.context["sampling_count"], 1)
        self.assertEqual(response.context["prototype_count"], 2)
        self.assertEqual(response.context["final_count"], 1)
        self.assertContains(response, "Pembelian Material")
        self.assertContains(response, "Prototype")

    def test_marketing_cannot_start_or_update_development(self):
        collection = self._collection()
        product = self._product(collection)
        product.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        product.save(update_fields=("document_status", "updated_at"))
        self.client.force_login(self.marketing)

        denied = self.client.post(reverse("rnd:collection_start_development", args=[collection.id]))
        self.assertEqual(denied.status_code, 403)
        collection.refresh_from_db()
        self.assertIsNone(collection.development_started_at)

    def test_collection_delete_requires_rnd_approve_and_removes_private_files(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup.save("delete-me.pdf", self._pdf("delete-me.pdf", "MDR"), save=True)
        mockup_name = product.mockup.name
        storage = product.mockup.storage

        self.client.force_login(self.rnd_editor)
        page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertNotContains(page, "Delete Collection")
        denied = self.client.post(reverse("rnd:collection_delete", args=[collection.id]))
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(Collection.objects.filter(pk=collection.id).exists())

        self.client.force_login(self.rnd_approver)
        page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertContains(page, "Delete Collection")
        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.post(reverse("rnd:collection_delete", args=[collection.id]))
        self.assertRedirects(deleted, reverse("rnd:dashboard"))
        self.assertFalse(Collection.objects.filter(pk=collection.id).exists())
        self.assertFalse(DevelopmentProduct.objects.filter(pk=product.id).exists())
        self.assertFalse(storage.exists(mockup_name))
        self.assertTrue(
            AuditEvent.objects.filter(
                action="rnd_collection_deleted",
                entity_id=str(collection.id),
            ).exists()
        )

    def test_collection_delete_is_blocked_after_marketing_handover(self):
        collection = self._collection()
        collection.status = Collection.Status.MARKETING_REVIEW
        collection.handed_over_at = timezone.now()
        collection.handed_over_by = self.rnd_approver
        collection.save()
        self.client.force_login(self.rnd_approver)

        page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertNotContains(page, "Delete Collection")
        blocked = self.client.post(
            reverse("rnd:collection_delete", args=[collection.id]),
            follow=True,
        )
        self.assertContains(blocked, "sudah di-handover ke Marketing tidak dapat dihapus")
        self.assertTrue(Collection.objects.filter(pk=collection.id).exists())

    def test_marketing_recommends_and_only_superadmin_makes_official_decision(self):
        collection = self._collection()
        product = self._product(collection, status=DevelopmentProduct.Status.FINAL_APPROVED)
        collection.status = Collection.Status.MARKETING_REVIEW
        collection.handed_over_at = timezone.now()
        collection.handed_over_by = self.rnd_approver
        collection.save()

        self.client.force_login(self.marketing)
        upcoming = self.client.get(reverse("dashboard:upcoming_collection_list"))
        self.assertContains(upcoming, collection.name)
        missing_qty = self.client.post(
            reverse("dashboard:upcoming_collection_recommend", args=[product.id]),
            {"recommendation": "GO", "recommended_quantity": "", "rationale": "Strong demand"},
        )
        self.assertContains(missing_qty, "Quantity wajib diisi")
        self.assertFalse(MarketingRecommendation.objects.exists())

        saved = self.client.post(
            reverse("dashboard:upcoming_collection_recommend", args=[product.id]),
            {"recommendation": "GO", "recommended_quantity": "120", "rationale": "Strong demand"},
        )
        self.assertRedirects(saved, reverse("dashboard:upcoming_collection_detail", args=[collection.id]))
        recommendation = MarketingRecommendation.objects.get(product=product)
        self.assertEqual(recommendation.recommended_quantity, 120)
        denied = self.client.post(
            reverse("dashboard:upcoming_collection_official_approve", args=[product.id]),
            {"official_decision": "GO", "official_quantity": "100", "official_note": "Approved"},
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.admin)
        official = self.client.post(
            reverse("dashboard:upcoming_collection_official_approve", args=[product.id]),
            {"official_decision": "GO", "official_quantity": "100", "official_note": "Approved"},
        )
        self.assertRedirects(official, reverse("dashboard:upcoming_collection_detail", args=[collection.id]))
        approved = self.client.post(
            reverse("dashboard:upcoming_collection_commercial_approve", args=[collection.id])
        )
        self.assertRedirects(approved, reverse("dashboard:upcoming_collection_detail", args=[collection.id]))
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.COMMERCIAL_APPROVED)
        self.assertEqual(collection.commercial_approved_by, self.admin)

    def test_collection_approval_rejects_incomplete_or_all_drop(self):
        collection = self._collection()
        product = self._product(collection, status=DevelopmentProduct.Status.FINAL_APPROVED)
        collection.status = Collection.Status.MARKETING_REVIEW
        collection.save()
        self.client.force_login(self.admin)

        incomplete = self.client.post(
            reverse("dashboard:upcoming_collection_commercial_approve", args=[collection.id]),
            follow=True,
        )
        self.assertContains(incomplete, "Seluruh Product wajib memiliki rekomendasi Marketing")

        MarketingRecommendation.objects.create(
            product=product,
            recommendation=MarketingRecommendation.Decision.DROP,
            rationale="Weak demand",
            recommended_by=self.marketing,
            recommended_at=timezone.now(),
            official_decision=MarketingRecommendation.Decision.DROP,
            approved_by=self.admin,
            approved_at=timezone.now(),
        )
        all_drop = self.client.post(
            reverse("dashboard:upcoming_collection_commercial_approve", args=[collection.id]),
            follow=True,
        )
        self.assertContains(all_drop, "Minimal satu Product wajib memiliki Official Decision GO")
        collection.refresh_from_db()
        self.assertEqual(collection.status, Collection.Status.MARKETING_REVIEW)
