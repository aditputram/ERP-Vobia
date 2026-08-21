from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Supplier
from merchandising.models import IncomingPlan, ProjectionScenario, SalesProjection
from merchandising.services.workflows import approve_incoming_plan, approve_sales_projection, create_incoming_plan

from .models import PPICRequirement, PurchaseOrder
from .services.workflows import create_draft_po, release_po, sync_ppic_requirement


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

    def test_review_create_release_and_immutable_cogs_snapshot(self):
        plan = self._approved_incoming(100)
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        po = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            requirement_quantities={requirement.id: 60},
        )
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertIsNone(po.po_number)
        po = release_po(po.id, self.user)
        self.assertEqual(po.po_number, "PO-VOB-09/26-001")
        line = po.lines.get()
        self.assertEqual(line.cogs_snapshot, Decimal("100000"))

        self.sku.current_master_cogs = Decimal("120000")
        self.sku.save(update_fields=["current_master_cogs"])
        line.refresh_from_db()
        self.assertEqual(line.cogs_snapshot, Decimal("100000"))

        second = create_draft_po(
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            actor=self.user,
            requirement_quantities={requirement.id: 40},
        )
        self.assertEqual(release_po(second.id, self.user).po_number, "PO-VOB-09/26-002")
        requirement.refresh_from_db()
        self.assertEqual(requirement.remaining_qty, Decimal("0"))

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
        payload = {
            "form_name": "requirement_po",
            "supplier": str(self.supplier.id),
            "need_month": "2026-09-01",
            "required_arrival": "2026-09-20",
            "notes": "UAT flow",
            "requirements": [str(requirement.id)],
            f"qty_{requirement.id}": "60",
        }

        review_response = self.client.post(reverse("purchasing:overview"), {**payload, "action": "review"})
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(review_response, "REVIEW RESULT · BELUM TERSIMPAN")
        self.assertEqual(PurchaseOrder.objects.count(), 0)

        create_response = self.client.post(reverse("purchasing:overview"), {**payload, "action": "create"})
        self.assertEqual(create_response.status_code, 302)
        po = PurchaseOrder.objects.get()
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(po.required_arrival, date(2026, 9, 20))
        self.assertEqual(po.lines.get().ordered_qty, Decimal("60"))

    def test_tracking_page_is_available_without_test_sheet_data(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("purchasing:tracking"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Belum ada PO aktual")
