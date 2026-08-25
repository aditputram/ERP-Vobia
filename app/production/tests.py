from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from audit.models import AuditEvent
from inventory.models import FIFOLayer, InboundReceipt, InventoryMovement, QCFollowUp, QCInspection
from inventory.services.fifo import record_inbound, record_qc
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Supplier, Warehouse
from purchasing.models import PurchaseOrder, PurchaseOrderLine

from .models import ProductionActivity, ProductionCogsFinalization, ProductionPlan, ProductionStage, ProductionTrial
from .forms import ProductionStageUpdateForm
from .services import (
    append_trial_note,
    approve_production_cogs_finalization,
    decide_trial,
    ensure_production_order,
    production_snapshot,
    production_cogs_finalization_card,
    save_production_plan,
    save_trial_target,
    start_trial,
    submit_trial,
    submit_delivery_activity_batch,
    submit_cmt_activity_batch,
    submit_production_activity,
    update_stage,
)


class ProductionWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="production", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="KNIT", name="Knitwear")
        product = Product.objects.create(code="P-1", parent_sku="PARENT-1", name="Jersey", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black", color="Black")
        self.sku = SKU.objects.create(
            sku="SKU-PROD-1",
            product_variant=variant,
            size="L",
            current_retail_price=Decimal("299000"),
            current_master_cogs=Decimal("120000"),
        )
        self.supplier = Supplier.objects.create(code="SUP-PROD", name="Vendor Production")
        self.po = PurchaseOrder.objects.create(
            po_number="PO-VOB-08/26-999",
            sequence=999,
            issue_month=date(2026, 8, 1),
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            required_arrival=date(2026, 9, 1),
            status=PurchaseOrder.Status.RELEASED,
            source=PurchaseOrder.Source.MANUAL_NEW_PRODUCT,
            created_by=self.user,
            released_by=self.user,
            released_at=timezone.now(),
        )
        self.line = PurchaseOrderLine.objects.create(
            po=self.po,
            sku=self.sku,
            ordered_qty=Decimal("100"),
            cogs_snapshot=Decimal("125000"),
        )
        self.production_order = ensure_production_order(self.po, actor=self.user)

    def _complete_stage(self, stage_code):
        completed_qty = Decimal("100") if stage_code in {
            ProductionStage.Stage.CUT,
            ProductionStage.Stage.MAKE,
            ProductionStage.Stage.TRIM,
        } else None
        return update_stage(
            production_order=self.production_order,
            stage_code=stage_code,
            status=ProductionStage.Status.COMPLETE,
            target_start_date=date(2026, 8, 1),
            target_end_date=date(2026, 8, 20),
            actual_start_date=date(2026, 8, 1),
            actual_end_date=date(2026, 8, 20),
            material_arrival_date=(
                date(2026, 8, 8) if stage_code == ProductionStage.Stage.MATERIAL_PURCHASE else None
            ),
            progress_percent=100,
            completed_qty=completed_qty,
            notes="UAT complete",
            actor=self.user,
        )

    def _approve_trial(self):
        target_trial_date = date(2026, 8, 9)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=target_trial_date,
            actor=self.user,
        )
        append_trial_note(
            production_order=self.production_order,
            note="Sample size dan workmanship sudah sesuai.",
            actor=self.user,
        )
        trial = submit_trial(
            production_order=self.production_order,
            trial_date=date(2026, 8, 10),
            actor=self.user,
        )
        return decide_trial(
            trial=trial,
            decision=ProductionTrial.Status.APPROVED,
            decision_notes="Approved untuk mass production.",
            actor=self.user,
        )

    def _activate_plan(self):
        values = {
            "target_material_purchase_date": date(2026, 8, 23),
            "target_trial_date": date(2026, 8, 24),
            "target_cut_start_date": date(2026, 8, 25),
            "target_cut_end_date": date(2026, 8, 26),
            "target_make_start_date": date(2026, 8, 26),
            "target_make_end_date": date(2026, 8, 28),
            "target_trim_start_date": date(2026, 8, 28),
            "target_trim_end_date": date(2026, 8, 29),
            "target_qc_start_date": date(2026, 8, 29),
            "target_qc_end_date": date(2026, 8, 30),
            "target_inbound_date": date(2026, 9, 1),
            "notes": "UAT plan",
        }
        return save_production_plan(
            production_order=self.production_order,
            values=values,
            activate=True,
            actor=self.user,
        )

    def test_gate_prevents_skipping_material_trial_and_cmt(self):
        with self.assertRaisesMessage(ValidationError, "setelah Pembelian Material selesai"):
            self._complete_stage(ProductionStage.Stage.CUT)
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        with self.assertRaisesMessage(ValidationError, "Trial Produksi di-approve"):
            self._complete_stage(ProductionStage.Stage.CUT)
        self._approve_trial()
        self._complete_stage(ProductionStage.Stage.CUT)
        self._complete_stage(ProductionStage.Stage.MAKE)
        self._complete_stage(ProductionStage.Stage.TRIM)
        qc = record_qc(
            po_line=self.line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 22, 10, 0)),
            qty_inspected=100,
            qty_passed=98,
            qty_failed=2,
            disposition=QCInspection.Disposition.REWORK,
            actor=self.user,
        )
        self.assertEqual(qc.qty_passed, Decimal("98"))
        self.assertTrue(ProductionActivity.objects.filter(action="production_trial_approved").exists())

    def test_material_arrival_date_is_required_before_material_stage_complete(self):
        with self.assertRaisesMessage(ValidationError, "Tanggal material datang"):
            update_stage(
                production_order=self.production_order,
                stage_code=ProductionStage.Stage.MATERIAL_PURCHASE,
                status=ProductionStage.Status.COMPLETE,
                progress_percent=100,
                actor=self.user,
            )

    def test_material_status_choices_use_business_labels(self):
        stage = self.production_order.stages.get(stage=ProductionStage.Stage.MATERIAL_PURCHASE)
        form = ProductionStageUpdateForm(instance=stage, prefix="material_purchase")

        self.assertEqual(
            list(form.fields["status"].choices),
            [
                (ProductionStage.Status.NOT_STARTED, "Belum di beli"),
                (ProductionStage.Status.IN_PROGRESS, "Menunggu ketersediaan Material"),
                (ProductionStage.Status.COMPLETE, "Material siap diproses"),
            ],
        )
        self.assertEqual(stage.operational_status_display, "Belum di beli")
        self.assertEqual(form.fields["target_start_date"].label, "Target Pembelian Material")
        self.assertEqual(form.fields["actual_start_date"].label, "Aktual Pembelian Material")
        self.assertNotIn("target_end_date", form.fields)
        self.assertNotIn("actual_end_date", form.fields)
        self.assertNotIn("progress_percent", form.fields)

    def test_material_arrival_date_automatically_marks_material_ready(self):
        arrival_date = date(2026, 8, 12)
        stage = update_stage(
            production_order=self.production_order,
            stage_code=ProductionStage.Stage.MATERIAL_PURCHASE,
            status=ProductionStage.Status.NOT_STARTED,
            material_arrival_date=arrival_date,
            progress_percent=0,
            notes="Material sudah tiba.",
            actor=self.user,
        )

        self.assertEqual(stage.status, ProductionStage.Status.COMPLETE)
        self.assertEqual(stage.operational_status_display, "Material siap diproses")
        self.assertEqual(
            production_snapshot(self.production_order)["material_status_display"],
            "Material siap diproses",
        )
        self.assertEqual(stage.progress_percent, 100)
        self.assertIsNone(stage.actual_end_date)
        activity = ProductionActivity.objects.filter(action="production_stage_updated").latest("occurred_at")
        self.assertIn("Material siap diproses", activity.description)

    def test_material_form_without_progress_field_accepts_arrival_date(self):
        stage = self.production_order.stages.get(stage=ProductionStage.Stage.MATERIAL_PURCHASE)
        form = ProductionStageUpdateForm(
            {
                "material_purchase-status": ProductionStage.Status.NOT_STARTED,
                "material_purchase-target_start_date": "2026-08-05",
                "material_purchase-actual_start_date": "2026-08-06",
                "material_purchase-material_arrival_date": "2026-08-12",
                "material_purchase-notes": "Material sudah tiba.",
            },
            instance=stage,
            prefix="material_purchase",
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("progress_percent", form.cleaned_data)
        self.assertEqual(form.cleaned_data["status"], ProductionStage.Status.COMPLETE)
        self.assertEqual(form.instance.progress_percent, 100)

    def test_saved_production_dates_render_as_valid_html_date_values(self):
        saved_date = date(2026, 8, 23)
        stage = self.production_order.stages.get(stage=ProductionStage.Stage.MATERIAL_PURCHASE)
        stage.target_start_date = saved_date
        stage.actual_start_date = saved_date
        stage.material_arrival_date = saved_date
        stage.save(
            update_fields=(
                "target_start_date",
                "actual_start_date",
                "material_arrival_date",
                "updated_at",
            )
        )

        form = ProductionStageUpdateForm(instance=stage, prefix="material_purchase")

        for field_name in (
            "target_start_date",
            "actual_start_date",
            "material_arrival_date",
        ):
            self.assertIn('value="2026-08-23"', form[field_name].as_widget())

    def test_cmt_qty_input_is_blank_until_progress_is_recorded(self):
        cut_stage = self.production_order.stages.get(stage=ProductionStage.Stage.CUT)
        form = ProductionStageUpdateForm(instance=cut_stage, prefix="cut")

        self.assertEqual(form["completed_qty"].value(), "")
        self.assertNotIn('value="0.0000"', form["completed_qty"].as_widget())

        cut_stage.completed_qty = Decimal("12")
        cut_stage.save(update_fields=("completed_qty", "updated_at"))
        saved_form = ProductionStageUpdateForm(instance=cut_stage, prefix="cut")

        self.assertEqual(saved_form["completed_qty"].value(), Decimal("12"))
        self.assertIn('value="12"', saved_form["completed_qty"].as_widget())

    def test_material_stage_uses_single_target_and_actual_purchase_dates(self):
        target_purchase = date(2026, 8, 5)
        actual_purchase = date(2026, 8, 6)
        stage = update_stage(
            production_order=self.production_order,
            stage_code=ProductionStage.Stage.MATERIAL_PURCHASE,
            status=ProductionStage.Status.IN_PROGRESS,
            target_start_date=target_purchase,
            target_end_date=date(2026, 8, 20),
            actual_start_date=actual_purchase,
            actual_end_date=date(2026, 8, 21),
            progress_percent=40,
            actor=self.user,
        )

        self.assertEqual(stage.target_start_date, target_purchase)
        self.assertEqual(stage.actual_start_date, actual_purchase)
        self.assertIsNone(stage.target_end_date)
        self.assertIsNone(stage.actual_end_date)
        self.assertEqual(stage.progress_percent, 0)

    def test_cmt_quantity_flows_from_cut_to_make_to_trim(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        self._approve_trial()

        cut = update_stage(
            production_order=self.production_order,
            stage_code=ProductionStage.Stage.CUT,
            completed_qty=Decimal("40"),
            notes="40 pcs sudah dipotong",
            actor=self.user,
        )
        make = update_stage(
            production_order=self.production_order,
            stage_code=ProductionStage.Stage.MAKE,
            completed_qty=Decimal("20"),
            notes="20 pcs sudah dijahit",
            actor=self.user,
        )
        trim = update_stage(
            production_order=self.production_order,
            stage_code=ProductionStage.Stage.TRIM,
            completed_qty=Decimal("8"),
            notes="8 pcs sudah finishing",
            actor=self.user,
        )

        snapshot = production_snapshot(self.production_order)
        self.assertEqual(cut.completed_qty, Decimal("40"))
        self.assertEqual(make.completed_qty, Decimal("20"))
        self.assertEqual(trim.completed_qty, Decimal("8"))
        self.assertEqual(snapshot["cmt_quantities"][ProductionStage.Stage.MAKE]["available_qty"], Decimal("40"))
        self.assertEqual(snapshot["cmt_quantities"][ProductionStage.Stage.MAKE]["remaining_qty"], Decimal("20"))
        self.assertEqual(snapshot["cmt_quantities"][ProductionStage.Stage.TRIM]["available_qty"], Decimal("20"))
        self.assertEqual(snapshot["cmt_quantities"][ProductionStage.Stage.TRIM]["remaining_qty"], Decimal("12"))
        self.assertTrue(snapshot["qc_open"])

        with self.assertRaisesMessage(ValidationError, "Qty sudah di-Cut (40 pcs)"):
            update_stage(
                production_order=self.production_order,
                stage_code=ProductionStage.Stage.MAKE,
                completed_qty=Decimal("41"),
                actor=self.user,
            )

    def test_qc_is_blocked_before_any_trim_output(self):
        with self.assertRaisesMessage(ValidationError, "output Trim"):
            record_qc(
                po_line=self.line,
                inspected_at=timezone.make_aware(datetime(2026, 8, 22, 10, 0)),
                qty_inspected=10,
                qty_passed=10,
                qty_failed=0,
                actor=self.user,
            )

    def test_revision_history_is_preserved(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )
        append_trial_note(
            production_order=self.production_order,
            note="Trial pertama.",
            actor=self.user,
        )
        trial = submit_trial(
            production_order=self.production_order,
            trial_date=date(2026, 8, 10),
            actor=self.user,
        )
        decide_trial(
            trial=trial,
            decision=ProductionTrial.Status.REVISION_REQUIRED,
            decision_notes="Ukuran perlu diperbaiki.",
            actor=self.user,
        )
        revision_two = start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 16),
            actor=self.user,
        )
        self.assertEqual(revision_two.revision, 2)
        self.assertEqual(self.production_order.trials.count(), 2)

    def test_trial_requires_target_date(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)

        with self.assertRaisesMessage(ValidationError, "Target tanggal Trial Production"):
            start_trial(
                production_order=self.production_order,
                target_trial_date=None,
                actor=self.user,
            )

    def test_trial_target_can_be_saved_separately_and_is_audited(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )

        saved_trial = save_trial_target(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 12),
            actor=self.user,
        )

        self.assertEqual(saved_trial.pk, trial.pk)
        self.assertEqual(saved_trial.target_trial_date, date(2026, 8, 12))
        self.assertTrue(
            ProductionActivity.objects.filter(
                production_order=self.production_order,
                action="production_trial_target_saved",
            ).exists()
        )

    def test_trial_submit_is_blocked_when_saved_target_is_missing(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )
        ProductionTrial.objects.filter(pk=trial.pk).update(target_trial_date=None)

        with self.assertRaisesMessage(ValidationError, "Simpan Target Tanggal Trial Production"):
            submit_trial(
                production_order=self.production_order,
                trial_date=date(2026, 8, 10),
                actor=self.user,
            )

    def test_trial_notes_append_in_numbered_order_and_are_audited(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )

        append_trial_note(
            production_order=self.production_order,
            note="Masih harus direvisi bagian kantongnya.",
            actor=self.user,
        )
        trial = append_trial_note(
            production_order=self.production_order,
            note="Sudah direvisi bagian kantongnya dan sesuai permintaan.",
            actor=self.user,
        )

        self.assertEqual(
            trial.notes,
            "1. Masih harus direvisi bagian kantongnya.\n"
            "2. Sudah direvisi bagian kantongnya dan sesuai permintaan.",
        )
        self.assertEqual(
            ProductionActivity.objects.filter(
                production_order=self.production_order,
                action="production_trial_note_appended",
            ).count(),
            2,
        )

    def test_trial_submit_only_requires_saved_target_and_approved_date(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )

        trial = submit_trial(
            production_order=self.production_order,
            trial_date=date(2026, 8, 10),
            actor=self.user,
        )

        self.assertEqual(trial.status, ProductionTrial.Status.WAITING_APPROVAL)
        self.assertEqual(trial.notes, "")

    def test_trial_card_lists_all_po_products_and_monitoring_dates(self):
        base_product = self.sku.product_variant.product
        second_product = Product.objects.create(
            code="P-2",
            parent_sku="PARENT-2",
            name="Crewneck Trial",
            status=base_product.status,
            category=base_product.category,
        )
        second_variant = ProductVariant.objects.create(
            product=second_product,
            name="Navy",
            color="Navy",
        )
        second_sku = SKU.objects.create(
            sku="SKU-PROD-2",
            product_variant=second_variant,
            size="XL",
            current_retail_price=Decimal("329000"),
            current_master_cogs=Decimal("130000"),
        )
        PurchaseOrderLine.objects.create(
            po=self.po,
            sku=second_sku,
            ordered_qty=Decimal("40"),
            cogs_snapshot=Decimal("130000"),
        )
        base_size_m = SKU.objects.create(
            sku="SKU-PROD-1-M",
            product_variant=self.sku.product_variant,
            size="M",
            current_retail_price=Decimal("299000"),
            current_master_cogs=Decimal("120000"),
        )
        PurchaseOrderLine.objects.create(
            po=self.po,
            sku=base_size_m,
            ordered_qty=Decimal("25"),
            cogs_snapshot=Decimal("125000"),
        )
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        target_trial_date = date(2026, 8, 9)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=target_trial_date,
            actor=self.user,
        )
        append_trial_note(
            production_order=self.production_order,
            note="Seluruh produk dan size lolos trial.",
            actor=self.user,
        )
        trial = submit_trial(
            production_order=self.production_order,
            trial_date=date(2026, 8, 10),
            actor=self.user,
        )
        trial = decide_trial(
            trial=trial,
            decision=ProductionTrial.Status.APPROVED,
            decision_notes="Approved untuk produksi massal.",
            actor=self.user,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("production:detail", args=[self.production_order.id]), follow=True)

        self.assertEqual(response.context["trial_product_count"], 2)
        self.assertEqual(len(response.context["trial_po_lines"]), 3)
        self.assertEqual(len(response.context["trial_products"]), 2)
        parent_one = next(row for row in response.context["trial_products"] if row["parent_sku"] == "PARENT-1")
        self.assertEqual(parent_one["size_display"], "M, L")
        self.assertEqual(parent_one["total_qty"], Decimal("125"))
        self.assertEqual([row["sku"] for row in parent_one["sku_lines"]], ["SKU-PROD-1-M", "SKU-PROD-1"])
        self.assertEqual(response.context["latest_trial"].target_trial_date, target_trial_date)
        self.assertIsNotNone(response.context["latest_trial"].decided_at)
        self.assertContains(response, "PARENT-1")
        self.assertContains(response, "PARENT-2")
        self.assertContains(response, "Crewneck Trial")
        self.assertNotContains(response, "<th>Variant</th>")
        self.assertContains(response, "<thead><tr><th>Parent SKU</th><th>Product</th><th>Size</th><th>Quantity</th></tr></thead>", html=True)
        self.assertContains(response, "<thead><tr><th>SKU</th><th>Size</th><th>Qty</th></tr></thead>", count=2, html=True)
        self.assertContains(response, 'data-po-product-toggle=', count=4)
        self.assertContains(response, "125 pcs")
        self.assertContains(response, "SKU-PROD-1-M")
        self.assertContains(response, "Trial Production")
        self.assertContains(response, "R1 · Approved")
        self.assertContains(response, "10 Agu 2026")
        self.assertContains(response, "Seluruh produk dan size lolos trial.")

    def test_active_trial_has_separate_target_save_and_approval_gate(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("production:detail", args=[self.production_order.id]))

        self.assertContains(response, "READ ONLY MONITORING")
        self.assertContains(response, "Entry Production Activity")
        self.assertNotContains(response, "Save Target Trial Production")

    def test_save_trial_note_post_appends_and_redirects_to_trial_card(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )
        self.client.force_login(self.user)
        detail_url = reverse("production:detail", args=[self.production_order.id])

        response = self.client.post(
            detail_url,
            {
                "form_name": "save_trial_note",
                "note": "Masih harus direvisi bagian kantongnya.",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('production:activity')}?production_order={self.production_order.id}",
            fetch_redirect_response=False,
        )
        trial.refresh_from_db()
        self.assertEqual(trial.notes, "")

    def test_save_trial_target_post_persists_and_redirects_to_trial_card(self):
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        trial = start_trial(
            production_order=self.production_order,
            target_trial_date=date(2026, 8, 9),
            actor=self.user,
        )
        self.client.force_login(self.user)
        detail_url = reverse("production:detail", args=[self.production_order.id])

        response = self.client.post(
            detail_url,
            {
                "form_name": "save_trial_target",
                "target_trial_date": "2026-08-12",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('production:activity')}?production_order={self.production_order.id}",
            fetch_redirect_response=False,
        )
        trial.refresh_from_db()
        self.assertEqual(trial.target_trial_date, date(2026, 8, 9))

    def test_production_pages_render(self):
        self.client.force_login(self.user)
        for url in (
            reverse("production:dashboard"),
            reverse("production:planning"),
            reverse("production:monitoring"),
            reverse("production:activity"),
            reverse("production:detail", args=[self.production_order.id]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertContains(self.client.get(reverse("production:dashboard")), "Production Dashboard")

    def test_activity_cancel_returns_to_initial_unselected_view(self):
        self._activate_plan()
        self.client.force_login(self.user)
        activity_url = reverse("production:activity")

        selected_response = self.client.get(
            activity_url,
            {"production_order": self.production_order.id},
        )
        self.assertContains(selected_response, "Cancel")
        self.assertContains(selected_response, f'href="{activity_url}"')

        initial_response = self.client.get(activity_url)
        self.assertIsNone(initial_response.context["selected"])
        self.assertNotContains(initial_response, "Catat aktivitas")
        self.assertNotContains(initial_response, ">Cancel</a>")

    def test_activity_form_lists_every_po_sku_and_submits_line_level_cut(self):
        base_product = self.sku.product_variant.product
        second_product = Product.objects.create(
            code="P-2",
            parent_sku="PARENT-2",
            name="Crewneck Production",
            status=base_product.status,
            category=base_product.category,
        )
        second_variant = ProductVariant.objects.create(
            product=second_product,
            name="Navy",
            color="Navy",
        )
        second_sku = SKU.objects.create(
            sku="SKU-PROD-2",
            product_variant=second_variant,
            size="XL",
            current_retail_price=Decimal("329000"),
            current_master_cogs=Decimal("130000"),
        )
        second_line = PurchaseOrderLine.objects.create(
            po=self.po,
            sku=second_sku,
            ordered_qty=Decimal("50"),
            cogs_snapshot=Decimal("130000"),
        )
        self._activate_plan()
        for activity_type, activity_date in (
            (ProductionActivity.ActivityType.MATERIAL_PURCHASE, date(2026, 8, 23)),
            (ProductionActivity.ActivityType.MATERIAL_ARRIVAL, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_SUBMIT, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_APPROVE, date(2026, 8, 24)),
        ):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=activity_type,
                activity_date=activity_date,
                actor=self.user,
            )
        self.client.force_login(self.user)
        activity_url = reverse("production:activity")

        page = self.client.get(activity_url, {"production_order": self.production_order.id})
        self.assertContains(page, "SKU-PROD-1")
        self.assertContains(page, "SKU-PROD-2")
        self.assertContains(page, "Jersey")
        self.assertContains(page, "Crewneck Production")
        self.assertContains(page, "Size")
        self.assertContains(page, ">L</strong>", html=False)
        self.assertContains(page, ">XL</strong>", html=False)
        self.assertContains(
            page,
            '<thead><tr><th>Parent SKU</th><th>Product</th><th>Size</th><th data-cmt-quantity-label>Qty Activity</th><th data-cmt-available-label>Sisa Parent SKU</th><th>PO Qty SKU</th></tr></thead>',
            html=True,
        )
        self.assertContains(page, "Qty Cut")
        self.assertContains(page, "Sisa Parent SKU")
        self.assertContains(page, "PO Qty SKU")
        self.assertNotContains(page, "PO Qty:")
        self.assertContains(page, 'data-cut="Tanpa batas"', count=2)

        response = self.client.post(
            activity_url,
            {
                "production_order": self.production_order.id,
                "activity_type": ProductionActivity.ActivityType.CUT,
                "activity_date": "2026-08-25",
                f"line_quantity_{self.line.id}": "30",
                f"line_quantity_{second_line.id}": "20",
                "notes": "Cut parsial per SKU.",
            },
        )

        self.assertRedirects(
            response,
            f"{activity_url}?production_order={self.production_order.id}",
            fetch_redirect_response=False,
        )
        cut_rows = ProductionActivity.objects.filter(
            production_order=self.production_order,
            entry_kind=ProductionActivity.EntryKind.ACTIVITY,
            activity_type=ProductionActivity.ActivityType.CUT,
        )
        self.assertEqual(cut_rows.count(), 2)
        self.assertSetEqual(set(cut_rows.values_list("po_line_id", flat=True)), {self.line.id, second_line.id})
        self.production_order.stages.get(stage=ProductionStage.Stage.CUT).refresh_from_db()
        self.assertEqual(
            self.production_order.stages.get(stage=ProductionStage.Stage.CUT).completed_qty,
            Decimal("50"),
        )
        updated_page = self.client.get(activity_url, {"production_order": self.production_order.id})
        self.assertContains(updated_page, 'data-cut="Tanpa batas"', count=2)
        self.assertContains(updated_page, 'data-make="30"')
        self.assertContains(updated_page, 'data-trim="0"')

    def test_make_qty_cannot_exceed_cut_for_same_sku(self):
        self._activate_plan()
        for activity_type, activity_date in (
            (ProductionActivity.ActivityType.MATERIAL_PURCHASE, date(2026, 8, 23)),
            (ProductionActivity.ActivityType.MATERIAL_ARRIVAL, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_SUBMIT, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_APPROVE, date(2026, 8, 24)),
        ):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=activity_type,
                activity_date=activity_date,
                actor=self.user,
            )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.CUT,
            activity_date=date(2026, 8, 25),
            quantity=Decimal("20"),
            po_line=self.line,
            actor=self.user,
        )

        with self.assertRaisesMessage(ValidationError, "tidak boleh melebihi Qty Cut"):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=ProductionActivity.ActivityType.MAKE,
                activity_date=date(2026, 8, 25),
                quantity=Decimal("21"),
                po_line=self.line,
                actor=self.user,
            )

    def test_monitoring_detail_is_read_only(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("production:detail", args=[self.production_order.id]))

        self.assertContains(response, "READ ONLY MONITORING")
        self.assertContains(response, "Tidak dapat diedit di halaman ini")
        self.assertNotContains(response, "Simpan Cut")
        self.assertNotContains(response, "Submit Approval Trial Production")

    def test_material_save_redirects_back_to_material_card(self):
        self.client.force_login(self.user)
        detail_url = reverse("production:detail", args=[self.production_order.id])

        response = self.client.post(
            detail_url,
            {
                "form_name": "stage",
                "stage": ProductionStage.Stage.MATERIAL_PURCHASE,
                "material_purchase-status": ProductionStage.Status.IN_PROGRESS,
                "material_purchase-target_start_date": "2026-08-23",
                "material_purchase-actual_start_date": "2026-08-23",
                "material_purchase-material_arrival_date": "",
                "material_purchase-notes": "Menunggu material tersedia.",
            },
        )

        self.assertRedirects(response, f"{reverse('production:activity')}?production_order={self.production_order.id}", fetch_redirect_response=False)

    def test_only_active_plan_enters_monitoring(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("production:monitoring"))
        self.assertNotContains(response, self.po.po_number)

        plan = self._activate_plan()
        self.assertEqual(plan.status, ProductionPlan.Status.ACTIVE)
        response = self.client.get(reverse("production:monitoring"))
        self.assertContains(response, self.po.po_number)

    def test_qc_follow_up_is_monitored_then_recorded_in_production_activity(self):
        self._activate_plan()
        ProductionStage.objects.filter(
            production_order=self.production_order,
            stage=ProductionStage.Stage.TRIM,
        ).update(completed_qty=Decimal("4"))
        inspection = record_qc(
            po_line=self.line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 25, 10, 0)),
            qty_inspected=2,
            qty_passed=1,
            qty_failed=1,
            disposition=QCInspection.Disposition.REWORK,
            actor=self.user,
        )
        follow_up = QCFollowUp.objects.get(source_inspection=inspection)
        record_qc(
            po_line=self.line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 25, 11, 0)),
            qty_inspected=2,
            qty_passed=0,
            qty_failed=2,
            disposition=QCInspection.Disposition.REJECTED,
            notes="Noda permanen.",
            actor=self.user,
        )
        self.client.force_login(self.user)

        monitoring = self.client.get(reverse("production:monitoring"))
        self.assertContains(monitoring, "Barang yang perlu ditindaklanjuti")
        self.assertContains(monitoring, self.sku.sku)
        self.assertContains(monitoring, "Catat Rework")
        self.assertNotContains(monitoring, 'href="/production/qc-follow-up/"')

        detail = self.client.get(reverse("production:detail", args=[self.production_order.id]))
        self.assertContains(detail, "06B · QC FOLLOW-UP")
        self.assertContains(detail, self.sku.sku)
        self.assertContains(detail, "Catat Rework")
        self.assertContains(detail, "Qty QC Passed")
        self.assertContains(detail, "Qty Rework &amp; Re-QC")
        self.assertContains(detail, "Qty Rejected")
        self.assertEqual(detail.context["snapshot"]["passed_qty"], Decimal("1"))
        self.assertEqual(detail.context["snapshot"]["rework_re_qc_qty"], Decimal("1"))
        self.assertEqual(detail.context["snapshot"]["rejected_qty"], Decimal("2"))

        rejected_goods = self.client.get(reverse("production:rejected_goods"))
        self.assertContains(rejected_goods, "Rejected Goods")
        self.assertContains(rejected_goods, self.po.po_number)
        self.assertContains(rejected_goods, self.sku.sku)
        self.assertContains(rejected_goods, "Noda permanen.")
        self.assertContains(rejected_goods, "Belum Dikirim")
        self.assertEqual(rejected_goods.context["total_rejected_qty"], Decimal("2"))
        self.assertNotContains(rejected_goods, "Tindakan")
        self.assertNotContains(rejected_goods, "Mulai Pengiriman")

        activity_url = reverse("production:activity")
        activity = self.client.get(activity_url, {"production_order": self.production_order.id})
        self.assertContains(activity, "Rework dan pemeriksaan ulang")
        self.assertContains(activity, "Catat Rework Selesai")

        response = self.client.post(
            activity_url,
            {
                "form_name": "qc_follow_up",
                "production_order": str(self.production_order.id),
                "follow_up": str(follow_up.id),
                "action": "complete_rework",
                "follow_up-activity_date": "2026-08-26",
                "follow_up-notes": "Jahitan sudah diperbaiki.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("action=re_qc", response["Location"])
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, QCFollowUp.Status.READY_RE_QC)

        re_qc_page = self.client.get(response["Location"])
        self.assertContains(re_qc_page, "Catat Hasil Re-QC")
        response = self.client.post(
            activity_url,
            {
                "form_name": "qc_follow_up",
                "production_order": str(self.production_order.id),
                "follow_up": str(follow_up.id),
                "action": "re_qc",
                "follow_up-activity_date": "2026-08-27",
                "follow_up-qty_passed": "1",
                "follow_up-failed_disposition": "",
                "follow_up-notes": "Lolos pemeriksaan ulang.",
            },
        )
        self.assertEqual(response.status_code, 302)
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, QCFollowUp.Status.RESOLVED)
        self.assertEqual(follow_up.resolved_passed_qty, Decimal("1"))
        updated_snapshot = production_snapshot(self.production_order)
        self.assertEqual(updated_snapshot["passed_qty"], Decimal("2"))
        self.assertEqual(updated_snapshot["rework_re_qc_qty"], Decimal("0"))
        self.assertEqual(updated_snapshot["rejected_qty"], Decimal("2"))
        updated_detail = self.client.get(reverse("production:detail", args=[self.production_order.id]))
        self.assertContains(updated_detail, "Rework Selesai")
        self.assertContains(updated_detail, "Jahitan sudah diperbaiki.")
        self.assertContains(updated_detail, "Re-QC")

    def test_rejected_goods_delivery_is_received_without_stock_or_fifo(self):
        self._activate_plan()
        self._complete_stage(ProductionStage.Stage.MATERIAL_PURCHASE)
        self._approve_trial()
        ProductionStage.objects.filter(
            production_order=self.production_order,
            stage=ProductionStage.Stage.TRIM,
        ).update(completed_qty=Decimal("2"))
        inspection = record_qc(
            po_line=self.line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 25, 10, 0)),
            qty_inspected=2,
            qty_passed=0,
            qty_failed=2,
            disposition=QCInspection.Disposition.REJECTED,
            notes="Reject permanen.",
            actor=self.user,
        )
        follow_up = QCFollowUp.objects.get(source_inspection=inspection)
        self.client.force_login(self.user)
        activity_url = reverse("production:activity")
        activity_page = self.client.get(activity_url, {"production_order": self.production_order.id})
        self.assertContains(activity_page, "Deliver Rejected Goods")
        self.assertContains(activity_page, 'data-rejected_warehouse_delivery="2"')

        response = self.client.post(
            activity_url,
            {
                "production_order": str(self.production_order.id),
                "activity_type": ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY,
                "activity_date": "2026-08-27",
                f"line_quantity_{self.line.id}": "2",
                "notes": "Kirim barang reject.",
            },
        )
        self.assertEqual(response.status_code, 302)
        delivery = ProductionActivity.objects.get(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY,
        )
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.delivery_status, QCFollowUp.DeliveryStatus.IN_TRANSIT)
        self.assertEqual(follow_up.delivery_activity, delivery)

        inbound_url = reverse("inventory:inbound")
        inbound_page = self.client.get(inbound_url)
        self.assertContains(inbound_page, "Rejected Goods")
        self.assertContains(inbound_page, delivery.delivery_order.number)
        movement_count = InventoryMovement.objects.count()
        warehouse = Warehouse.objects.get(code="REJECT")
        received = self.client.post(
            inbound_url,
            {
                "form_name": "delivery_receive",
                "delivery_activity": str(delivery.id),
                "inbound_date": "2026-08-27",
                "received_qty": "2",
                "warehouse": str(warehouse.id),
                "notes": "Reject diterima fisik.",
            },
        )
        self.assertEqual(received.status_code, 302)
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.delivery_status, QCFollowUp.DeliveryStatus.INBOUND)
        self.assertEqual(follow_up.received_date, date(2026, 8, 27))
        self.assertEqual(follow_up.received_warehouse, warehouse)
        self.assertEqual(InventoryMovement.objects.count(), movement_count + 1)
        rejected_movement = InventoryMovement.objects.get(
            movement_type=InventoryMovement.MovementType.REJECTED_IN,
            warehouse=warehouse,
        )
        self.assertEqual(rejected_movement.quantity, Decimal("2"))
        self.assertEqual(rejected_movement.allocated_cost, Decimal("0"))
        self.assertFalse(InboundReceipt.objects.filter(delivery_activity=delivery).exists())
        empty_sku = SKU.objects.create(
            sku="SKU-WITHOUT-REJECT",
            product_variant=self.sku.product_variant,
            size="XL",
            current_retail_price=Decimal("299000"),
            current_master_cogs=Decimal("125000"),
        )
        summary = self.client.get(
            reverse("inventory:overview"),
            {"warehouse": warehouse.id, "as_of_date": "2026-08-27"},
        )
        summary_row = next(row for row in summary.context["balances"] if row["sku"].id == self.sku.id)
        self.assertNotIn(empty_sku.id, {row["sku"].id for row in summary.context["balances"]})
        self.assertEqual(summary_row["balance"], Decimal("2"))
        self.assertEqual(summary_row["fifo_qty"], Decimal("0"))
        self.assertEqual(summary_row["stock_status"], "REJECTED")
        turnover = self.client.get(reverse("inventory:turnover"), {"warehouse": warehouse.id})
        self.assertContains(turnover, "Rejected Goods In")
        self.assertContains(turnover, "Reject Warehouse")

    def test_active_plan_leaves_planning_selector_and_opens_revision_mode(self):
        self.client.force_login(self.user)
        self._activate_plan()

        response = self.client.get(
            reverse("production:planning"),
            {"production_order": self.production_order.id},
        )

        self.assertNotIn(self.production_order, list(response.context["planning_rows"]))
        self.assertContains(response, "Revisi Production Plan")
        self.assertContains(response, "Simpan Revisi Production Plan")
        self.assertNotContains(response, "Aktifkan Production Plan")
        self.assertNotContains(response, ">Simpan Draft<")

    def test_active_plan_cannot_be_activated_twice(self):
        self._activate_plan()

        with self.assertRaisesMessage(ValidationError, "sudah Active"):
            save_production_plan(
                production_order=self.production_order,
                values={"notes": "Percobaan aktivasi kedua"},
                activate=True,
                actor=self.user,
            )

    def test_active_plan_revision_requires_reason_and_uses_revision_audit(self):
        self._activate_plan()

        with self.assertRaisesMessage(ValidationError, "Alasan perubahan wajib"):
            save_production_plan(
                production_order=self.production_order,
                values={"notes": "Target direvisi"},
                activate=False,
                actor=self.user,
            )

        plan = save_production_plan(
            production_order=self.production_order,
            values={"notes": "Target direvisi"},
            activate=False,
            actor=self.user,
            change_reason="Penyesuaian kapasitas vendor.",
        )

        self.assertEqual(plan.status, ProductionPlan.Status.ACTIVE)
        self.assertEqual(plan.notes, "Target direvisi")
        self.assertTrue(
            ProductionActivity.objects.filter(
                production_order=self.production_order,
                action="production_plan_revised",
            ).exists()
        )

    def test_activity_pipeline_allows_partial_cmt_and_partial_qc(self):
        self._activate_plan()
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_PURCHASE,
            activity_date=date(2026, 8, 23),
            actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_ARRIVAL,
            activity_date=date(2026, 8, 24),
            actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_SUBMIT,
            activity_date=date(2026, 8, 24),
            notes="Trial sesuai.",
            actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_APPROVE,
            activity_date=date(2026, 8, 24),
            notes="Approved.",
            actor=self.user,
        )
        for activity_type, quantity in (("CUT", 40), ("MAKE", 20), ("TRIM", 8)):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=activity_type,
                activity_date=date(2026, 8, 25),
                quantity=quantity,
                po_line=self.line,
                actor=self.user,
            )
        qc_activity = submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.QC,
            activity_date=date(2026, 8, 25),
            po_line=self.line,
            qty_inspected=5,
            qty_passed=4,
            qty_failed=1,
            failed_disposition=QCInspection.Disposition.REWORK,
            actor=self.user,
        )
        snapshot = production_snapshot(self.production_order)
        self.assertEqual(qc_activity.quantity, Decimal("5"))
        self.assertEqual(snapshot["trim_completed_qty"], Decimal("8"))
        self.assertEqual(snapshot["remaining_qc_qty"], Decimal("3"))
        self.assertTrue(snapshot["qc_open"])

        delivery_order, delivery_activities = submit_delivery_activity_batch(
            production_order=self.production_order,
            activity_date=date(2026, 8, 26),
            line_quantities=[(self.line, 3)],
            notes="Pengiriman pertama.",
            actor=self.user,
        )
        delivery_activity = delivery_activities[0]
        self.assertEqual(delivery_order.number, "DOP.VOB-08/26-001")
        self.assertEqual(delivery_activity.delivery_order, delivery_order)
        self.client.force_login(self.user)
        preview = self.client.get(
            reverse("production:delivery_order_preview"),
            {"date": "2026-09-01"},
        )
        self.assertEqual(preview.json()["number"], "DOP.VOB-09/26-001")
        snapshot = production_snapshot(self.production_order)
        self.assertEqual(snapshot["ready_to_deliver_qty"], Decimal("1"))
        self.assertEqual(snapshot["delivering_qty"], Decimal("3"))

        warehouse = Warehouse.objects.create(code="WH-PROD", name="Vobia Warehouse")
        main_warehouse = Warehouse.objects.get(code="MAIN")
        inbound_page = self.client.get(reverse("inventory:inbound"))
        self.assertContains(inbound_page, "1 Delivery Order")
        self.assertContains(inbound_page, '<table class="delivery-order-queue-table">')
        self.assertContains(inbound_page, "<th>No. DO</th>")
        self.assertContains(inbound_page, "View Detail")
        self.assertContains(inbound_page, ">Receive<")
        self.assertContains(inbound_page, "data-preserve-scroll")
        self.assertContains(inbound_page, "Qty Received")
        self.assertNotContains(inbound_page, "No. GRN / Surat Jalan")
        self.assertContains(inbound_page, "3 pcs")
        self.assertNotContains(inbound_page, 'name="received_qty" value="3"')
        self.assertContains(
            inbound_page,
            f'<option value="{main_warehouse.id}" selected>Main Warehouse</option>',
        )
        self.assertContains(inbound_page, 'min="2026-08-26"')
        self.assertContains(inbound_page, 'value="2026-08-26"')
        rejected_early_date = self.client.post(
            reverse("inventory:inbound"),
            {
                "form_name": "delivery_receive",
                "delivery_activity": str(delivery_activity.id),
                "inbound_date": "2026-08-25",
                "received_qty": "3",
                "warehouse": str(warehouse.id),
            },
        )
        self.assertContains(
            rejected_early_date,
            "Tanggal Diterima minimal 26/08/2026, sama dengan Tanggal Kirim.",
        )
        rejected_partial = self.client.post(
            reverse("inventory:inbound"),
            {
                "form_name": "delivery_receive",
                "delivery_activity": str(delivery_activity.id),
                "inbound_date": "2026-08-27",
                "received_qty": "2",
                "warehouse": str(warehouse.id),
            },
        )
        self.assertContains(
            rejected_partial,
            "Quantity Received harus sama dengan Qty Delivering (3 pcs).",
        )
        inbound_response = self.client.post(
            reverse("inventory:inbound"),
            {
                "form_name": "delivery_receive",
                "delivery_activity": str(delivery_activity.id),
                "inbound_date": "2026-08-27",
                "received_qty": "3",
                "warehouse": str(warehouse.id),
                "notes": "Diterima parsial.",
            },
        )
        self.assertRedirects(
            inbound_response,
            f"{reverse('inventory:inbound')}?delivery_order={delivery_order.id}"
            f"#delivery-order-{delivery_order.id}",
        )
        snapshot = production_snapshot(self.production_order)
        self.assertEqual(snapshot["delivering_qty"], Decimal("0"))
        self.assertEqual(snapshot["received_qty"], Decimal("3"))
        self.assertTrue(
            InboundReceipt.objects.get(delivery_activity=delivery_activity).reference.startswith(
                "DOP.VOB-08/26-001/"
            )
        )
        completed_inbound_page = self.client.get(
            reverse("inventory:inbound"),
            {"delivery_order": delivery_order.id},
        )
        self.assertEqual(completed_inbound_page.context["delivery_orders"], [])
        self.assertEqual(len(completed_inbound_page.context["completed_delivery_orders"]), 1)
        self.assertContains(completed_inbound_page, "Pengiriman sudah diterima")
        self.assertContains(completed_inbound_page, delivery_order.number)
        self.assertContains(completed_inbound_page, "Received")

        response = self.client.get(reverse("production:detail", args=[self.production_order.id]))
        details = {entry["stage"].stage: entry["detail"] for entry in response.context["stage_entries"]}

        self.assertEqual(details[ProductionStage.Stage.CUT]["rows"][0]["quantity"], Decimal("40"))
        self.assertEqual(details[ProductionStage.Stage.MAKE]["rows"][0]["quantity"], Decimal("20"))
        self.assertEqual(details[ProductionStage.Stage.TRIM]["rows"][0]["quantity"], Decimal("8"))
        self.assertEqual(response.context["qc_detail"]["rows"][0]["quantity"], Decimal("4"))
        self.assertEqual(response.context["inbound_detail"]["rows"][0]["quantity"], Decimal("3"))
        self.assertContains(response, "Delivering")
        self.assertEqual(response.context["snapshot"]["material_status_display"], "Material telah diproses")
        self.assertContains(response, "Material telah diproses")
        self.assertContains(response, "Tindak lanjut: Rework.")
        self.assertContains(response, "Alasan gagal: belum dicatat.")
        self.assertContains(response, "View Detail", count=5)
        for label in ("Qty Cut", "Qty Make", "Qty Trim", "Qty QC Passed", "Qty Inbound"):
            self.assertContains(response, label)

        filtered_response = self.client.get(
            reverse("production:detail", args=[self.production_order.id]),
            {"activity": "Quality Control"},
        )
        self.assertEqual(filtered_response.context["selected_audit_activity"], "Quality Control")
        self.assertTrue(filtered_response.context["activities"])
        self.assertTrue(
            all(
                row["activity_label"] == "Quality Control"
                for row in filtered_response.context["activities"]
            )
        )
        self.assertContains(filtered_response, "Semua Activity")
        self.assertContains(filtered_response, "Reset")

    def test_manager_approves_final_quantity_and_revalues_only_rejected_sku(self):
        ProductionStage.objects.filter(
            production_order=self.production_order,
            stage=ProductionStage.Stage.TRIM,
        ).update(completed_qty=Decimal("100"))
        record_qc(
            po_line=self.line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 25, 10, 0)),
            qty_inspected=100,
            qty_passed=98,
            qty_failed=2,
            disposition=QCInspection.Disposition.REJECTED,
            notes="Reject permanen.",
            actor=self.user,
        )
        warehouse = Warehouse.objects.create(code="WH-COGS", name="COGS Warehouse")
        receipt, movement = record_inbound(
            po_line=self.line,
            inbound_date=date(2026, 8, 26),
            received_qty=98,
            warehouse=warehouse,
            reference="GRN-COGS-FINAL",
            actor=self.user,
        )
        card = production_cogs_finalization_card(self.production_order, actor=self.user)
        self.assertTrue(card["ready"])
        self.assertFalse(card["can_approve"])
        self.assertEqual(card["rows"][0]["final_unit_cogs"], Decimal("127551.0204"))

        self.client.force_login(self.user)
        approve_url = reverse("production:approve_cogs_finalization", args=[self.production_order.id])
        self.assertEqual(self.client.post(approve_url).status_code, 403)

        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=("is_superuser", "is_staff"))
        detail = self.client.get(reverse("production:detail", args=[self.production_order.id]))
        self.assertContains(detail, "Finalisasi Quantity &amp; COGS")
        self.assertContains(detail, "Approve Final Quantity &amp; COGS")
        response = self.client.post(approve_url)
        self.assertRedirects(
            response,
            f"{reverse('production:detail', args=[self.production_order.id])}#cogs-finalization",
        )

        finalization = ProductionCogsFinalization.objects.get(production_order=self.production_order)
        layer = FIFOLayer.objects.get(source_po_line=self.line)
        movement.refresh_from_db()
        self.sku.refresh_from_db()
        self.assertEqual(finalization.total_po_cost, Decimal("12500000"))
        self.assertEqual(layer.unit_cost, Decimal("127551.0204"))
        self.assertEqual(movement.allocated_cost, Decimal("12500000"))
        self.assertEqual(self.sku.current_master_cogs, Decimal("127551.0204"))
        self.assertTrue(
            ProductionActivity.objects.filter(
                production_order=self.production_order,
                action="production_cogs_finalized",
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action="production_cogs_finalized",
                entity_id=str(finalization.id),
            ).exists()
        )

    def test_parent_sku_absorbs_cutting_mix_and_reject_cost(self):
        second_sku = SKU.objects.create(
            sku="SKU-PROD-2",
            product_variant=self.sku.product_variant,
            size="M",
            current_retail_price=Decimal("299000"),
            current_master_cogs=Decimal("125000"),
        )
        second_line = PurchaseOrderLine.objects.create(
            po=self.po,
            sku=second_sku,
            ordered_qty=Decimal("50"),
            cogs_snapshot=Decimal("125000"),
        )
        self._activate_plan()
        for activity_type, activity_date in (
            (ProductionActivity.ActivityType.MATERIAL_PURCHASE, date(2026, 8, 23)),
            (ProductionActivity.ActivityType.MATERIAL_ARRIVAL, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_SUBMIT, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_APPROVE, date(2026, 8, 24)),
        ):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=activity_type,
                activity_date=activity_date,
                actor=self.user,
            )
        for activity_type in (
            ProductionActivity.ActivityType.CUT,
            ProductionActivity.ActivityType.MAKE,
            ProductionActivity.ActivityType.TRIM,
        ):
            submit_cmt_activity_batch(
                production_order=self.production_order,
                activity_type=activity_type,
                activity_date=date(2026, 8, 25),
                line_quantities=((self.line, 98), (second_line, 56)),
                actor=self.user,
            )

        record_qc(
            po_line=self.line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 25, 10, 0)),
            qty_inspected=98,
            qty_passed=96,
            qty_failed=2,
            disposition=QCInspection.Disposition.REJECTED,
            notes="Reject permanen.",
            actor=self.user,
        )
        record_qc(
            po_line=second_line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 25, 11, 0)),
            qty_inspected=56,
            qty_passed=56,
            qty_failed=0,
            disposition="",
            notes="Lolos.",
            actor=self.user,
        )
        warehouse = Warehouse.objects.create(code="WH-PARENT", name="Parent COGS Warehouse")
        _receipt_one, movement_one = record_inbound(
            po_line=self.line,
            inbound_date=date(2026, 8, 26),
            received_qty=96,
            warehouse=warehouse,
            reference="GRN-PARENT-1",
            actor=self.user,
        )
        _receipt_two, movement_two = record_inbound(
            po_line=second_line,
            inbound_date=date(2026, 8, 26),
            received_qty=56,
            warehouse=warehouse,
            reference="GRN-PARENT-2",
            actor=self.user,
        )

        card = production_cogs_finalization_card(self.production_order, actor=self.user)
        self.assertTrue(card["ready"], card["blockers"])
        self.assertEqual(card["total_shortage_qty"], Decimal("0"))
        self.assertEqual(card["total_excess_qty"], Decimal("4"))
        self.assertEqual(card["rows"][0]["shortage_qty"], Decimal("2"))
        self.assertEqual(card["rows"][1]["excess_qty"], Decimal("6"))
        self.assertSetEqual(
            {row["final_unit_cogs"] for row in card["rows"]},
            {Decimal("123355.2632")},
        )

        self.user.is_superuser = True
        self.user.save(update_fields=("is_superuser",))
        approve_production_cogs_finalization(production_order=self.production_order, actor=self.user)
        movement_one.refresh_from_db()
        movement_two.refresh_from_db()
        self.sku.refresh_from_db()
        second_sku.refresh_from_db()
        self.assertEqual(movement_one.allocated_cost + movement_two.allocated_cost, Decimal("18750000"))
        self.assertEqual(self.sku.current_master_cogs, Decimal("123355.2632"))
        self.assertEqual(second_sku.current_master_cogs, Decimal("123355.2632"))
        self.assertEqual(
            production_cogs_finalization_card(self.production_order)["total_shortage_qty"],
            Decimal("0"),
        )

    def test_activity_qc_form_submits_all_sku_rows_in_one_batch(self):
        second_sku = SKU.objects.create(
            sku="SKU-PROD-2",
            product_variant=self.sku.product_variant,
            size="M",
            current_retail_price=Decimal("299000"),
            current_master_cogs=Decimal("120000"),
        )
        second_line = PurchaseOrderLine.objects.create(
            po=self.po,
            sku=second_sku,
            ordered_qty=Decimal("50"),
            cogs_snapshot=Decimal("125000"),
        )
        self._activate_plan()
        for activity_type, activity_date in (
            (ProductionActivity.ActivityType.MATERIAL_PURCHASE, date(2026, 8, 23)),
            (ProductionActivity.ActivityType.MATERIAL_ARRIVAL, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_SUBMIT, date(2026, 8, 24)),
            (ProductionActivity.ActivityType.TRIAL_APPROVE, date(2026, 8, 24)),
        ):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=activity_type,
                activity_date=activity_date,
                actor=self.user,
            )
        for activity_type in (
            ProductionActivity.ActivityType.CUT,
            ProductionActivity.ActivityType.MAKE,
            ProductionActivity.ActivityType.TRIM,
        ):
            for line, quantity in ((self.line, 8), (second_line, 6)):
                submit_production_activity(
                    production_order=self.production_order,
                    activity_type=activity_type,
                    activity_date=date(2026, 8, 25),
                    quantity=quantity,
                    po_line=line,
                    actor=self.user,
                )

        self.client.force_login(self.user)
        url = f"{reverse('production:activity')}?production_order={self.production_order.id}"
        response = self.client.get(url)
        rows = {
            row["line"].sku.sku: row
            for row in response.context["activity_form"].qc_line_fields
        }
        self.assertEqual(rows["SKU-PROD-1"]["remaining_qty"], Decimal("8"))
        self.assertEqual(rows["SKU-PROD-2"]["remaining_qty"], Decimal("6"))
        self.assertContains(response, "Sisa Wajib QC")
        self.assertContains(response, "SKU-PROD-1")
        self.assertContains(response, "SKU-PROD-2")
        self.assertContains(response, "data-qc-failed")
        self.assertContains(response, "Alasan Gagal")

        response = self.client.post(
            reverse("production:activity"),
            {
                "production_order": str(self.production_order.id),
                "activity_type": ProductionActivity.ActivityType.QC,
                "activity_date": "2026-08-26",
                f"qc_inspected_{self.line.id}": "5",
                f"qc_passed_{self.line.id}": "4",
                f"qc_disposition_{self.line.id}": QCInspection.Disposition.REWORK,
                f"qc_failure_reason_{self.line.id}": "Jahitan lepas.",
                f"qc_inspected_{second_line.id}": "6",
                f"qc_passed_{second_line.id}": "6",
                f"qc_disposition_{second_line.id}": "",
                "notes": "QC batch semua SKU.",
            },
        )

        self.assertEqual(response.status_code, 302)
        inspections = QCInspection.objects.filter(po_line__po=self.po).order_by("po_line__sku__sku")
        self.assertEqual(inspections.count(), 2)
        self.assertEqual(inspections[0].qty_failed, Decimal("1"))
        self.assertEqual(inspections[0].notes, "Jahitan lepas.")
        self.assertEqual(inspections[1].qty_failed, Decimal("0"))
        self.assertEqual(
            ProductionActivity.objects.filter(
                production_order=self.production_order,
                activity_type=ProductionActivity.ActivityType.QC,
                entry_kind=ProductionActivity.EntryKind.ACTIVITY,
            ).count(),
            2,
        )
        self.assertIn(
            "Tindak lanjut: Rework. Alasan gagal: Jahitan lepas.",
            ProductionActivity.objects.filter(
                production_order=self.production_order,
                activity_type=ProductionActivity.ActivityType.QC,
                po_line=self.line,
            ).get().description,
        )

    def test_approved_trial_cannot_be_approved_twice(self):
        self._activate_plan()
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_PURCHASE,
            activity_date=date(2026, 8, 23), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_ARRIVAL,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_SUBMIT,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_APPROVE,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        with self.assertRaisesMessage(ValidationError, "sudah selesai"):
            submit_production_activity(
                production_order=self.production_order,
                activity_type=ProductionActivity.ActivityType.TRIAL_APPROVE,
                activity_date=date(2026, 8, 25), actor=self.user,
            )

    def test_passed_and_next_process_advance_only_after_completed_milestone(self):
        self._activate_plan()
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_PURCHASE,
            activity_date=date(2026, 8, 23), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_ARRIVAL,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_SUBMIT,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_APPROVE,
            activity_date=date(2026, 8, 24), actor=self.user,
        )

        snapshot = production_snapshot(self.production_order)
        self.assertEqual(snapshot["passed_process_label"], "Trial Production Approved")
        self.assertEqual(snapshot["next_process_label"], "Cut - Potong")

        self.client.force_login(self.user)
        response = self.client.get(reverse("production:detail", args=[self.production_order.id]), follow=True)
        self.assertContains(response, "Passed Process")
        self.assertContains(response, "Trial Production Approved")
        self.assertContains(response, "Next Process")
        self.assertContains(response, "Cut - Potong")

        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.CUT,
            activity_date=date(2026, 8, 25),
            quantity=Decimal("100"),
            po_line=self.line,
            actor=self.user,
        )
        snapshot = production_snapshot(self.production_order)
        self.assertEqual(snapshot["passed_process_label"], "Cut - Potong")
        self.assertEqual(snapshot["next_process_label"], "Make · Jahit")

    def test_monitoring_compares_plan_targets_with_activity_actual_dates(self):
        self._activate_plan()
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_PURCHASE,
            activity_date=date(2026, 8, 23), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.MATERIAL_ARRIVAL,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_SUBMIT,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.TRIAL_APPROVE,
            activity_date=date(2026, 8, 24), actor=self.user,
        )
        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.CUT,
            activity_date=date(2026, 8, 25), quantity=Decimal("40"), po_line=self.line, actor=self.user,
        )

        snapshot = production_snapshot(self.production_order)
        cut_timing = snapshot["timing"][ProductionStage.Stage.CUT]
        self.assertEqual(cut_timing["target_start"], date(2026, 8, 25))
        self.assertEqual(cut_timing["target_end"], date(2026, 8, 26))
        self.assertEqual(cut_timing["actual_start"], date(2026, 8, 25))
        self.assertIsNone(cut_timing["actual_end"])

        submit_production_activity(
            production_order=self.production_order,
            activity_type=ProductionActivity.ActivityType.CUT,
            activity_date=date(2026, 8, 26), quantity=Decimal("60"), po_line=self.line, actor=self.user,
        )
        snapshot = production_snapshot(self.production_order)
        cut_timing = snapshot["timing"][ProductionStage.Stage.CUT]
        self.assertEqual(cut_timing["actual_end"], date(2026, 8, 26))
        self.assertEqual(cut_timing["status"], "Selesai Tepat Waktu")

        self.client.force_login(self.user)
        response = self.client.get(reverse("production:detail", args=[self.production_order.id]))
        self.assertContains(response, "Target mulai")
        self.assertContains(response, "Aktual selesai")
        self.assertContains(response, "Selesai Tepat Waktu")
        self.assertContains(response, "Target inbound")
