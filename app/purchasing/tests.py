import tempfile
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from audit.models import AuditEvent
from inventory.models import FIFOLayer, InventoryMovement, QCInspection
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Supplier
from merchandising.models import IncomingPlan, ProjectionScenario, SalesProjection
from merchandising.services.workflows import approve_incoming_plan, approve_sales_projection, create_incoming_plan
from production.models import ProductionCogsFinalization
from production.services import ensure_production_order

from .models import PPICRequirement, POWIPImportBatch, PurchaseOrder
from .services.wip_import import approve_po_wip_import, create_po_wip_import
from .services.workflows import (
    create_draft_po,
    delete_unused_supplier,
    release_po,
    revise_legacy_wip_supplier,
    sync_ppic_requirement,
)


class PurchasingWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="buyer", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="APPAREL", name="Apparel")
        product = Product.objects.create(code="P-1", name="Product", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black", color="Black")
        self.sku = SKU.objects.create(
            sku="SKU-1",
            product_variant=variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        self.supplier = Supplier.objects.create(code="SUP-1", name="Supplier 1")
        self.scenario = ProjectionScenario.objects.create(
            name="September 2026",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )

    def _approved_incoming(self, qty=100):
        projection = SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            system_recommendation=Decimal("100"),
        )
        approve_sales_projection(projection.id, 100, self.user)
        plan = create_incoming_plan(projection.id, 0)
        approve_incoming_plan(plan.id, qty, self.user)
        return IncomingPlan.objects.get(pk=plan.id)

    def test_only_approved_incoming_syncs_and_keeps_revision_history(self):
        plan = self._approved_incoming(100)
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        self.assertEqual(requirement.approved_qty, Decimal("100"))
        self.assertEqual(requirement.revisions.count(), 1)

        plan.approval_status = IncomingPlan.ApprovalStatus.DRAFT
        plan.save(update_fields=["approval_status"])
        with self.assertRaises(ValidationError):
            sync_ppic_requirement(plan.id, self.user)

    def test_zero_approved_incoming_does_not_create_ppic_requirement(self):
        projection = SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            system_recommendation=Decimal("0"),
        )
        approve_sales_projection(projection.id, 0, self.user)
        plan = create_incoming_plan(projection.id, 0)
        approve_incoming_plan(plan.id, 0, self.user)

        self.assertFalse(PPICRequirement.objects.filter(incoming_plan=plan).exists())

    def test_zero_resync_removes_unallocated_requirement_and_its_revision(self):
        plan = self._approved_incoming(100)
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        requirement_id = requirement.id
        self.assertEqual(requirement.revisions.count(), 1)

        plan.final_approved_incoming = Decimal("0")
        plan.save(update_fields=["final_approved_incoming"])
        self.assertIsNone(sync_ppic_requirement(plan.id, self.user, "Zero requirement UAT"))

        self.assertFalse(PPICRequirement.objects.filter(pk=requirement_id).exists())

    def test_review_create_release_and_immutable_cogs_snapshot(self):
        plan = self._approved_incoming(100)
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        po = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            requirement_quantities={requirement.id: 60},
            requirement_cogs={requirement.id: 125000},
        )
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertIsNone(po.po_number)
        self.assertEqual(po.lines.get().cogs_snapshot, Decimal("125000"))
        august_release = timezone.make_aware(datetime(2026, 8, 23, 10, 0))
        with patch("purchasing.services.workflows.timezone.now", return_value=august_release):
            po = release_po(po.id, self.user)
        self.assertEqual(po.po_number, "PO-VOB-08/26-001")
        self.assertEqual(po.issue_month, date(2026, 8, 1))
        self.assertEqual(po.need_month, date(2026, 9, 1))
        line = po.lines.get()
        self.assertEqual(line.cogs_snapshot, Decimal("125000"))

        self.sku.current_master_cogs = Decimal("120000")
        self.sku.save(update_fields=["current_master_cogs"])
        line.refresh_from_db()
        self.assertEqual(line.cogs_snapshot, Decimal("125000"))

        second = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            requirement_quantities={requirement.id: 40},
        )
        with patch("purchasing.services.workflows.timezone.now", return_value=august_release):
            self.assertEqual(release_po(second.id, self.user).po_number, "PO-VOB-08/26-002")
        requirement.refresh_from_db()
        self.assertEqual(requirement.remaining_qty, Decimal("0"))

        september_release = timezone.make_aware(datetime(2026, 9, 2, 10, 0))
        third = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            manual_lines=[(self.sku, 1)],
        )
        with patch("purchasing.services.workflows.timezone.now", return_value=september_release):
            third = release_po(third.id, self.user)
        self.assertEqual(third.po_number, "PO-VOB-09/26-001")
        self.assertEqual(third.issue_month, date(2026, 9, 1))

    def test_cannot_order_above_remaining_requirement(self):
        plan = self._approved_incoming(100)
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        with self.assertRaises(ValidationError):
            create_draft_po(
                supplier=self.supplier,
                need_month=date(2026, 9, 1),
                actor=self.user,
                requirement_quantities={requirement.id: 101},
            )
        self.assertEqual(PPICRequirement.objects.count(), 1)

    def test_ppic_page_reviews_without_write_then_creates_draft_after_confirmation(self):
        plan = self._approved_incoming(100)
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        self.client.force_login(self.user)
        review_response = self.client.get(
            reverse("purchasing:generator"),
            {"month": "2026-09", "review": "1"},
        )
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(review_response, "REVIEW RESULT · BELUM TERSIMPAN")
        self.assertContains(review_response, requirement.sku.sku)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

        create_response = self.client.post(
            reverse("purchasing:generator"),
            {
                "form_name": "generator_create",
                "month": "2026-09",
                "supplier": str(self.supplier.id),
                "arrival_2026_09": "2026-09-20",
                "notes": "UAT flow",
                f"cogs_{requirement.id}": "115000",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        po = PurchaseOrder.objects.get()
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(po.required_arrival, date(2026, 9, 20))
        self.assertEqual(po.lines.get().ordered_qty, Decimal("100"))
        self.assertEqual(po.lines.get().cogs_snapshot, Decimal("115000"))

        second_review = self.client.get(
            reverse("purchasing:generator"),
            {"month": "2026-09", "review": "1"},
        )
        self.assertEqual(second_review.context["generator"]["candidates"], [])

        delete_response = self.client.post(reverse("purchasing:po_delete_draft", args=[po.id]))
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        restored_review = self.client.get(
            reverse("purchasing:generator"),
            {"month": "2026-09", "review": "1"},
        )
        self.assertEqual(
            [row.id for row in restored_review.context["generator"]["candidates"]],
            [requirement.id],
        )

    def test_po_generator_filters_exact_candidates_and_creates_one_po_per_need_month(self):
        first_plan = self._approved_incoming(100)
        first = PPICRequirement.objects.get(incoming_plan=first_plan)
        second_scenario = ProjectionScenario.objects.create(
            name="October 2026",
            start_month=date(2026, 10, 1),
            end_month=date(2026, 10, 1),
            created_by=self.user,
        )
        projection = SalesProjection.objects.create(
            scenario=second_scenario,
            month=date(2026, 10, 1),
            sku=self.sku,
            system_recommendation=Decimal("40"),
        )
        approve_sales_projection(projection.id, 40, self.user)
        second_plan = create_incoming_plan(projection.id, 0)
        approve_incoming_plan(second_plan.id, 40, self.user)
        second = PPICRequirement.objects.get(incoming_plan=second_plan)

        self.client.force_login(self.user)
        params = {
            "month": ["2026-09", "2026-10"],
            "status": str(self.sku.product_variant.product.status_id),
            "category": str(self.sku.product_variant.product.category_id),
            "product": str(self.sku.product_variant.product_id),
            "size": "—",
            "review": "1",
        }
        response = self.client.get(reverse("purchasing:generator"), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row.id for row in response.context["generator"]["candidates"]},
            {first.id, second.id},
        )
        self.assertEqual(PurchaseOrder.objects.count(), 0)

        create_response = self.client.post(
            reverse("purchasing:generator"),
            {
                "form_name": "generator_create",
                "month": ["2026-09", "2026-10"],
                "status": str(self.sku.product_variant.product.status_id),
                "category": str(self.sku.product_variant.product.category_id),
                "product": str(self.sku.product_variant.product_id),
                "size": "—",
                "supplier": str(self.supplier.id),
                "arrival_2026_09": "2026-09-20",
                "arrival_2026_10": "2026-10-20",
                f"cogs_{first.id}": "115000",
                f"cogs_{second.id}": "125000",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(PurchaseOrder.objects.count(), 2)
        self.assertEqual(
            set(PurchaseOrder.objects.values_list("need_month", flat=True)),
            {date(2026, 9, 1), date(2026, 10, 1)},
        )
        self.assertEqual(
            set(PurchaseOrder.objects.values_list("lines__ordered_qty", flat=True)),
            {Decimal("100"), Decimal("40")},
        )
        self.assertEqual(
            set(PurchaseOrder.objects.values_list("lines__cogs_snapshot", flat=True)),
            {Decimal("115000"), Decimal("125000")},
        )

    def test_po_generator_options_cascade_by_month_and_exclude_drafted_need_keys(self):
        september_plan = self._approved_incoming(100)
        september_requirement = PPICRequirement.objects.get(incoming_plan=september_plan)

        october_status = ProductStatus.objects.create(code="ESSENTIAL", name="Essential+")
        october_category = Category.objects.create(code="KNITWEAR", name="Knitwear")
        october_product = Product.objects.create(
            code="P-2",
            name="October Product",
            status=october_status,
            category=october_category,
        )
        october_variant = ProductVariant.objects.create(
            product=october_product,
            name="Navy",
            color="Navy",
        )
        october_sku = SKU.objects.create(
            sku="SKU-OCT",
            product_variant=october_variant,
            current_retail_price=Decimal("300000"),
            current_master_cogs=Decimal("150000"),
        )
        october_scenario = ProjectionScenario.objects.create(
            name="October 2026",
            start_month=date(2026, 10, 1),
            end_month=date(2026, 10, 1),
            created_by=self.user,
        )
        october_projection = SalesProjection.objects.create(
            scenario=october_scenario,
            month=date(2026, 10, 1),
            sku=october_sku,
            system_recommendation=Decimal("40"),
        )
        approve_sales_projection(october_projection.id, 40, self.user)
        october_plan = create_incoming_plan(october_projection.id, 0)
        approve_incoming_plan(october_plan.id, 40, self.user)

        self.client.force_login(self.user)
        september_response = self.client.get(
            reverse("purchasing:generator"),
            {"month": "2026-09"},
        )
        generator = september_response.context["generator"]
        self.assertEqual(
            {option["value"] for option in generator["status_options"]},
            {str(self.sku.product_variant.product.status_id)},
        )
        self.assertEqual(
            {option["value"] for option in generator["category_options"]},
            {str(self.sku.product_variant.product.category_id)},
        )
        self.assertEqual(
            {option["value"] for option in generator["product_options"]},
            {str(self.sku.product_variant.product_id)},
        )
        self.assertNotIn(
            str(october_product.id),
            {option["value"] for option in generator["product_options"]},
        )

        create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            required_arrival=date(2026, 9, 20),
            actor=self.user,
            requirement_quantities={september_requirement.id: september_requirement.approved_qty},
        )
        stale_response = self.client.get(
            reverse("purchasing:generator"),
            {
                "month": "2026-09",
                "status": str(self.sku.product_variant.product.status_id),
                "category": str(self.sku.product_variant.product.category_id),
                "product": str(self.sku.product_variant.product_id),
            },
        )
        stale_generator = stale_response.context["generator"]
        self.assertNotIn("2026-09", {option["value"] for option in stale_generator["month_options"]})
        self.assertEqual(stale_generator["selected"]["months"], [])
        self.assertNotIn(
            str(self.sku.product_variant.product_id),
            {option["value"] for option in stale_generator["product_options"]},
        )
        self.assertEqual(stale_generator["candidates"], [PPICRequirement.objects.get(incoming_plan=october_plan)])

    def test_tracking_page_is_available_without_test_sheet_data(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("purchasing:tracking"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Belum ada PO aktual")

    def test_ppic_navigation_is_split_into_four_business_pages(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_module"] = "operation"
        session.save()
        expected = {
            "purchasing:requirements": "PPIC Requirement",
            "purchasing:generator": "Create dan Print Purchase Order",
            "purchasing:purchase_orders": "Daftar dan Tracking Purchase Order",
            "purchasing:vendors": "Daftar Vendor",
        }
        for route, heading in expected.items():
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, heading)
                self.assertContains(response, "Requirement")
                self.assertContains(response, "PO Generator")
                self.assertContains(response, "Purchase Order")
                self.assertContains(response, "Daftar Vendor")
                for group_name in ("merchandising", "purchasing", "production", "inventory"):
                    self.assertContains(response, f'data-nav-group-toggle="{group_name}"')
                    self.assertContains(response, f'data-nav-group-panel="{group_name}"')
                self.assertContains(response, "Planning Builder")
                self.assertContains(response, "Production Monitoring")
                self.assertContains(response, "Inventory Summary")

    def test_requirement_need_month_filter_updates_table_and_kpis(self):
        september_plan = self._approved_incoming(100)
        october_scenario = ProjectionScenario.objects.create(
            name="October 2026",
            start_month=date(2026, 10, 1),
            end_month=date(2026, 10, 1),
            created_by=self.user,
        )
        october_projection = SalesProjection.objects.create(
            scenario=october_scenario,
            month=date(2026, 10, 1),
            sku=self.sku,
            system_recommendation=Decimal("40"),
        )
        approve_sales_projection(october_projection.id, 40, self.user)
        october_plan = create_incoming_plan(october_projection.id, 0)
        approve_incoming_plan(october_plan.id, 40, self.user)

        self.client.force_login(self.user)
        response = self.client.get(reverse("purchasing:requirements"), {"need_month": "2026-09"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_need_month"], "2026-09")
        self.assertEqual(response.context["total_approved"], Decimal("100"))
        self.assertEqual(response.context["total_remaining"], Decimal("100"))
        self.assertEqual(len(response.context["requirements"]), 1)
        self.assertContains(response, "September 2026")
        self.assertContains(response, "All Months")
        self.assertEqual(
            {month.strftime("%Y-%m") for month in response.context["need_month_options"]},
            {september_plan.month.strftime("%Y-%m"), october_plan.month.strftime("%Y-%m")},
        )

    def test_requirement_status_and_category_filters_are_cascading(self):
        first_plan = self._approved_incoming(100)
        second_status = ProductStatus.objects.create(code="ESSENTIAL", name="Essential+")
        second_category = Category.objects.create(code="KNITWEAR", name="Knitwear")
        second_product = Product.objects.create(
            code="P-2",
            name="Second Product",
            status=second_status,
            category=second_category,
        )
        second_variant = ProductVariant.objects.create(product=second_product, name="Blue", color="Blue")
        second_sku = SKU.objects.create(
            sku="SKU-2",
            product_variant=second_variant,
            current_retail_price=Decimal("250000"),
            current_master_cogs=Decimal("110000"),
        )
        second_projection = SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=second_sku,
            system_recommendation=Decimal("30"),
        )
        approve_sales_projection(second_projection.id, 30, self.user)
        second_plan = create_incoming_plan(second_projection.id, 0)
        approve_incoming_plan(second_plan.id, 30, self.user)

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("purchasing:requirements"),
            {
                "need_month": "2026-09",
                "status": str(second_status.id),
                "category": str(second_category.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status"], str(second_status.id))
        self.assertEqual(response.context["selected_category"], str(second_category.id))
        self.assertEqual(response.context["total_approved"], Decimal("30"))
        self.assertEqual(len(response.context["requirements"]), 1)
        self.assertEqual(response.context["requirements"][0].sku_id, second_sku.id)
        self.assertEqual(
            response.context["category_options"],
            [{"value": str(second_category.id), "label": "Knitwear"}],
        )

        stale_response = self.client.get(
            reverse("purchasing:requirements"),
            {
                "need_month": "2026-09",
                "status": str(self.sku.product_variant.product.status_id),
                "category": str(second_category.id),
            },
        )
        self.assertEqual(stale_response.context["selected_category"], "")
        self.assertEqual(
            stale_response.context["category_options"],
            [{"value": str(self.sku.product_variant.product.category_id), "label": "Apparel"}],
        )

        first_requirement = PPICRequirement.objects.get(incoming_plan=first_plan)
        create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            requirement_quantities={first_requirement.id: 100},
        )
        allocation_response = self.client.get(
            reverse("purchasing:requirements"),
            {
                "need_month": "2026-09",
                "allocation_status": ["Open", "Fully Allocated"],
            },
        )
        self.assertEqual(
            allocation_response.context["selected_allocation_statuses"],
            ["Open", "Fully Allocated"],
        )
        self.assertEqual(
            allocation_response.context["allocation_status_options"],
            [
                {"value": "Open", "label": "Open"},
                {"value": "Fully Allocated", "label": "Fully Allocated"},
            ],
        )
        self.assertEqual(len(allocation_response.context["requirements"]), 2)
        fully_allocated_only = self.client.get(
            reverse("purchasing:requirements"),
            {"need_month": "2026-09", "allocation_status": "Fully Allocated"},
        )
        self.assertEqual(fully_allocated_only.context["total_approved"], Decimal("130"))
        self.assertEqual(fully_allocated_only.context["total_ordered"], Decimal("100"))
        self.assertEqual(fully_allocated_only.context["total_remaining"], Decimal("30"))
        self.assertEqual(fully_allocated_only.context["requirement_line_count"], 2)
        self.assertEqual(len(fully_allocated_only.context["requirements"]), 1)
        self.assertEqual(fully_allocated_only.context["requirements"][0].sku_id, self.sku.id)

    def test_purchase_order_list_can_search_and_filter(self):
        po = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            manual_lines=[(self.sku, 10)],
        )
        po = release_po(po.id, self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("purchasing:purchase_orders"), {"q": po.po_number})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["line_count"], 1)
        self.assertContains(response, po.po_number)

        response = self.client.get(reverse("purchasing:purchase_orders"), {"q": "NOT-FOUND"})
        self.assertEqual(len(response.context["rows"]), 0)

        response = self.client.get(
            reverse("purchasing:purchase_orders"),
            {"supplier": str(self.supplier.id), "need_month": "2026-09", "po_status": "RELEASED", "schedule_status": "Open"},
        )
        self.assertEqual(len(response.context["rows"]), 1)

    def test_purchase_order_list_groups_sku_lines_and_detail_expands_them(self):
        second_sku = SKU.objects.create(
            sku="SKU-2",
            product_variant=self.sku.product_variant,
            current_retail_price=Decimal("250000"),
            current_master_cogs=Decimal("125000"),
        )
        po = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            manual_lines=[(self.sku, 10), (second_sku, 20)],
        )
        po = release_po(po.id, self.user)
        first_line = po.lines.order_by("created_at").first()
        first_line.qc_passed_before_cutover_qty = Decimal("10")
        first_line.save(update_fields=["qc_passed_before_cutover_qty"])
        self.client.force_login(self.user)

        response = self.client.get(reverse("purchasing:purchase_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["line_count"], 2)
        self.assertEqual(response.context["rows"][0]["total_qty"], Decimal("30"))
        self.assertIsNone(response.context["rows"][0]["qc_passed"])
        self.assertContains(response, "Jumlah SKU")
        self.assertNotContains(response, "SKU-1")
        self.assertNotContains(response, "SKU-2")

        detail_response = self.client.get(reverse("purchasing:po_detail", args=[po.id]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "SKU-1")
        self.assertContains(detail_response, "SKU-2")

        print_response = self.client.get(reverse("purchasing:po_print", args=[po.id]))
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "<h1>Purchase Order</h1>", html=True)
        self.assertContains(print_response, "PO Number")
        self.assertNotContains(print_response, "Need Month")
        self.assertNotContains(print_response, "Purchased By")
        self.assertNotContains(print_response, "Vobia Warehouse")
        self.assertNotContains(print_response, "Document Control")
        self.assertNotContains(print_response, "Notes")
        self.assertContains(print_response, "SKU-1")
        self.assertContains(print_response, "SKU-2")
        self.assertContains(print_response, "Rp 3.500.000")
        self.assertContains(print_response, "30 pcs")
        self.assertContains(print_response, "Created By")
        self.assertContains(print_response, "Checked By")
        self.assertContains(print_response, "Approved By")
        self.assertContains(print_response, "Create, Check &amp; Approve by Digital Sign")
        self.assertNotContains(print_response, "Payment Terms")

        QCInspection.objects.create(
            po_line=first_line,
            inspected_at=timezone.now(),
            qty_inspected=Decimal("3"),
            qty_passed=Decimal("2"),
            qty_failed=Decimal("1"),
            failed_disposition=QCInspection.Disposition.REWORK,
            recorded_by=self.user,
        )
        tracked_after_qc = self.client.get(reverse("purchasing:purchase_orders"))
        self.assertEqual(tracked_after_qc.context["rows"][0]["qc_passed"], Decimal("2"))

    def test_po_detail_shows_approved_final_quantity_and_cogs(self):
        po = release_po(
            create_draft_po(
                supplier=self.supplier,
                need_month=date(2026, 9, 1),
                actor=self.user,
                manual_lines=[(self.sku, 10)],
            ).id,
            self.user,
        )
        line = po.lines.get()
        ProductionCogsFinalization.objects.create(
            production_order=ensure_production_order(po, actor=self.user),
            line_snapshot=[{
                "po_line_id": str(line.id),
                "sellable_qty": "9",
                "final_unit_cogs": "111111.1111",
                "unit_cogs_increase": "11111.1111",
            }],
            total_po_cost=Decimal("1000000"),
            total_final_cost=Decimal("1000000"),
            approved_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("purchasing:po_detail", args=[po.id]))

        self.assertContains(response, "Final Qty")
        self.assertContains(response, "Rp 111.111")
        self.assertContains(response, "+Rp 11.111")
        self.assertContains(response, "Total COGS")
        self.assertContains(response, "Rp 1.000.000")
        self.assertNotContains(response, "QC records")
        self.assertContains(response, "Riwayat Quantity Activity")
        self.assertEqual(
            response.context["activity_rows"][0],
            {"label": "Cutting", "qty": Decimal("0"), "po_qty": Decimal("10"), "gap": Decimal("-10")},
        )

    def test_legacy_wip_vendor_revision_is_scoped_and_audited(self):
        corrected_supplier = Supplier.objects.create(code="HARMONI", name="Harmoni")
        po = PurchaseOrder.objects.create(
            po_number="PO-VOB-06/26-087",
            supplier=self.supplier,
            need_month=date(2026, 6, 1),
            source=PurchaseOrder.Source.LEGACY_WIP,
            status=PurchaseOrder.Status.RELEASED,
            created_by=self.user,
            released_by=self.user,
            migration_cutoff_date=date(2026, 7, 31),
            migration_evidence_reference="PO WIP.xlsx · test checksum",
        )
        po = revise_legacy_wip_supplier(
            po.id,
            corrected_supplier,
            self.user,
            "Koreksi berdasarkan PO WIP (1).numbers",
        )
        self.assertEqual(po.supplier, corrected_supplier)
        event = AuditEvent.objects.get(action="legacy_wip_supplier_revised", entity_id=str(po.id))
        self.assertEqual(event.before_values["supplier_name"], "Supplier 1")
        self.assertEqual(event.after_values["supplier_name"], "Harmoni")
        self.assertFalse(event.metadata["quantities_and_cost_snapshots_changed"])

        regular_po = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            manual_lines=[(self.sku, 1)],
        )
        regular_po = release_po(regular_po.id, self.user)
        with self.assertRaises(ValidationError):
            revise_legacy_wip_supplier(
                regular_po.id,
                corrected_supplier,
                self.user,
                "Tidak boleh untuk PO reguler",
            )

    def test_legacy_wip_vendor_revision_view_requires_reason(self):
        corrected_supplier = Supplier.objects.create(code="HARMONI", name="Harmoni")
        po = PurchaseOrder.objects.create(
            po_number="PO-VOB-06/26-087",
            supplier=self.supplier,
            need_month=date(2026, 6, 1),
            source=PurchaseOrder.Source.LEGACY_WIP,
            status=PurchaseOrder.Status.RELEASED,
            created_by=self.user,
            released_by=self.user,
            migration_cutoff_date=date(2026, 7, 31),
            migration_evidence_reference="PO WIP.xlsx · test checksum",
        )
        self.client.force_login(self.user)
        invalid = self.client.post(
            reverse("purchasing:po_revise_vendor", args=[po.id]),
            {"supplier": str(corrected_supplier.id), "reason": ""},
        )
        self.assertEqual(invalid.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.supplier, self.supplier)

        valid = self.client.post(
            reverse("purchasing:po_revise_vendor", args=[po.id]),
            {
                "supplier": str(corrected_supplier.id),
                "reason": "Koreksi berdasarkan PO WIP (1).numbers",
            },
        )
        self.assertEqual(valid.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.supplier, corrected_supplier)

    def test_unused_vendor_can_be_deleted_but_used_vendor_is_protected(self):
        unused = Supplier.objects.create(code="UNUSED", name="Unused Vendor")
        unused_id = unused.id
        deleted = delete_unused_supplier(
            unused.id,
            self.user,
            "Placeholder vendor tidak lagi digunakan",
        )
        self.assertEqual(deleted["name"], "Unused Vendor")
        self.assertFalse(Supplier.objects.filter(pk=unused_id).exists())
        event = AuditEvent.objects.get(action="unused_supplier_deleted", entity_id=str(unused_id))
        self.assertTrue(event.after_values["deleted"])

        po = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            manual_lines=[(self.sku, 1)],
        )
        with self.assertRaises(ValidationError):
            delete_unused_supplier(
                self.supplier.id,
                self.user,
                "Vendor masih dipakai PO sehingga harus ditolak",
            )
        self.assertTrue(Supplier.objects.filter(pk=self.supplier.id).exists())
        self.assertTrue(PurchaseOrder.objects.filter(pk=po.id).exists())

    def test_po_wip_import_stages_then_commits_without_stock_posting(self):
        source = (
            "NO PO,SKU Induk,SKU,Nama Barang,WIP\n"
            "PO-VOB-06/26-087,,SKU-1,Legacy Product Name,10\n"
        )
        uploaded = SimpleUploadedFile("PO WIP.csv", source.encode(), content_type="text/csv")
        with tempfile.TemporaryDirectory() as directory, override_settings(PRIVATE_UPLOAD_ROOT=directory):
            batch = create_po_wip_import(uploaded, self.user)
            self.assertEqual(batch.status, POWIPImportBatch.Status.READY)
            self.assertEqual(batch.po_count, 1)
            self.assertEqual(batch.total_rows, 1)
            self.assertEqual(batch.total_outstanding_qty, Decimal("10"))
            self.assertEqual(batch.blocking_issue_count, 0)
            self.assertEqual(batch.warning_count, 3)
            approve_po_wip_import(batch.id, self.user)

        po = PurchaseOrder.objects.get(po_number="PO-VOB-06/26-087")
        self.assertEqual(po.source, PurchaseOrder.Source.LEGACY_WIP)
        self.assertEqual(po.status, PurchaseOrder.Status.RELEASED)
        self.assertEqual(po.supplier.name, "Vobia Vendor")
        self.assertEqual(po.need_month, date(2026, 6, 1))
        self.assertEqual(po.created_at.date(), date(2026, 7, 31))
        self.assertIsNone(po.required_arrival)
        line = po.lines.get()
        self.assertEqual(line.ordered_qty, Decimal("10"))
        self.assertEqual(line.received_before_cutover_qty, Decimal("0"))
        self.assertEqual(line.qc_passed_before_cutover_qty, Decimal("10"))
        self.assertEqual(line.cogs_snapshot, Decimal("100000"))
        self.assertEqual(InventoryMovement.objects.count(), 0)
        self.assertEqual(FIFOLayer.objects.count(), 0)

    def test_po_wip_import_blocks_duplicate_po_and_sku(self):
        source = (
            "NO PO,SKU Induk,SKU,Nama Barang,WIP\n"
            "PO-VOB-06/26-087,,SKU-1,Product,10\n"
            "PO-VOB-06/26-087,,SKU-1,Product,5\n"
        )
        uploaded = SimpleUploadedFile("PO WIP duplicate.csv", source.encode(), content_type="text/csv")
        with tempfile.TemporaryDirectory() as directory, override_settings(PRIVATE_UPLOAD_ROOT=directory):
            batch = create_po_wip_import(uploaded, self.user)
        self.assertEqual(batch.status, POWIPImportBatch.Status.BLOCKED)
        self.assertGreater(batch.blocking_issue_count, 0)
        self.assertEqual(batch.issues.filter(code="DUPLICATE_PO_SKU").count(), 2)
