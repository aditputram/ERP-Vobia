from datetime import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from inventory.models import InventoryMovement, PhysicalReturnReceipt
from inventory.services.aging import refresh_po_close
from inventory.services.fifo import post_opening, record_inbound, record_physical_return, record_qc
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Supplier, Warehouse
from merchandising.models import ProjectionRule, ProjectionScenario
from merchandising.services.builder import apply_rule
from merchandising.services.workflows import approve_incoming_plan, approve_sales_projection, create_incoming_plan
from purchasing.models import PPICRequirement
from purchasing.services.workflows import create_draft_po, release_po
from reconciliation.models import ReconciliationRun
from reconciliation.services.engine import run_reconciliation
from sales.services.manual import create_manual_sale


class EndToEndAcceptanceFlowTests(TestCase):
    def test_projection_to_reconciliation_and_po_reopen_cycle(self):
        actor = User.objects.create_superuser(username="vobiasuperadmin", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="APPAREL", name="Apparel")
        product = Product.objects.create(code="P-1", name="Acceptance Product", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black")
        sku = SKU.objects.create(
            sku="SKU-E2E",
            product_variant=variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        supplier = Supplier.objects.create(code="SUP-E2E", name="Acceptance Supplier")
        warehouse = Warehouse.objects.create(code="WH-E2E", name="Acceptance Warehouse")
        today = timezone.localdate()
        month = today.replace(day=1)

        post_opening(sku=sku, quantity=10, unit_cost=100000, actor=actor, warehouse=warehouse)
        scenario = ProjectionScenario.objects.create(
            name="Acceptance Scenario",
            start_month=month,
            end_month=month,
            created_by=actor,
        )
        _, counts = apply_rule(
            scenario=scenario,
            target_month=month,
            scope_type=ProjectionRule.ScopeType.PRODUCT,
            method=ProjectionRule.Method.INCREASE_PERCENT,
            parameter=Decimal("0"),
            product=product,
            actor=actor,
            reason="Acceptance flow",
        )
        self.assertEqual(counts["applied"], 1)
        projection = scenario.projections.get(sku=sku)
        approve_sales_projection(projection.id, 20, actor, "Adit target adjustment")
        incoming = create_incoming_plan(projection.id, 10)
        approve_incoming_plan(incoming.id, 10, actor, "Approved for production")
        requirement = PPICRequirement.objects.get(incoming_plan=incoming)
        self.assertEqual(requirement.approved_qty, Decimal("10"))

        draft = create_draft_po(
            supplier=supplier,
            need_month=month,
            actor=actor,
            requirement_quantities={requirement.id: 10},
        )
        po = release_po(draft.id, actor)
        po_line = po.lines.get()
        record_qc(
            po_line=po_line,
            inspected_at=timezone.now(),
            qty_inspected=10,
            qty_passed=10,
            qty_failed=0,
            actor=actor,
        )
        record_inbound(
            po_line=po_line,
            inbound_date=today,
            received_qty=10,
            warehouse=warehouse,
            reference="GRN-E2E-001",
            actor=actor,
        )

        returned_sale = create_manual_sale(
            source_label="Whatsapp",
            order_number="WA-E2E-RETURN",
            order_datetime=timezone.now(),
            sku=sku,
            quantity=20,
            net_unit_price=Decimal("180000"),
            status="Retur",
            actor=actor,
        )
        refresh_po_close(po.id, today)
        po.refresh_from_db()
        self.assertEqual(po.close_date, today)

        record_physical_return(
            sales_line=returned_sale,
            received_date=today,
            quantity=12,
            warehouse=warehouse,
            condition=PhysicalReturnReceipt.Condition.SELLABLE,
            actor=actor,
        )
        po.refresh_from_db()
        self.assertIsNone(po.close_date)

        create_manual_sale(
            source_label="Offline",
            order_number="OFF-E2E-RESALE",
            order_datetime=timezone.now(),
            sku=sku,
            quantity=12,
            net_unit_price=Decimal("200000"),
            status="Selesai",
            actor=actor,
        )
        refresh_po_close(po.id, today)
        po.refresh_from_db()
        self.assertEqual(po.close_date, today)

        run = run_reconciliation(actor, today)
        self.assertEqual(run.status, ReconciliationRun.Status.PASSED)
        self.assertEqual(run.issues.count(), 0)
        self.assertEqual(InventoryMovement.objects.count(), 5)
