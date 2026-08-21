from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Supplier, Warehouse
from purchasing.models import PurchaseOrder, PurchaseOrderLine

from .models import (
    IncomingPlan,
    IncomingCarryover,
    IncomingMonthClose,
    MerchandisingMonthlySnapshot,
    MerchandisingSnapshotBatch,
    ProjectionRule,
    ProjectionScenario,
    SalesProjection,
)
from .services.calculations import (
    apply_product_guardrail,
    beginning_quantity,
    current_month_metric_values,
    current_month_multiplier,
    current_month_projection,
    future_projection,
    incoming_calculation,
    select_effective_rule,
)
from .services.workflows import (
    approve_incoming_plan,
    approve_sales_projection,
    create_incoming_plan,
)
from .services.builder import recommendation_for
from inventory.services.fifo import post_opening
from inventory.services.fifo import record_inbound
from .services.incoming_actuals import close_incoming_month
from sales.services.manual import create_manual_sale


class MerchandisingCalculationTests(TestCase):
    def test_current_month_multiplier_schedule_and_stock_cap(self):
        self.assertEqual(current_month_multiplier(date(2026, 8, 12)), 25)
        self.assertEqual(current_month_multiplier(date(2026, 8, 17)), 26)
        self.assertEqual(current_month_multiplier(date(2026, 8, 24)), 27)
        self.assertEqual(current_month_multiplier(date(2026, 8, 25)), 31)
        self.assertEqual(
            current_month_projection(Decimal("100"), date(2026, 8, 10), Decimal("220")),
            Decimal("220"),
        )

    def test_beginning_uses_prior_ending_plus_same_month_incoming(self):
        self.assertEqual(beginning_quantity(50, 250), Decimal("300"))
        self.assertEqual(beginning_quantity(-10, 4), Decimal("-6"))

    def test_current_projection_uses_run_day_factor_and_cutoff_denominator(self):
        self.assertEqual(
            current_month_projection(
                actual_qty=100,
                cutoff_date=date(2026, 8, 10),
                beginning_qty=400,
                run_date=date(2026, 8, 20),
            ),
            Decimal("270"),
        )
        self.assertEqual(
            current_month_projection(
                actual_qty=100,
                cutoff_date=date(2026, 8, 10),
                beginning_qty=220,
                run_date=date(2026, 8, 20),
            ),
            Decimal("220"),
        )
        self.assertEqual(
            current_month_projection(
                actual_qty=10,
                cutoff_date=date(2026, 8, 10),
                beginning_qty=-5,
                run_date=date(2026, 8, 20),
            ),
            Decimal("0"),
        )

    def test_current_month_metrics_rebuild_financials_from_official_sales_projection(self):
        values = current_month_metric_values(
            prior_ending_qty=50,
            incoming_qty=250,
            actual_qty=100,
            actual_net=18000000,
            cutoff_date=date(2026, 8, 10),
            cogs=100000,
            retail_price=200000,
            run_date=date(2026, 8, 20),
        )
        self.assertEqual(values["beginning_qty"], Decimal("300"))
        self.assertEqual(values["sales_qty"], Decimal("270"))
        self.assertEqual(values["sales_gross"], Decimal("54000000"))
        self.assertEqual(values["sales_net"], Decimal("48600000"))
        self.assertEqual(values["ending_qty"], Decimal("30"))
        self.assertEqual(values["mos"], Decimal("30") / Decimal("270"))

    def test_future_projection_methods(self):
        self.assertEqual(future_projection("INCREASE_PERCENT", 100, 20), Decimal("120"))
        self.assertEqual(future_projection("DECREASE_PERCENT", 100, 20), Decimal("80"))
        self.assertEqual(
            future_projection("TARGET_STOCK_RATIO", 100, 2, beginning_qty=300),
            Decimal("150"),
        )

    def test_incoming_formulas(self):
        values = incoming_calculation(100, 30, Decimal("1.5"))
        self.assertEqual(values["minimum"], Decimal("70"))
        self.assertEqual(values["recommended"], Decimal("120.0"))
        self.assertEqual(values["desired_beginning"], Decimal("150.0"))

    def test_no_incoming_guardrail_caps_sales(self):
        self.assertEqual(
            apply_product_guardrail(100, "Discontinue", "Apparel", available_stock=35),
            Decimal("35"),
        )


class MerchandisingWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="planner", password="test-password")
        self.status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        self.category = Category.objects.create(code="APPAREL", name="Apparel")
        self.product = Product.objects.create(
            code="PRODUCT-1",
            name="Product 1",
            status=self.status,
            category=self.category,
        )
        variant = ProductVariant.objects.create(product=self.product, name="Black", color="Black")
        self.sku = SKU.objects.create(
            sku="SKU-1",
            product_variant=variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        self.scenario = ProjectionScenario.objects.create(
            name="September 2026",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )

    def _projection(self):
        return SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            system_recommendation=Decimal("100"),
        )

    def test_rule_priority_product_over_category_over_status(self):
        rules = [
            ProjectionRule.objects.create(
                scenario=self.scenario,
                target_month=date(2026, 9, 1),
                scope_type=ProjectionRule.ScopeType.PRODUCT_STATUS,
                product_status=self.status,
                method=ProjectionRule.Method.INCREASE_PERCENT,
                parameter=Decimal("5"),
                created_by=self.user,
            ),
            ProjectionRule.objects.create(
                scenario=self.scenario,
                target_month=date(2026, 9, 1),
                scope_type=ProjectionRule.ScopeType.CATEGORY,
                category=self.category,
                method=ProjectionRule.Method.INCREASE_PERCENT,
                parameter=Decimal("10"),
                created_by=self.user,
            ),
            ProjectionRule.objects.create(
                scenario=self.scenario,
                target_month=date(2026, 9, 1),
                scope_type=ProjectionRule.ScopeType.PRODUCT,
                product=self.product,
                method=ProjectionRule.Method.INCREASE_PERCENT,
                parameter=Decimal("15"),
                created_by=self.user,
            ),
        ]
        selected, overridden = select_effective_rule(rules, self.sku)
        self.assertEqual(selected.scope_type, ProjectionRule.ScopeType.PRODUCT)
        self.assertEqual(len(overridden), 2)

    def test_current_month_builder_uses_cutoff_denominator_and_run_date_factor(self):
        post_opening(sku=self.sku, quantity=100, unit_cost=100000, actor=self.user)
        create_manual_sale(
            source_label="Offline",
            order_number="OFF-CUTOFF",
            order_datetime=timezone.make_aware(datetime(2026, 8, 10, 10, 0)),
            sku=self.sku,
            quantity=10,
            net_unit_price=180000,
            status="Selesai",
            actor=self.user,
        )
        result = recommendation_for(
            sku=self.sku,
            target_month=date(2026, 8, 1),
            method=ProjectionRule.Method.INCREASE_PERCENT,
            parameter=0,
            today=date(2026, 8, 20),
        )
        self.assertEqual(result["baseline_month"], date(2026, 8, 10))
        self.assertEqual(result["beginning_qty"], Decimal("100"))
        self.assertEqual(result["recommendation"], Decimal("27"))

    def test_only_approved_projection_creates_incoming(self):
        projection = self._projection()
        with self.assertRaises(ValidationError):
            create_incoming_plan(projection.id, 30)
        approve_sales_projection(projection.id, 100, self.user, "approved for test")
        plan = create_incoming_plan(projection.id, 30, Decimal("1.5"))
        self.assertEqual(plan.minimum_incoming, Decimal("70"))
        self.assertEqual(plan.recommended_incoming, Decimal("120"))

    def test_projection_and_incoming_approval_require_whole_units_and_minimum(self):
        projection = self._projection()
        with self.assertRaises(ValidationError):
            approve_sales_projection(projection.id, Decimal("99.5"), self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.approval_status, SalesProjection.ApprovalStatus.DRAFT)

        approve_sales_projection(projection.id, 100, self.user)
        plan = create_incoming_plan(projection.id, 30)
        with self.assertRaises(ValidationError):
            approve_incoming_plan(plan.id, 69, self.user)
        plan.refresh_from_db()
        self.assertEqual(plan.approval_status, IncomingPlan.ApprovalStatus.DRAFT)

        approved = approve_incoming_plan(plan.id, 70, self.user, "approved for PPIC")
        self.assertEqual(approved.approval_status, IncomingPlan.ApprovalStatus.APPROVED)


class MerchandisingReportViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reporter", password="test-password")
        status = ProductStatus.objects.create(code="REPORT-ACTIVE", name="Active")
        category = Category.objects.create(code="REPORT-APPAREL", name="Apparel")
        product = Product.objects.create(code="REPORT-PRODUCT", parent_sku="REPORT-PARENT", name="Report Product", status=status, category=category)
        self.product = product
        variant = ProductVariant.objects.create(product=product, name="Black", color="Black")
        sku = SKU.objects.create(
            sku="REPORT-SKU",
            product_variant=variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        self.sku = sku
        batch = MerchandisingSnapshotBatch.objects.create(
            source_workbook_id="workbook-test",
            source_file_name="Vobia MD 2026.xlsx",
            source_sha256="a" * 64,
            imported_by=self.user,
            row_count=1,
            is_active=True,
        )
        self.batch = batch
        MerchandisingMonthlySnapshot.objects.bulk_create([
            MerchandisingMonthlySnapshot(
                batch=batch,
                sku=sku,
                source_row=694,
                month=date(2026, month, 1),
                status_snapshot="Active",
                product_snapshot="Report Product",
                category_snapshot="Apparel",
                cogs_snapshot=100000,
                retail_price_snapshot=200000,
                prior_year_ending_qty=10,
                prior_year_ending_cogs=1000000,
                prior_year_ending_gross=2000000,
                incoming_qty=5,
                incoming_cogs=500000,
                incoming_gross=1000000,
                beginning_qty=10,
                beginning_cogs=1000000,
                beginning_gross=2000000,
                sales_qty=2,
                sales_cogs=200000,
                sales_gross=400000,
                sales_net=360000,
                ratio=5,
                ending_qty=13,
                ending_cogs=1300000,
                ending_gross=2600000,
                mos=Decimal("6.5"),
            )
            for month in range(1, 13)
        ])
        self.client.force_login(self.user)

    def test_incoming_month_close_freezes_actual_and_po_backed_carryover(self):
        supplier = Supplier.objects.create(code="SUP-CLOSE", name="Supplier Close")
        warehouse = Warehouse.objects.create(code="WH-CLOSE", name="Warehouse Close")
        po = PurchaseOrder.objects.create(
            po_number="PO-CLOSE-001",
            sequence=1,
            supplier=supplier,
            need_month=date(2026, 8, 1),
            required_arrival=date(2026, 8, 1),
            status=PurchaseOrder.Status.RELEASED,
            source=PurchaseOrder.Source.LEGACY_WIP,
            migration_cutoff_date=date(2026, 7, 31),
            migration_evidence_reference="WIP-EVIDENCE",
            created_by=self.user,
            released_by=self.user,
            released_at=timezone.now(),
        )
        line = PurchaseOrderLine.objects.create(
            po=po,
            sku=self.sku,
            ordered_qty=5,
            cogs_snapshot=100000,
            qc_passed_before_cutover_qty=5,
        )
        record_inbound(
            po_line=line,
            inbound_date=date(2026, 8, 10),
            received_qty=3,
            warehouse=warehouse,
            reference="GRN-CLOSE-001",
            actor=self.user,
        )
        close = close_incoming_month(
            month=date(2026, 8, 1),
            actor=self.user,
            evidence_reference="MONTH-CLOSE-UAT",
            today=date(2026, 8, 31),
            allow_open_month=True,
        )
        actual = close.actual_rows.get(sku=self.sku)
        self.assertEqual(actual.projected_qty, Decimal("5"))
        self.assertEqual(actual.actual_qty, Decimal("3"))
        self.assertEqual(actual.variance_qty, Decimal("-2"))
        carryover = IncomingCarryover.objects.get(source_close=close, po_line=line)
        self.assertEqual(carryover.target_month, date(2026, 9, 1))
        self.assertEqual(carryover.carryover_qty, Decimal("2"))
        response = self.client.get(
            "/merchandising/projection/",
            {"month": ["9"], "metric": ["incoming"], "submetric": ["qty"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["table_rows"][0]["cells"][-1]["value"], Decimal("2"))

    def test_dashboard_uses_snapshot_and_surfaces_source_range_exception(self):
        response = self.client.get("/merchandising/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Monthly Merchandising Indicators")
        self.assertContains(response, "Stock Value Ratio")
        self.assertContains(response, "GPM Rate")
        self.assertContains(response, "Margin Ratio")
        self.assertContains(response, "Incoming Capital Turnover")
        self.assertEqual(response.context["months"], [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ])
        self.assertContains(response, "1 SOURCE-RANGE EXCEPTION")
        self.assertContains(response, "ERP memakai seluruh 1 SKU yang sesuai filter")
        self.assertContains(response, "SUMMARY ↔ PROJECTION CONNECTED")
        rows = {row["label"]: row for row in response.context["table_rows"]}
        self.assertEqual(rows["Beginning Gross"]["values"][7], Decimal("3600000"))
        self.assertEqual(rows["Sales Gross"]["values"][7], Decimal("0"))
        self.assertEqual(rows["Ending Stock Gross"]["values"][7], Decimal("3600000"))
        self.assertEqual(rows["Sales Gross"]["values"][8:], [None, None, None, None])
        self.assertEqual(rows["GPM"]["values"][8:], [None, None, None, None])
        self.assertEqual(rows["GPM"]["values"][0], Decimal("160000"))
        self.assertEqual(rows["GPM Rate"]["values"][0], Decimal("0.4"))
        self.assertEqual(rows["Margin Ratio"]["values"][0], Decimal("1.8"))
        self.assertEqual(rows["Incoming Capital Turnover"]["values"][0], Decimal("0.8"))
        self.assertEqual(rows["Stock Value Ratio"]["values"][0], Decimal("5"))
        self.assertEqual(rows["Stock Value Ratio"]["kind"], "ratio2")
        self.assertEqual(rows["Margin Ratio"]["kind"], "ratio2")
        self.assertEqual(rows["Incoming Capital Turnover"]["kind"], "ratio2")
        self.assertEqual(rows["Sales Gross"]["total"], Decimal("2800000"))

    def test_projection_supports_selected_months_and_metrics(self):
        response = self.client.get(
            "/merchandising/projection/",
            {"month": ["8"], "metric": ["sales", "ending"], "submetric": ["qty"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Projection matrix")
        self.assertContains(response, "Sales QTY")
        self.assertContains(response, "Ending QTY")
        self.assertEqual(
            [header["label"] for header in response.context["dynamic_headers"]],
            ["Ending QTY", "Sales QTY", "Ending QTY"],
        )
        self.assertTrue(response.context["dynamic_headers"][0]["is_auto_previous"])
        self.assertEqual(response.context["dynamic_headers"][0]["month"], "Jul")
        self.assertEqual(response.context["table_rows"][0]["cells"][0]["value"], Decimal("13"))
        self.assertEqual(response.context["table_rows"][0]["cells"][1]["value"], Decimal("0"))
        self.assertEqual(response.context["table_rows"][0]["cells"][2]["value"], Decimal("18"))
        self.assertEqual(response.context["visible_row_count"], 1)
        self.assertContains(response, "seluruh hasil dalam satu halaman")
        self.assertNotContains(response, "50 SKU per halaman")
        self.assertContains(response, "menunggu Projection Builder")

    def test_incoming_view_modes_change_matrix_and_include_filtered_subtotal(self):
        base_query = {"month": ["8"], "metric": ["incoming"], "submetric": ["qty"]}
        projection = self.client.get(
            "/merchandising/projection/",
            {**base_query, "incoming_mode": "projection"},
        )
        actual = self.client.get(
            "/merchandising/projection/",
            {**base_query, "incoming_mode": "actual"},
        )
        comparison = self.client.get(
            "/merchandising/projection/",
            {**base_query, "incoming_mode": "comparison"},
        )
        self.assertEqual([cell["value"] for cell in projection.context["table_rows"][0]["cells"]], [Decimal("13"), Decimal("5")])
        self.assertEqual([cell["value"] for cell in actual.context["table_rows"][0]["cells"]], [Decimal("13"), Decimal("0")])
        self.assertEqual(
            [header["label"] for header in comparison.context["dynamic_headers"]],
            ["Ending QTY", "Incoming QTY · Projection", "Incoming QTY · Actual", "Incoming QTY · Variance"],
        )
        self.assertEqual(
            [cell["value"] for cell in comparison.context["table_rows"][0]["cells"]],
            [Decimal("13"), Decimal("5"), Decimal("0"), Decimal("-5")],
        )
        self.assertEqual(
            [cell["value"] for cell in comparison.context["table_summary"]["cells"]],
            [Decimal("13"), Decimal("5"), Decimal("0"), Decimal("-5")],
        )
        self.assertContains(comparison, "TOTAL FILTER")

    def test_future_month_snapshot_is_not_presented_as_official_projection(self):
        response = self.client.get(
            "/merchandising/projection/",
            {"month": ["9"], "metric": ["incoming", "sales", "ending"], "submetric": ["qty"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dynamic_headers"][0]["month"], "Aug")
        self.assertTrue(response.context["dynamic_headers"][0]["is_auto_previous"])
        self.assertEqual(
            [cell["value"] for cell in response.context["table_rows"][0]["cells"]],
            [Decimal("18"), None, None, None],
        )

    def test_projection_net_submetric_only_applies_to_sales(self):
        response = self.client.get(
            "/merchandising/projection/",
            {"month": ["8"], "metric": ["incoming", "sales", "ending"], "submetric": ["net"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [header["label"] for header in response.context["dynamic_headers"]],
            ["Sales Net"],
        )

    def test_projection_parent_sku_groups_children_and_recomputes_ratios(self):
        second_variant = ProductVariant.objects.create(product=self.product, name="White", color="White")
        second_sku = SKU.objects.create(
            sku="REPORT-SKU-2",
            product_variant=second_variant,
            current_retail_price=Decimal("220000"),
            current_master_cogs=Decimal("110000"),
        )
        batch = MerchandisingSnapshotBatch.objects.get(is_active=True)
        MerchandisingMonthlySnapshot.objects.bulk_create([
            MerchandisingMonthlySnapshot(
                batch=batch, sku=second_sku, source_row=695, month=date(2026, 6, 1),
                status_snapshot="Active", product_snapshot="Report Product", variant_snapshot="White",
                category_snapshot="Apparel", size_snapshot="XL", cogs_snapshot=110000,
                retail_price_snapshot=220000, ending_qty=7, ending_cogs=770000, ending_gross=1540000,
            ),
            MerchandisingMonthlySnapshot(
                batch=batch, sku=second_sku, source_row=695, month=date(2026, 7, 1),
                status_snapshot="Active", product_snapshot="Report Product", variant_snapshot="White",
                category_snapshot="Apparel", size_snapshot="XL", cogs_snapshot=110000,
                retail_price_snapshot=220000, incoming_qty=3, incoming_cogs=330000,
                incoming_gross=660000, beginning_qty=12, beginning_cogs=1320000,
                beginning_gross=2640000, sales_qty=4, sales_cogs=440000,
                sales_gross=880000, sales_net=800000, ending_qty=8,
                ending_cogs=880000, ending_gross=1760000, ratio=3, mos=2,
            ),
        ])
        response = self.client.get(
            "/merchandising/projection/",
            {
                "sku_type": "parent", "month": ["7"],
                "metric": ["incoming", "beginning", "sales", "ending", "stock_ratio", "mos"],
                "submetric": ["qty"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sku_type"], "parent")
        self.assertEqual(response.context["row_identity_label"], "Parent SKU")
        self.assertEqual(response.context["visible_row_count"], 1)
        row = response.context["table_rows"][0]
        self.assertEqual(row["identity"]["sku__sku"], "REPORT-PARENT")
        self.assertEqual(row["identity"]["child_sku_count"], 2)
        self.assertEqual(
            [cell["value"] for cell in row["cells"]],
            [Decimal("20"), Decimal("8"), Decimal("22"), Decimal("6"), Decimal("21"), Decimal("22") / Decimal("6"), Decimal("3.5")],
        )

    def test_projection_defaults_to_sku_type(self):
        response = self.client.get("/merchandising/projection/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sku_type"], "sku")
        self.assertEqual(response.context["row_identity_label"], "SKU")
