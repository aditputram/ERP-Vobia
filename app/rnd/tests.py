import tempfile
from io import BytesIO
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject
from reportlab.pdfgen import canvas

from audit.models import AuditEvent
from master_data.models import Product

from .models import (
    Collection,
    DesignAsset,
    DevelopmentProduct,
    DevelopmentProductDocumentRevision,
    MarketingRecommendation,
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
        self.assertEqual(collection.status, Collection.Status.DEVELOPMENT)
        self.assertEqual(collection.products.count(), 2)
        self.assertEqual(collection.products.values("working_code").distinct().count(), 2)
        self.assertTrue(all(product.working_code.startswith("RND-") for product in collection.products.all()))
        self.assertEqual(Product.objects.count(), 0)

    def test_designing_upload_and_approve_only_recommendation(self):
        self.client.force_login(self.rnd_editor)
        uploaded = self.client.post(
            reverse("rnd:designing"),
            {
                "image": SimpleUploadedFile(
                    "draft-flannel.png",
                    b"\x89PNG\r\n\x1a\nlocal design",
                    content_type="image/png",
                )
            },
        )
        self.assertRedirects(uploaded, reverse("rnd:designing"))
        design = DesignAsset.objects.get()
        page = self.client.get(reverse("rnd:designing"))
        self.assertContains(page, "Desain Terbaru")
        self.assertContains(page, "Direkomendasikan untuk Collection Baru")
        self.assertContains(page, design.original_name)
        detail = self.client.get(reverse("rnd:design_detail", args=[design.id]))
        self.assertNotContains(detail, "Rekomendasikan untuk Collection Baru</button>")
        denied = self.client.post(reverse("rnd:design_recommend", args=[design.id]))
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.rnd_approver)
        recommended = self.client.post(reverse("rnd:design_recommend", args=[design.id]))
        self.assertRedirects(recommended, reverse("rnd:design_detail", args=[design.id]))
        design.refresh_from_db()
        self.assertIsNotNone(design.recommended_at)
        self.assertEqual(design.recommended_by, self.rnd_approver)
        self.assertEqual(Collection.objects.count(), 0)
        self.assertEqual(DevelopmentProduct.objects.count(), 0)
        page = self.client.get(reverse("rnd:designing"))
        self.assertContains(page, "Direkomendasikan")

        cancelled = self.client.post(reverse("rnd:design_unrecommend", args=[design.id]))
        self.assertRedirects(cancelled, reverse("rnd:design_detail", args=[design.id]))
        design.refresh_from_db()
        self.assertIsNone(design.recommended_at)

        DesignAsset.objects.filter(pk=design.pk).update(created_at=timezone.now() - timedelta(days=8))
        archive = self.client.get(reverse("rnd:designing"))
        self.assertContains(archive, "Design Lainnya")

        file_response = self.client.get(reverse("rnd:design_file", args=[design.id]))
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response["Cache-Control"], "private, no-store")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("rnd:design_file", args=[design.id])).status_code, 302)

    def test_product_form_is_minimal_and_accepts_private_design_files(self):
        collection = self._collection()
        self.client.force_login(self.rnd_editor)

        page = self.client.get(reverse("rnd:collection_detail", args=[collection.id]))
        self.assertContains(page, "Bill of Material (BOM)")
        self.assertContains(page, "Material")
        self.assertContains(page, "Kebutuhan")
        self.assertContains(page, "EOM / Satuan")
        self.assertContains(page, "+ Tambah Material")
        self.assertContains(page, "Upload Product Cover")
        self.assertContains(page, "Upload MDR")
        self.assertContains(page, "Upload Technical Drawing")
        for removed_label in (
            "Working Code",
            "Product Story",
            "Target Customer",
            "Target Retail Price",
            "Estimated COGS",
            "Final Sample",
            "Notes",
        ):
            self.assertNotContains(page, removed_label)

        response = self.client.post(
            reverse("rnd:collection_detail", args=[collection.id]),
            {
                "name": "Commuter Bag",
                "category": "Bag",
                "status": DevelopmentProduct.Status.CONCEPT,
                "product_cover": SimpleUploadedFile(
                    "cover.png",
                    b"\x89PNG\r\n\x1a\nlocal cover",
                    content_type="image/png",
                ),
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
                "materials-1-material": "Resleting YKK 30 cm",
                "materials-1-requirement": "1",
                "materials-1-eom": "pcs",
            },
        )
        self.assertRedirects(response, reverse("rnd:collection_detail", args=[collection.id]))
        product = collection.products.get(name="Commuter Bag")
        self.assertTrue(product.product_cover.name.startswith("rnd/product_covers/"))
        self.assertTrue(product.mockup.name.startswith("rnd/mockups/"))
        self.assertTrue(product.technical_drawing.name.startswith("rnd/technical_drawings/"))
        self.assertEqual(product.materials.count(), 2)
        canvas = product.materials.get(material="Canvas 12 oz")
        self.assertEqual(canvas.requirement, Decimal("1.2000"))
        self.assertEqual(canvas.eom, "meter")

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

        self.client.logout()
        denied = self.client.get(reverse("rnd:product_file", args=[product.id, "mockup"]))
        self.assertEqual(denied.status_code, 302)

    def test_collection_uses_product_cards_and_draft_opens_one_combined_preview(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR PAGE")
        product.technical_drawing = self._pdf("drawing.pdf", "TECHNICAL DRAWING PAGE")
        product.product_cover = SimpleUploadedFile(
            "cover.png",
            b"\x89PNG\r\n\x1a\nlocal cover",
            content_type="image/png",
        )
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
        self.assertContains(product_page, "Preview MDR dan Technical Drawing")
        self.assertContains(product_page, "Klik area PDF untuk scroll dan mengatur zoom.")
        self.assertContains(product_page, 'scrolling="yes"')
        self.assertContains(product_page, 'tabindex="0"')

        edit_page = self.client.get(f'{reverse("rnd:product_detail", args=[product.id])}?edit=1')
        self.assertContains(edit_page, "Tutup Edit")
        self.assertContains(edit_page, "Simpan Product")

        preview = self.client.get(preview_url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "application/pdf")
        self.assertEqual(preview["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(preview["Cache-Control"], "private, no-store")
        combined = b"".join(preview.streaming_content)
        reader = PdfReader(BytesIO(combined))
        self.assertEqual(len(reader.pages), 2)
        text = " ".join(page.extract_text() for page in reader.pages)
        self.assertIn("MDR PAGE", text)
        self.assertIn("TECHNICAL DRAWING PAGE", text)
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
        self.assertEqual(collection.status, Collection.Status.DRAFT)

    def test_submit_and_superadmin_approval_generate_one_audited_pdf(self):
        collection = self._collection()
        product = self._product(collection)
        product.mockup = self._pdf("mockup.pdf", "MDR PAGE")
        product.technical_drawing = self._pdf("drawing.pdf", "TECHNICAL DRAWING PAGE")
        product.save(update_fields=("mockup", "technical_drawing", "updated_at"))
        product.mockup.open("rb")
        source_mockup = product.mockup.read()
        product.mockup.close()

        self.client.force_login(self.rnd_editor)
        submitted = self.client.post(reverse("rnd:product_submit", args=[product.id]))
        self.assertRedirects(submitted, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.SUBMITTED)
        self.assertIsNotNone(product.submitted_at)
        self.assertEqual(product.submitted_by, self.rnd_editor)
        submitted_pdf = PdfReader(product.submitted_document.path)
        self.assertEqual(len(submitted_pdf.pages), 2)
        submitted_page_texts = [page.extract_text() for page in submitted_pdf.pages]
        submitted_text = " ".join(submitted_page_texts)
        self.assertIn("MDR PAGE", submitted_text)
        self.assertIn("TECHNICAL DRAWING PAGE", submitted_text)
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
        self.assertEqual(product.document_status, DevelopmentProduct.DocumentStatus.APPROVED)
        self.assertEqual(product.status, DevelopmentProduct.Status.SAMPLING)
        self.assertEqual(product.rnd_approved_by, self.admin)
        revision.refresh_from_db()
        self.assertEqual(revision.status, DevelopmentProductDocumentRevision.Status.APPROVED)
        self.assertEqual(revision.approved_document.name, product.approved_document.name)
        final_pdf = PdfReader(product.approved_document.path)
        self.assertEqual(len(final_pdf.pages), 2)
        first_page_text = final_pdf.pages[0].extract_text()
        second_page_text = final_pdf.pages[1].extract_text()
        self.assertNotIn("DIGITAL APPROVED", first_page_text)
        self.assertIn("Aditya Saputra", first_page_text)
        self.assertIn("Aditya Saputra", second_page_text)
        self.assertNotIn("DIGITAL APPROVED", second_page_text)
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

        self.assertEqual(len(submitted_pdf.pages), 2)
        self.assertIn("MDR PAGE", submitted_pdf.pages[0].extract_text())
        self.assertIn("TECH PACK ARRAY PAGE", submitted_pdf.pages[1].extract_text())

        live_preview = self.client.get(
            reverse("rnd:product_revision_file", args=[product.id, 0])
        )
        live_pdf = PdfReader(BytesIO(b"".join(live_preview.streaming_content)))
        self.assertEqual(len(live_pdf.pages), 2)
        self.assertIn("TECH PACK ARRAY PAGE", live_pdf.pages[1].extract_text())

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
        self.assertContains(revision_page, "MDR / Mockup")
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
            "Upload MDR / Mockup baru sesuai permintaan revisi.",
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
        self.assertContains(response, "MDR dan Technical Drawing wajib tersedia")
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
        self.assertContains(blocked, "Seluruh Product wajib Final R&amp;D")
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

    def test_approved_product_moves_through_costing_before_approver_sets_final(self):
        collection = self._collection()
        product = self._product(collection)
        product.document_status = DevelopmentProduct.DocumentStatus.APPROVED
        product.status = DevelopmentProduct.Status.SAMPLING
        product.save(update_fields=("document_status", "status", "updated_at"))

        self.client.force_login(self.rnd_editor)
        costing = self.client.post(reverse("rnd:product_move_to_costing", args=[product.id]))
        self.assertRedirects(costing, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.status, DevelopmentProduct.Status.COSTING)

        denied = self.client.post(reverse("rnd:product_finalize", args=[product.id]))
        self.assertEqual(denied.status_code, 403)
        product.refresh_from_db()
        self.assertEqual(product.status, DevelopmentProduct.Status.COSTING)

        self.client.force_login(self.rnd_approver)
        finalized = self.client.post(reverse("rnd:product_finalize", args=[product.id]))
        self.assertRedirects(finalized, reverse("rnd:product_detail", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(product.status, DevelopmentProduct.Status.FINAL_APPROVED)

    def test_product_cannot_skip_costing_or_finalize_without_approved_document(self):
        collection = self._collection()
        product = self._product(collection)
        self.client.force_login(self.rnd_approver)

        skipped = self.client.post(reverse("rnd:product_finalize", args=[product.id]), follow=True)
        self.assertContains(skipped, "Dokumen Product wajib Approved")
        product.refresh_from_db()
        self.assertEqual(product.status, DevelopmentProduct.Status.CONCEPT)

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
