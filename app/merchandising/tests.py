from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from audit.models import AuditEvent
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Subcategory, Supplier, Warehouse
from purchasing.models import PPICRequirement, PurchaseOrder, PurchaseOrderLine

from .forms import ProjectionBuilderForm
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
    planning_buffer_incoming,
    select_effective_rule,
)
from .services.workflows import (
    approve_incoming_plan,
    approve_scenario,
    approve_sales_projection,
    create_incoming_plan,
    delete_draft_scenario,
    delete_draft_scenario_items,
    save_scenario_draft,
    update_draft_scenario,
)
from .services.builder import (
    aggregate_preview_by_parent,
    build_draft_matrix,
    recommendation_for,
    summarize_preview,
)
from inventory.services.fifo import post_opening
from inventory.services.fifo import record_inbound
from inventory.models import InventoryMovement
from .services.incoming_actuals import close_incoming_month
from .services.planning_reporting import future_planning_values
from .services.planning_activity import (
    filter_products_by_planning_activity,
    planning_activity_snapshot,
)
from sales.services.manual import create_manual_sale
from sales.models import SalesPlan, SalesPlanningScenario, SalesPlanSKU


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
        self.assertEqual(future_projection("SAME_AS_LAST_MONTH", 100, 0), Decimal("100"))
        self.assertEqual(future_projection("INCREASE_PERCENT", 100, 20), Decimal("120"))
        self.assertEqual(future_projection("DECREASE_PERCENT", 100, 20), Decimal("80"))
        self.assertEqual(
            future_projection("TARGET_STOCK_RATIO", 100, 2, beginning_qty=300),
            Decimal("150"),
        )
        self.assertEqual(
            future_projection(
                "SELL_OUT_ENDING_MONTHS",
                100,
                2,
                previous_ending_qty=101,
            ),
            Decimal("50.5"),
        )

    def test_incoming_formulas(self):
        values = incoming_calculation(100, 30, Decimal("1.5"))
        self.assertEqual(values["minimum"], Decimal("70"))
        self.assertEqual(values["recommended"], Decimal("120.0"))
        self.assertEqual(values["desired_beginning"], Decimal("150.0"))

    def test_planning_buffer_lifts_low_coverage_to_at_least_one_point_five(self):
        self.assertEqual(planning_buffer_incoming(19, 1), Decimal("28"))
        self.assertEqual(planning_buffer_incoming(19, 19), Decimal("10"))
        self.assertEqual(planning_buffer_incoming(19, 20), Decimal("9"))
        self.assertEqual(planning_buffer_incoming(20, 26), Decimal("4"))
        self.assertEqual(planning_buffer_incoming(20, 30), Decimal("0"))
        self.assertEqual(
            planning_buffer_incoming(19, 1, incoming_allowed=False),
            Decimal("0"),
        )

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

    def test_parent_preview_aggregates_children_and_recomputes_metrics(self):
        second_variant = ProductVariant.objects.create(product=self.product, name="White", color="White")
        second_sku = SKU.objects.create(sku="SKU-2", product_variant=second_variant)
        rows = [
            {
                "sku": self.sku,
                "sales_30d": Decimal("3"),
                "inbound_30d": Decimal("2"),
                "activity_ending_qty": Decimal("8"),
                "baseline_qty": Decimal("4"),
                "previous_ending_qty": Decimal("10"),
                "beginning_qty": Decimal("12"),
                "recommendation": Decimal("6"),
            },
            {
                "sku": second_sku,
                "sales_30d": Decimal("5"),
                "inbound_30d": Decimal("1"),
                "activity_ending_qty": Decimal("7"),
                "baseline_qty": Decimal("6"),
                "previous_ending_qty": Decimal("20"),
                "beginning_qty": Decimal("28"),
                "recommendation": Decimal("14"),
            },
        ]
        parent_rows = aggregate_preview_by_parent(rows)
        self.assertEqual(len(parent_rows), 1)
        parent = parent_rows[0]
        self.assertEqual(parent["parent_sku"], "PRODUCT-1")
        self.assertEqual(parent["sku_count"], 2)
        self.assertEqual(parent["baseline_qty"], Decimal("10"))
        self.assertEqual(parent["previous_ending_qty"], Decimal("30"))
        self.assertEqual(parent["beginning_qty"], Decimal("40"))
        self.assertEqual(parent["recommendation"], Decimal("20"))
        self.assertEqual(parent["ending_qty"], Decimal("20"))
        self.assertEqual(parent["stock_ratio"], Decimal("2"))
        self.assertEqual(parent["incoming_gap"], Decimal("0"))
        self.assertEqual(parent["growth_pct"], Decimal("100"))
        summary = summarize_preview(rows)
        self.assertEqual(summary["sku_count"], 2)
        self.assertEqual(summary["recommendation"], Decimal("20"))
        self.assertEqual(summary["ending_qty"], Decimal("20"))
        self.assertEqual(summary["stock_ratio"], Decimal("2"))

    def test_draft_matrix_exposes_growth_vs_prior_month_sales_and_stock_ratio(self):
        projection = self._projection()
        projection.baseline_qty = Decimal("80")
        projection.beginning_qty = Decimal("250")
        projection.cogs_snapshot = Decimal("100000")
        projection.retail_price_snapshot = Decimal("200000")
        projection.net_rate_snapshot = Decimal("0.97")
        projection.save(update_fields=[
            "baseline_qty",
            "beginning_qty",
            "cogs_snapshot",
            "retail_price_snapshot",
            "net_rate_snapshot",
        ])

        sku_rows, headers, summary = build_draft_matrix(
            [projection],
            [date(2026, 9, 1)],
            ["sales", "stock_ratio"],
            grain="sku",
            selected_submetrics=["qty", "cogs", "gross", "net"],
        )
        parent_rows, _, _ = build_draft_matrix(
            [projection],
            [date(2026, 9, 1)],
            ["sales", "stock_ratio"],
            grain="parent_sku",
            selected_submetrics=["qty", "cogs", "gross", "net"],
        )

        self.assertEqual(
            [(header["metric"], header["submetric"]) for header in headers],
            [
                ("sales", "qty"),
                ("sales", "cogs"),
                ("sales", "gross"),
                ("sales", "net"),
                ("stock_ratio", None),
            ],
        )
        self.assertEqual(sku_rows[0]["cells"][0]["growth_pct"], Decimal("25"))
        self.assertEqual(parent_rows[0]["cells"][0]["growth_pct"], Decimal("25"))
        self.assertEqual(summary["cells"][0]["growth_pct"], Decimal("25"))
        self.assertEqual(sku_rows[0]["cells"][0]["value"], Decimal("100"))
        self.assertEqual(sku_rows[0]["cells"][1]["value"], Decimal("10000000"))
        self.assertEqual(sku_rows[0]["cells"][2]["value"], Decimal("20000000"))
        self.assertEqual(sku_rows[0]["cells"][3]["value"], Decimal("19400000"))
        self.assertEqual(sku_rows[0]["cells"][4]["value"], Decimal("2.5"))

    def test_draft_matrix_marks_no_incoming_product_for_read_only_incoming(self):
        self.product.status.name = "Discontinue"
        self.product.status.save(update_fields=["name"])
        projection = self._projection()
        projection.beginning_qty = Decimal("200")
        projection.save(update_fields=["beginning_qty"])

        rows, _, _ = build_draft_matrix(
            [projection],
            [date(2026, 9, 1)],
            ["sales", "incoming_recommendation"],
            grain="sku",
            selected_submetrics=["qty"],
        )

        sales_cell, incoming_cell = rows[0]["cells"]
        self.assertFalse(sales_cell["incoming_allowed"])
        self.assertFalse(incoming_cell["incoming_allowed"])
        self.assertEqual(incoming_cell["value"], Decimal("0"))

    def test_rule_priority_product_over_category_over_status(self):
        rules = [
            ProjectionRule.objects.create(
                scenario=self.scenario,
                target_month=date(2026, 9, 1),
                scope_type=ProjectionRule.ScopeType.ALL_PRODUCTS,
                method=ProjectionRule.Method.INCREASE_PERCENT,
                parameter=Decimal("2"),
                created_by=self.user,
            ),
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
        self.assertEqual(len(overridden), 3)

    def test_builder_defaults_to_all_products_but_respects_active_filters(self):
        default_form = ProjectionBuilderForm()
        self.assertEqual(default_form["scope_type"].value(), ProjectionRule.ScopeType.ALL_PRODUCTS)

        form = ProjectionBuilderForm(
            data={
                "scenario": self.scenario.id,
                "target_month": "2026-09",
                "scope_type": ProjectionRule.ScopeType.ALL_PRODUCTS,
                "product_status": self.status.id,
                "category": self.category.id,
                "product": [self.product.id],
                "planning_activity": "ALL",
                "method": ProjectionRule.Method.INCREASE_PERCENT,
                "parameter": "10",
                "reason": "All active products",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["scope_type"], ProjectionRule.ScopeType.PRODUCT)
        self.assertEqual(form.cleaned_data["product_status"], self.status)
        self.assertEqual(form.cleaned_data["category"], self.category)
        self.assertEqual(list(form.cleaned_data["product"]), [self.product])

    def test_new_projection_methods_validate_parameter_rules(self):
        base_data = {
            "scenario": self.scenario.id,
            "target_month": "2026-09",
            "scope_type": ProjectionRule.ScopeType.ALL_PRODUCTS,
            "planning_activity": "ALL",
            "method": ProjectionRule.Method.SAME_AS_LAST_MONTH,
            "reason": "Method validation",
        }
        same_form = ProjectionBuilderForm(data=base_data)
        self.assertTrue(same_form.is_valid(), same_form.errors)
        self.assertEqual(same_form.cleaned_data["parameter"], Decimal("0"))

        sell_out_form = ProjectionBuilderForm(data={
            **base_data,
            "method": ProjectionRule.Method.SELL_OUT_ENDING_MONTHS,
            "parameter": "2",
        })
        self.assertTrue(sell_out_form.is_valid(), sell_out_form.errors)
        self.assertEqual(sell_out_form.cleaned_data["parameter"], Decimal("2"))

        fractional_form = ProjectionBuilderForm(data={
            **base_data,
            "method": ProjectionRule.Method.SELL_OUT_ENDING_MONTHS,
            "parameter": "2.5",
        })
        self.assertFalse(fractional_form.is_valid())
        self.assertIn("Jumlah bulan harus bilangan bulat", str(fractional_form.errors))

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

    def test_future_recommendation_is_whole_unit_and_incoming_updates_stock_chain(self):
        target_month = date(2026, 9, 1)
        result = recommendation_for(
            sku=self.sku,
            target_month=target_month,
            method=ProjectionRule.Method.INCREASE_PERCENT,
            parameter=Decimal("5"),
            today=date(2026, 8, 22),
            official_current_value={"sales_qty": Decimal("38"), "ending_qty": Decimal("0")},
        )
        self.assertEqual(result["baseline_qty"], Decimal("38"))
        self.assertEqual(result["previous_ending_qty"], Decimal("0"))
        self.assertEqual(result["incoming_qty"], Decimal("0"))
        self.assertEqual(result["beginning_qty"], Decimal("0"))
        self.assertEqual(result["recommendation"], Decimal("40"))
        self.assertEqual(result["incoming_gap"], Decimal("60"))
        self.assertEqual(result["planned_beginning_qty"], Decimal("60"))
        self.assertEqual(result["ending_qty"], Decimal("20"))
        self.assertEqual(result["stock_ratio"], Decimal("1.5"))

    def test_next_month_uses_prior_draft_in_same_scenario_without_approval(self):
        september_projection = SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            baseline_month=date(2026, 8, 1),
            baseline_qty=Decimal("80"),
            beginning_qty=Decimal("50"),
            system_recommendation=Decimal("100"),
        )
        IncomingPlan.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            sales_projection=september_projection,
            prior_ending_qty=Decimal("50"),
            minimum_incoming=Decimal("50"),
            recommended_incoming=Decimal("70"),
        )

        result = recommendation_for(
            sku=self.sku,
            scenario=self.scenario,
            target_month=date(2026, 10, 1),
            method=ProjectionRule.Method.INCREASE_PERCENT,
            parameter=Decimal("5"),
            today=date(2026, 8, 22),
            official_current_value={"sales_qty": Decimal("80"), "ending_qty": Decimal("50")},
        )

        self.assertEqual(result["baseline_month"], date(2026, 9, 1))
        self.assertEqual(result["baseline_qty"], Decimal("100"))
        self.assertEqual(result["beginning_qty"], Decimal("20"))
        self.assertEqual(result["recommendation"], Decimal("105"))
        self.assertEqual(result["growth_pct"], Decimal("5"))
        self.assertEqual(result["incoming_gap"], Decimal("138"))

    def test_same_month_and_sell_out_methods_use_correct_future_baselines(self):
        target_month = date(2026, 9, 1)
        same_result = recommendation_for(
            sku=self.sku,
            target_month=target_month,
            method=ProjectionRule.Method.SAME_AS_LAST_MONTH,
            parameter=Decimal("0"),
            today=date(2026, 8, 22),
            official_current_value={"sales_qty": Decimal("38"), "ending_qty": Decimal("101")},
        )
        self.assertEqual(same_result["recommendation"], Decimal("38"))

        sell_out_result = recommendation_for(
            sku=self.sku,
            target_month=target_month,
            method=ProjectionRule.Method.SELL_OUT_ENDING_MONTHS,
            parameter=Decimal("2"),
            today=date(2026, 8, 22),
            official_current_value={"sales_qty": Decimal("38"), "ending_qty": Decimal("101")},
        )
        self.assertEqual(sell_out_result["previous_ending_qty"], Decimal("101"))
        self.assertEqual(sell_out_result["recommendation"], Decimal("51"))

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

    def test_scenario_draft_allows_moq_adjustment_then_approves_everything_atomically(self):
        projection = self._projection()
        projection.beginning_qty = Decimal("30")
        projection.save(update_fields=["beginning_qty"])

        save_scenario_draft(
            self.scenario.id,
            self.user,
            sales_values={str(projection.id): "100"},
            incoming_values={str(projection.id): "130"},
            reason="MOQ vendor 130",
        )
        projection.refresh_from_db()
        plan = IncomingPlan.objects.get(sales_projection=projection)
        self.assertEqual(projection.proposed_qty, Decimal("100"))
        self.assertEqual(plan.minimum_incoming, Decimal("120"))
        self.assertEqual(plan.recommended_incoming, Decimal("120"))
        self.assertEqual(plan.proposed_incoming, Decimal("130"))
        self.assertEqual(plan.approval_status, IncomingPlan.ApprovalStatus.DRAFT)

        approve_scenario(self.scenario.id, self.user, reason="Approve one scenario")
        self.scenario.refresh_from_db()
        projection.refresh_from_db()
        plan.refresh_from_db()
        requirement = PPICRequirement.objects.get(incoming_plan=plan)
        self.assertEqual(self.scenario.status, ProjectionScenario.Status.APPROVED)
        self.assertEqual(projection.final_approved_qty, Decimal("100"))
        self.assertEqual(plan.final_approved_incoming, Decimal("130"))
        self.assertEqual(requirement.approved_qty, Decimal("130"))

    def test_scenario_approval_rolls_back_when_a_target_month_is_missing(self):
        self.scenario.end_month = date(2026, 10, 1)
        self.scenario.save(update_fields=["end_month"])
        projection = self._projection()
        projection.beginning_qty = Decimal("30")
        projection.save(update_fields=["beginning_qty"])

        with self.assertRaises(ValidationError):
            approve_scenario(self.scenario.id, self.user)

        self.scenario.refresh_from_db()
        projection.refresh_from_db()
        self.assertEqual(self.scenario.status, ProjectionScenario.Status.DRAFT)
        self.assertEqual(projection.approval_status, SalesProjection.ApprovalStatus.DRAFT)
        self.assertFalse(IncomingPlan.objects.filter(scenario=self.scenario).exists())

    def test_draft_scenario_with_planning_rows_can_be_deleted_with_audit(self):
        projection = self._projection()
        projection.beginning_qty = Decimal("30")
        projection.save(update_fields=["beginning_qty"])
        save_scenario_draft(self.scenario.id, self.user)
        scenario_id = self.scenario.id

        deleted = delete_draft_scenario(
            scenario_id,
            self.user,
            "Draft tidak digunakan",
        )

        self.assertEqual(deleted["projection_count"], 1)
        self.assertEqual(deleted["incoming_plan_count"], 1)
        self.assertFalse(ProjectionScenario.objects.filter(pk=scenario_id).exists())
        self.assertFalse(SalesProjection.objects.filter(pk=projection.id).exists())
        audit = AuditEvent.objects.get(
            action="projection_scenario_draft_deleted",
            entity_id=str(scenario_id),
        )
        self.assertEqual(audit.before_values["name"], "September 2026")
        self.assertTrue(audit.after_values["deleted"])

    def test_draft_scenario_with_legacy_approved_row_cannot_be_deleted(self):
        projection = self._projection()
        approve_sales_projection(projection.id, 100, self.user)

        with self.assertRaises(ValidationError):
            delete_draft_scenario(self.scenario.id, self.user)

        self.assertTrue(ProjectionScenario.objects.filter(pk=self.scenario.id).exists())
        self.assertTrue(SalesProjection.objects.filter(pk=projection.id).exists())

    def test_selected_sku_is_deleted_from_every_draft_month_with_audit(self):
        self.scenario.end_month = date(2026, 10, 1)
        self.scenario.save(update_fields=["end_month"])
        selected_rule = ProjectionRule.objects.create(
            scenario=self.scenario,
            target_month=date(2026, 9, 1),
            scope_type=ProjectionRule.ScopeType.PRODUCT,
            product=self.product,
            method=ProjectionRule.Method.SAME_AS_LAST_MONTH,
            parameter=Decimal("0"),
            created_by=self.user,
        )
        selected = []
        for month in (date(2026, 9, 1), date(2026, 10, 1)):
            selected.append(
                SalesProjection.objects.create(
                    scenario=self.scenario,
                    month=month,
                    sku=self.sku,
                    applied_rule=selected_rule,
                    beginning_qty=Decimal("30"),
                    system_recommendation=Decimal("10"),
                )
            )
        other_product = Product.objects.create(
            code="PRODUCT-OTHER",
            name="Product Other",
            status=self.status,
            category=self.category,
        )
        other_variant = ProductVariant.objects.create(product=other_product, name="Black", color="Black")
        other_sku = SKU.objects.create(sku="SKU-OTHER", product_variant=other_variant)
        preserved = SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=other_sku,
            beginning_qty=Decimal("20"),
            system_recommendation=Decimal("5"),
        )
        save_scenario_draft(self.scenario.id, self.user)

        deleted = delete_draft_scenario_items(
            self.scenario.id,
            self.user,
            grain="sku",
            identifiers=[self.sku.id],
        )

        self.assertEqual(deleted["deleted_projection_count"], 2)
        self.assertEqual(deleted["deleted_incoming_plan_count"], 2)
        self.assertFalse(SalesProjection.objects.filter(id__in=[row.id for row in selected]).exists())
        self.assertFalse(IncomingPlan.objects.filter(sku=self.sku, scenario=self.scenario).exists())
        self.assertTrue(SalesProjection.objects.filter(pk=preserved.id).exists())
        self.assertFalse(ProjectionRule.objects.filter(pk=selected_rule.id).exists())
        audit = AuditEvent.objects.get(action="projection_scenario_draft_items_deleted")
        self.assertEqual(audit.before_values["grain"], "sku")
        self.assertEqual(audit.before_values["months"], ["2026-09-01", "2026-10-01"])

    def test_selected_parent_sku_deletes_all_child_skus_but_preserves_other_parent(self):
        self.product.parent_sku = "PARENT-ONE"
        self.product.save(update_fields=["parent_sku"])
        second_variant = ProductVariant.objects.create(product=self.product, name="White", color="White")
        second_sku = SKU.objects.create(sku="SKU-2", product_variant=second_variant)
        for sku in (self.sku, second_sku):
            SalesProjection.objects.create(
                scenario=self.scenario,
                month=date(2026, 9, 1),
                sku=sku,
                system_recommendation=Decimal("10"),
            )
        other_product = Product.objects.create(
            code="PRODUCT-TWO",
            parent_sku="PARENT-TWO",
            name="Product Two",
            status=self.status,
            category=self.category,
        )
        other_variant = ProductVariant.objects.create(product=other_product, name="Black", color="Black")
        other_sku = SKU.objects.create(sku="SKU-3", product_variant=other_variant)
        preserved = SalesProjection.objects.create(
            scenario=self.scenario,
            month=date(2026, 9, 1),
            sku=other_sku,
            system_recommendation=Decimal("5"),
        )

        deleted = delete_draft_scenario_items(
            self.scenario.id,
            self.user,
            grain="parent_sku",
            identifiers=["PARENT-ONE"],
        )

        self.assertEqual(deleted["deleted_projection_count"], 2)
        self.assertFalse(self.scenario.projections.filter(sku__in=[self.sku, second_sku]).exists())
        self.assertTrue(SalesProjection.objects.filter(pk=preserved.id).exists())

    def test_used_draft_scenario_can_be_edited_without_excluding_linked_months(self):
        self._projection()
        updated = update_draft_scenario(
            self.scenario.id,
            self.user,
            name="August to October Planning",
            start_month=date(2026, 8, 1),
            end_month=date(2026, 10, 1),
        )
        self.assertEqual(updated.name, "August to October Planning")
        self.assertEqual(updated.start_month, date(2026, 8, 1))
        self.assertEqual(updated.end_month, date(2026, 10, 1))

        with self.assertRaises(ValidationError):
            update_draft_scenario(
                self.scenario.id,
                self.user,
                name="October Only",
                start_month=date(2026, 10, 1),
                end_month=date(2026, 10, 1),
            )

        updated.refresh_from_db()
        self.assertEqual(updated.name, "August to October Planning")
        self.assertEqual(updated.start_month, date(2026, 8, 1))


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
        self.assertEqual(actual.actual_cogs, Decimal("300000"))
        self.assertEqual(actual.projected_cogs, Decimal("500000"))
        self.assertEqual(actual.actual_ending_qty, Decimal("3"))
        self.assertEqual(actual.actual_ending_cogs, Decimal("300000"))
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
        dashboard = self.client.get("/merchandising/dashboard/")
        dashboard_rows = {row["label"]: row for row in dashboard.context["table_rows"]}
        self.assertEqual(dashboard_rows["Incoming COGS"]["values"][7], Decimal("300000"))
        self.assertEqual(dashboard_rows["Ending Stock COGS"]["values"][7], Decimal("300000"))

    def test_future_cost_uses_sales_projection_snapshot_and_ignores_po_cost(self):
        scenario = ProjectionScenario.objects.create(
            name="September Cost Snapshot",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            baseline_month=date(2026, 8, 1),
            baseline_qty=Decimal("10"),
            beginning_qty=Decimal("10"),
            system_recommendation=Decimal("4"),
            cogs_snapshot=Decimal("125000"),
            retail_price_snapshot=Decimal("225000"),
        )
        values, _ = future_planning_values(
            sku_ids=[self.sku.id],
            planning_year=2026,
            current_month_number=8,
            prior_ending_by_sku={self.sku.id: Decimal("10")},
            price_by_sku={self.sku.id: {"cogs": Decimal("100000"), "retail": Decimal("200000")}},
        )
        september = values[(self.sku.id, 9)]
        self.assertEqual(september["sales_cogs"], Decimal("500000"))
        self.assertEqual(september["ending_cogs"], Decimal("750000"))

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
        self.assertEqual(rows["Beginning Gross"]["values"][8:], [Decimal("3600000")] * 4)
        self.assertEqual(rows["Sales Gross"]["values"][8:], [Decimal("0")] * 4)
        self.assertEqual(rows["Ending Stock Gross"]["values"][8:], [Decimal("3600000")] * 4)
        self.assertEqual(rows["GPM"]["values"][8:], [Decimal("0")] * 4)
        self.assertEqual(rows["GPM"]["values"][0], Decimal("160000"))
        self.assertEqual(rows["GPM Rate"]["values"][0], Decimal("0.4"))
        self.assertEqual(rows["Margin Ratio"]["values"][0], Decimal("1.8"))
        self.assertEqual(rows["Incoming Capital Turnover"]["values"][0], Decimal("0.8"))
        self.assertEqual(rows["Stock Value Ratio"]["values"][0], Decimal("5"))
        self.assertEqual(rows["Stock Value Ratio"]["kind"], "ratio2")
        self.assertEqual(rows["Margin Ratio"]["kind"], "ratio2")
        self.assertEqual(rows["Incoming Capital Turnover"]["kind"], "ratio2")
        self.assertEqual(rows["Sales Gross"]["total"], Decimal("2800000"))

    def test_draft_scenario_is_visible_in_projection_and_dashboard_with_warning(self):
        scenario = ProjectionScenario.objects.create(
            name="September Draft Preview",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        projection = SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            baseline_month=date(2026, 8, 1),
            baseline_qty=Decimal("0"),
            beginning_qty=Decimal("18"),
            system_recommendation=Decimal("25"),
        )
        save_scenario_draft(
            scenario.id,
            self.user,
            incoming_values={str(projection.id): "20"},
            reason="MOQ preview",
        )

        projection_response = self.client.get(
            "/merchandising/projection/",
            {
                "month": ["9"],
                "metric": ["incoming", "beginning", "sales", "ending", "stock_ratio", "mos"],
                "submetric": ["qty"],
            },
        )
        self.assertEqual(projection_response.context["planning_preview"]["draft_scenario_count"], 1)
        self.assertContains(
            projection_response,
            "Data ini mengandung 1 Scenario Projection Draft yang belum disetujui.",
        )
        self.assertEqual(
            [cell["value"] for cell in projection_response.context["table_rows"][0]["cells"]],
            [
                Decimal("18"), Decimal("20"), Decimal("38"), Decimal("25"),
                Decimal("13"), Decimal("1.52"), Decimal("0.52"),
            ],
        )

        dashboard_response = self.client.get("/merchandising/dashboard/")
        self.assertEqual(dashboard_response.context["planning_preview"]["draft_scenario_count"], 1)
        self.assertContains(
            dashboard_response,
            "Data ini mengandung 1 Scenario Projection Draft yang belum disetujui.",
        )
        rows = {row["label"]: row for row in dashboard_response.context["table_rows"]}
        self.assertEqual(rows["Incoming Gross"]["values"][8], Decimal("4000000"))
        self.assertEqual(rows["Beginning Gross"]["values"][8], Decimal("7600000"))
        self.assertEqual(rows["Sales Gross"]["values"][8], Decimal("5000000"))
        self.assertEqual(rows["Sales COGS"]["values"][8], Decimal("2500000"))
        self.assertEqual(rows["Sales Net"]["values"][8], Decimal("4850000"))
        self.assertEqual(rows["Ending Stock Gross"]["values"][8], Decimal("2600000"))
        self.assertEqual(rows["GPM"]["values"][8], Decimal("2350000"))
        self.assertEqual(rows["GPM Rate"]["values"][8], Decimal("0.47"))
        self.assertEqual(rows["Margin Ratio"]["values"][8], Decimal("1.94"))
        self.assertEqual(rows["Incoming Capital Turnover"]["values"][8], Decimal("2.5"))
        self.assertFalse(PPICRequirement.objects.filter(incoming_plan__scenario=scenario).exists())

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
            ["Ending QTY", "Target Sales QTY", "Sales QTY", "Ending QTY"],
        )
        self.assertTrue(response.context["dynamic_headers"][0]["is_auto_previous"])
        self.assertEqual(response.context["dynamic_headers"][0]["month"], "Jul")
        self.assertEqual(response.context["table_rows"][0]["cells"][0]["value"], Decimal("13"))
        self.assertIsNone(response.context["table_rows"][0]["cells"][1]["value"])
        self.assertEqual(response.context["table_rows"][0]["cells"][2]["value"], Decimal("0"))
        self.assertEqual(response.context["table_rows"][0]["cells"][3]["value"], Decimal("18"))
        self.assertEqual(response.context["visible_row_count"], 1)
        self.assertContains(response, "seluruh hasil dalam satu halaman")
        self.assertNotContains(response, "50 SKU per halaman")
        self.assertContains(response, "menunggu Projection Builder")
        self.assertEqual(response.context["selected_detail_columns"], [])
        self.assertNotContains(response, "<th>Status</th>", html=True)
        self.assertNotContains(response, "<th>Retail Price</th>", html=True)

    def test_projection_product_detail_columns_are_opt_in(self):
        response = self.client.get(
            "/merchandising/projection/",
            {
                "month": ["8"],
                "metric": ["sales"],
                "submetric": ["qty"],
                "detail": ["status", "category", "cogs"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["selected_detail_columns"],
            ["status", "category", "cogs"],
        )
        self.assertEqual(response.context["identity_summary_colspan"], 4)
        self.assertContains(response, "<th>Status</th>", html=True)
        self.assertContains(response, "<th>Category</th>", html=True)
        self.assertContains(response, "<th>COGS</th>", html=True)
        self.assertNotContains(response, "<th>Variant</th>", html=True)
        self.assertNotContains(response, "<th>Retail Price</th>", html=True)
        self.assertContains(response, "<td>Active</td>", html=True)
        self.assertContains(response, "<td>Apparel</td>", html=True)
        self.assertContains(response, "<td>Rp 100.000</td>", html=True)

    def test_projection_current_month_shows_builder_target_before_live_sales_projection(self):
        current_month = timezone.localdate().replace(day=1)
        scenario = ProjectionScenario.objects.create(
            name="Current Month Target",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        SalesProjection.objects.create(
            scenario=scenario,
            month=current_month,
            sku=self.sku,
            baseline_month=date(2026, 7, 1),
            baseline_qty=Decimal("2"),
            beginning_qty=Decimal("18"),
            system_recommendation=Decimal("11"),
        )

        response = self.client.get(
            "/merchandising/projection/",
            {
                "month": [str(current_month.month)],
                "metric": ["sales"],
                "submetric": ["qty"],
            },
        )

        self.assertEqual(
            [header["label"] for header in response.context["dynamic_headers"]],
            ["Ending QTY", "Target Sales QTY", "Sales QTY"],
        )
        self.assertEqual(
            [cell["value"] for cell in response.context["table_rows"][0]["cells"]],
            [Decimal("13"), Decimal("11"), Decimal("0")],
        )
        self.assertEqual(response.context["planning_preview"]["draft_scenario_count"], 1)
        self.assertContains(response, "Current Month Target")

    def test_projection_product_options_follow_selected_category(self):
        knitwear = Category.objects.create(code="REPORT-KNIT", name="Knitwear")
        knit_product = Product.objects.create(
            code="REPORT-KNIT-PRODUCT",
            parent_sku="REPORT-KNIT-PARENT",
            name="Knit Product",
            status=self.product.status,
            category=knitwear,
        )
        knit_variant = ProductVariant.objects.create(
            product=knit_product,
            name="Black",
            color="Black",
        )
        knit_sku = SKU.objects.create(
            sku="REPORT-KNIT-SKU",
            product_variant=knit_variant,
            current_retail_price=Decimal("300000"),
            current_master_cogs=Decimal("150000"),
        )
        MerchandisingMonthlySnapshot.objects.bulk_create([
            MerchandisingMonthlySnapshot(
                batch=self.batch,
                sku=knit_sku,
                source_row=695,
                month=date(2026, month, 1),
                status_snapshot="Active",
                product_snapshot="Knit Product",
                category_snapshot="Knitwear",
                cogs_snapshot=150000,
                retail_price_snapshot=300000,
                ending_qty=10,
                ending_cogs=1500000,
                ending_gross=3000000,
            )
            for month in range(1, 13)
        ])

        response = self.client.get(
            "/merchandising/projection/",
            {"category": ["Knitwear"], "month": ["8"], "product": ["Report Product"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["categories"], ["Apparel", "Knitwear"])
        self.assertEqual(response.context["products"], ["Knit Product"])
        self.assertEqual(response.context["selected"]["category"], ["Knitwear"])
        self.assertEqual(response.context["selected"]["product"], [])
        self.assertEqual(response.context["visible_row_count"], 1)
        self.assertEqual(
            response.context["table_rows"][0]["identity"]["product_snapshot"],
            "Knit Product",
        )
        self.assertContains(response, 'value="Knit Product"')
        self.assertNotContains(response, 'value="Report Product"')

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
            {"month": ["9", "10"], "metric": ["incoming", "sales", "ending"], "submetric": ["qty"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dynamic_headers"][0]["month"], "Aug")
        self.assertTrue(response.context["dynamic_headers"][0]["is_auto_previous"])
        self.assertNotIn(
            "Target Sales QTY",
            [header["label"] for header in response.context["dynamic_headers"]],
        )
        self.assertEqual(
            [cell["value"] for cell in response.context["table_rows"][0]["cells"]],
            [
                Decimal("18"), Decimal("0"), Decimal("0"), Decimal("18"),
                Decimal("0"), Decimal("0"), Decimal("18"),
            ],
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

    def test_planning_builder_is_a_separate_subtab_and_owns_scenario_workflow(self):
        session = self.client.session
        session["active_module"] = "operation"
        session.save()

        projection_response = self.client.get("/merchandising/projection/")
        self.assertEqual(projection_response.status_code, 200)
        self.assertContains(projection_response, "Projection matrix")
        self.assertNotContains(projection_response, "Buat periode planning")
        self.assertContains(projection_response, "Planning Builder")

        builder_response = self.client.get("/merchandising/planning-builder/")
        self.assertEqual(builder_response.status_code, 200)
        self.assertContains(builder_response, "Planning Builder")
        self.assertContains(builder_response, "Buat periode planning")
        self.assertContains(builder_response, "Incoming Plan → PPIC")
        self.assertContains(builder_response, 'type="month" name="start_month"')
        self.assertContains(builder_response, 'type="month" name="end_month"')
        self.assertContains(builder_response, 'type="month" name="target_month"')
        self.assertContains(builder_response, 'type="hidden" name="scope_type"')
        self.assertNotContains(builder_response, "Scope type:")

        create_response = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "scenario",
                "name": "October Planning UAT",
                "start_month": "2026-10",
                "end_month": "2026-12",
            },
        )
        self.assertRedirects(create_response, "/merchandising/planning-builder/")
        scenario = ProjectionScenario.objects.get(name="October Planning UAT")
        self.assertEqual(scenario.start_month, date(2026, 10, 1))
        self.assertEqual(scenario.end_month, date(2026, 12, 1))

    def test_cancel_preview_discards_unsaved_builder_result(self):
        scenario = ProjectionScenario.objects.create(
            name="Cancel Preview UAT",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        response = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "builder",
                "action": "cancel",
                "scenario": scenario.id,
                "target_month": "2026-09",
            },
        )
        self.assertRedirects(
            response,
            "/merchandising/planning-builder/",
            fetch_redirect_response=False,
        )
        self.assertFalse(ProjectionRule.objects.filter(scenario=scenario).exists())
        self.assertFalse(SalesProjection.objects.filter(scenario=scenario).exists())
        follow_up = self.client.get("/merchandising/planning-builder/")
        self.assertContains(follow_up, "Preview dibatalkan. Tidak ada Draft Projection yang disimpan.")
        self.assertNotContains(follow_up, 'name="action" value="cancel"')

    def test_planning_builder_filter_options_cascade_status_category_and_product(self):
        essential = ProductStatus.objects.create(code="ESSENTIAL-PLUS", name="Essential+")
        shirt = Category.objects.create(code="SHIRT-CASCADE", name="Shirt Cascade")
        jacket = Category.objects.create(code="JACKET-CASCADE", name="Jacket Cascade")
        shirt_subcategory = Subcategory.objects.create(
            category=shirt,
            code="TSHIRT-CASCADE",
            name="T-Shirt Cascade",
        )
        essential_product = Product.objects.create(
            code="ESSENTIAL-SHIRT",
            name="Essential Shirt",
            status=essential,
            category=shirt,
            subcategory=shirt_subcategory,
        )
        Product.objects.create(
            code="ACTIVE-JACKET",
            name="Active Jacket",
            status=self.product.status,
            category=jacket,
        )

        response = self.client.get(
            "/merchandising/planning-builder/filter-options/",
            {"product_status": essential.id, "category": jacket.id, "planning_activity": "ALL"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["categories"], [{"id": str(shirt.id), "name": "Shirt Cascade"}])
        self.assertFalse(payload["selected_category_valid"])
        self.assertEqual(
            payload["subcategories"],
            [{"id": str(shirt_subcategory.id), "name": "T-Shirt Cascade"}],
        )
        self.assertFalse(payload["selected_subcategory_valid"])
        self.assertEqual(payload["products"], [{"id": str(essential_product.id), "name": "Essential Shirt"}])

        response = self.client.get(
            "/merchandising/planning-builder/filter-options/",
            {
                "product_status": essential.id,
                "category": shirt.id,
                "subcategory": shirt_subcategory.id,
                "planning_activity": "ALL",
            },
        )
        payload = response.json()
        self.assertTrue(payload["selected_category_valid"])
        self.assertTrue(payload["selected_subcategory_valid"])
        self.assertEqual(payload["products"], [{"id": str(essential_product.id), "name": "Essential Shirt"}])

    def test_all_products_respects_subcategory_intersection(self):
        category = Category.objects.create(code="SUBCAT-FILTER", name="Subcategory Filter")
        tops = Subcategory.objects.create(category=category, code="TOPS", name="Tops")
        outerwear = Subcategory.objects.create(category=category, code="OUTER", name="Outerwear")

        included = Product.objects.create(
            code="SUBCAT-INCLUDED",
            name="Subcategory Included",
            status=self.product.status,
            category=category,
            subcategory=tops,
        )
        included_variant = ProductVariant.objects.create(product=included, name="Black", color="Black")
        SKU.objects.create(
            sku="SUBCAT-INCLUDED-SKU",
            product_variant=included_variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        excluded = Product.objects.create(
            code="SUBCAT-EXCLUDED",
            name="Subcategory Excluded",
            status=self.product.status,
            category=category,
            subcategory=outerwear,
        )
        excluded_variant = ProductVariant.objects.create(product=excluded, name="Black", color="Black")
        SKU.objects.create(
            sku="SUBCAT-EXCLUDED-SKU",
            product_variant=excluded_variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        current_month = timezone.localdate().replace(day=1)
        scenario = ProjectionScenario.objects.create(
            name="Subcategory Intersection",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        response = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "builder",
                "scenario": scenario.id,
                "target_month": current_month.strftime("%Y-%m"),
                "scope_type": ProjectionRule.ScopeType.ALL_PRODUCTS,
                "product_status": self.product.status_id,
                "category": category.id,
                "subcategory": tops.id,
                "planning_activity": "ALL",
                "method": ProjectionRule.Method.INCREASE_PERCENT,
                "parameter": "0",
                "action": "preview",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 Product · 1 SKU terdampak")
        self.assertContains(response, "SUBCAT-INCLUDED-SKU")
        self.assertNotContains(response, "SUBCAT-EXCLUDED-SKU")

    def test_drafted_product_is_unavailable_only_for_the_same_target_month(self):
        available_product = Product.objects.create(
            code="AVAILABLE-PRODUCT",
            name="Available Product",
            status=self.product.status,
            category=self.product.category,
        )
        available_variant = ProductVariant.objects.create(
            product=available_product,
            name="Black",
            color="Black",
        )
        SKU.objects.create(
            sku="AVAILABLE-SKU",
            product_variant=available_variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        current_month = timezone.localdate().replace(day=1)
        next_target = date(
            current_month.year + (1 if current_month.month == 12 else 0),
            1 if current_month.month == 12 else current_month.month + 1,
            1,
        )
        drafted_scenario = ProjectionScenario.objects.create(
            name="Existing Draft",
            start_month=current_month,
            end_month=next_target,
            created_by=self.user,
        )
        SalesProjection.objects.create(
            scenario=drafted_scenario,
            month=current_month,
            sku=self.sku,
            system_recommendation=Decimal("1"),
        )

        current_options = self.client.get(
            "/merchandising/planning-builder/filter-options/",
            {"target_month": current_month.strftime("%Y-%m"), "planning_activity": "ALL"},
        ).json()["products"]
        self.assertNotIn(str(self.product.id), {row["id"] for row in current_options})
        self.assertIn(str(available_product.id), {row["id"] for row in current_options})

        next_options = self.client.get(
            "/merchandising/planning-builder/filter-options/",
            {"target_month": next_target.strftime("%Y-%m"), "planning_activity": "ALL"},
        ).json()["products"]
        self.assertIn(str(self.product.id), {row["id"] for row in next_options})

        duplicate_scenario = ProjectionScenario.objects.create(
            name="Duplicate Draft Guard",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        duplicate_response = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "builder",
                "scenario": duplicate_scenario.id,
                "target_month": current_month.strftime("%Y-%m"),
                "scope_type": ProjectionRule.ScopeType.PRODUCT,
                "product": [self.product.id],
                "planning_activity": "ALL",
                "method": ProjectionRule.Method.SAME_AS_LAST_MONTH,
                "action": "preview",
            },
        )
        self.assertContains(duplicate_response, "sudah memiliki Draft Projection")
        self.assertFalse(duplicate_scenario.rules.exists())
        self.assertFalse(duplicate_scenario.projections.exists())

        broad_preview = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "builder",
                "scenario": duplicate_scenario.id,
                "target_month": current_month.strftime("%Y-%m"),
                "scope_type": ProjectionRule.ScopeType.ALL_PRODUCTS,
                "planning_activity": "ALL",
                "method": ProjectionRule.Method.SAME_AS_LAST_MONTH,
                "action": "preview",
            },
        )
        self.assertContains(broad_preview, "Available Product")
        self.assertEqual(
            [row["sku"].sku for row in broad_preview.context["preview_rows"]],
            ["AVAILABLE-SKU"],
        )

    def test_save_scenario_draft_keeps_the_saved_draft_open(self):
        current_month = timezone.localdate().replace(day=1)
        scenario = ProjectionScenario.objects.create(
            name="Save And Close Draft Matrix",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        SalesProjection.objects.create(
            scenario=scenario,
            month=current_month,
            sku=self.sku,
            system_recommendation=Decimal("1"),
        )

        response = self.client.post(
            f"/merchandising/planning-builder/scenario/{scenario.id}/draft/",
            {"action": "save"},
        )

        self.assertRedirects(
            response,
            f"/merchandising/planning-builder/?view_draft={scenario.id}#draft-projection",
        )
        reopened_response = self.client.get(
            "/merchandising/planning-builder/",
            {"view_draft": scenario.id},
        )
        self.assertContains(reopened_response, "SCENARIO DRAFT")
        self.assertContains(reopened_response, "Save And Close Draft Matrix")

    def test_save_draft_ignores_unchanged_invalid_legacy_row(self):
        scenario = ProjectionScenario.objects.create(
            name="Legacy Invalid Row",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        editable = SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            beginning_qty=Decimal("200"),
            system_recommendation=Decimal("100"),
        )
        stale_status = ProductStatus.objects.create(code="DISCONTINUE", name="Discontinue")
        stale_product = Product.objects.create(
            code="STALE-PRODUCT",
            name="Stale Product",
            status=stale_status,
            category=self.product.category,
        )
        stale_variant = ProductVariant.objects.create(
            product=stale_product,
            name="Default",
            color="Default",
        )
        stale_sku = SKU.objects.create(sku="ST345", product_variant=stale_variant)
        stale = SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=stale_sku,
            beginning_qty=Decimal("-31"),
            system_recommendation=Decimal("0"),
        )

        response = self.client.post(
            f"/merchandising/planning-builder/scenario/{scenario.id}/draft/",
            {
                "action": "save",
                f"sales_qty_{editable.id}": "120",
                f"sales_qty_{stale.id}": "0",
            },
        )

        self.assertRedirects(
            response,
            f"/merchandising/planning-builder/?view_draft={scenario.id}#draft-projection",
        )
        editable.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(editable.proposed_qty, Decimal("120"))
        self.assertEqual(stale.proposed_qty, Decimal("0"))

    def test_save_draft_forces_posted_incoming_to_zero_for_no_incoming_product(self):
        scenario = ProjectionScenario.objects.create(
            name="No Incoming Guard",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        self.product.status.name = "Discontinue"
        self.product.status.save(update_fields=["name"])
        projection = SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            beginning_qty=Decimal("200"),
            system_recommendation=Decimal("100"),
        )

        response = self.client.post(
            f"/merchandising/planning-builder/scenario/{scenario.id}/draft/",
            {
                "action": "save",
                f"sales_qty_{projection.id}": "120",
                f"incoming_qty_{projection.id}": "13",
            },
        )

        self.assertRedirects(
            response,
            f"/merchandising/planning-builder/?view_draft={scenario.id}#draft-projection",
        )
        self.assertEqual(
            IncomingPlan.objects.get(sales_projection=projection).proposed_incoming,
            Decimal("0"),
        )

    def test_approval_allows_zero_sales_for_negative_legacy_stock_without_incoming(self):
        scenario = ProjectionScenario.objects.create(
            name="Negative Legacy Stock",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        self.product.status.name = "Discontinue"
        self.product.status.save(update_fields=["name"])
        projection = SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            beginning_qty=Decimal("-31"),
            system_recommendation=Decimal("0"),
        )

        approve_scenario(scenario.id, self.user)

        scenario.refresh_from_db()
        projection.refresh_from_db()
        plan = IncomingPlan.objects.get(sales_projection=projection)
        self.assertEqual(scenario.status, ProjectionScenario.Status.APPROVED)
        self.assertEqual(projection.final_approved_qty, Decimal("0"))
        self.assertEqual(plan.final_approved_incoming, Decimal("0"))

    def test_approval_still_rejects_sales_without_available_stock_or_incoming(self):
        scenario = ProjectionScenario.objects.create(
            name="Invalid No Incoming Sales",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        self.product.status.name = "Discontinue"
        self.product.status.save(update_fields=["name"])
        SalesProjection.objects.create(
            scenario=scenario,
            month=date(2026, 9, 1),
            sku=self.sku,
            beginning_qty=Decimal("-31"),
            system_recommendation=Decimal("1"),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Sales Projection melampaui stock",
        ):
            approve_scenario(scenario.id, self.user)

    def test_scenario_library_edit_button_starts_disabled_until_a_field_changes(self):
        current_month = timezone.localdate().replace(day=1)
        ProjectionScenario.objects.create(
            name="Dirty State Scenario",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )

        response = self.client.get("/merchandising/planning-builder/")

        self.assertContains(response, "data-scenario-edit-form")
        self.assertContains(response, "data-scenario-edit-submit disabled aria-disabled=\"true\"")

    def test_planning_builder_applies_one_product_rule_to_multiple_selected_products(self):
        second_product = Product.objects.create(
            code="REPORT-PRODUCT-SECOND",
            parent_sku="REPORT-PARENT-SECOND",
            name="Report Product Second",
            status=self.product.status,
            category=self.product.category,
        )
        second_variant = ProductVariant.objects.create(product=second_product, name="Black", color="Black")
        SKU.objects.create(
            sku="REPORT-SKU-SECOND",
            product_variant=second_variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        current_month = timezone.localdate().replace(day=1)
        scenario = ProjectionScenario.objects.create(
            name="Multi Product Current Month",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        payload = {
            "form_name": "builder",
            "scenario": scenario.id,
            "target_month": current_month.strftime("%Y-%m"),
            "scope_type": ProjectionRule.ScopeType.PRODUCT,
            "product_status": self.product.status_id,
            "category": self.product.category_id,
            "product": [self.product.id, second_product.id],
            "planning_activity": "ALL",
            "method": ProjectionRule.Method.INCREASE_PERCENT,
            "parameter": "5",
            "reason": "Multi product UAT",
        }

        preview_response = self.client.post(
            "/merchandising/planning-builder/",
            {**payload, "action": "preview"},
        )
        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "2 Product · 2 SKU terdampak")
        self.assertContains(preview_response, "Report Product")
        self.assertContains(preview_response, "Report Product Second")
        self.assertContains(preview_response, "Sales Projection")
        self.assertContains(preview_response, "Growth vs")
        self.assertContains(preview_response, "Incoming Recommendation")
        self.assertContains(preview_response, 'data-incoming-recommendation-input', count=2)
        self.assertContains(
            preview_response,
            f'name="incoming_qty_{self.sku.id}"',
        )
        self.assertContains(preview_response, "data-beginning-cell", count=2)
        self.assertContains(preview_response, 'class="preview-header-actions"')
        self.assertContains(preview_response, "data-preview-table-scroll")
        self.assertContains(preview_response, "Tampilan baris")
        self.assertContains(preview_response, 'value="parent_sku"')
        self.assertContains(preview_response, '#projection-preview')
        self.assertContains(preview_response, 'id="projection-preview"')
        self.assertContains(preview_response, 'class="preview-total-row"', count=2)
        self.assertEqual(len(preview_response.context["preview_parent_rows"]), 2)
        self.assertEqual(preview_response.context["preview_totals"]["sku_count"], 2)
        self.assertNotContains(preview_response, "Activity Ending")
        self.assertNotContains(preview_response, "<th>Incoming Plan</th>", html=True)

        projection_fields = {
            f"projection_qty_{self.sku.id}": "7",
            f"projection_qty_{second_product.variants.get().skus.get().id}": "9",
            f"incoming_qty_{self.sku.id}": "11",
            f"incoming_qty_{second_product.variants.get().skus.get().id}": "14",
        }
        response = self.client.post(
            "/merchandising/planning-builder/",
            {**payload, **projection_fields, "action": "draft"},
        )
        self.assertRedirects(
            response,
            f"/merchandising/planning-builder/?view_draft={scenario.id}#draft-projection",
        )
        self.assertEqual(scenario.rules.filter(scope_type=ProjectionRule.ScopeType.PRODUCT).count(), 2)
        self.assertEqual(scenario.projections.count(), 2)
        first_projection = scenario.projections.get(sku=self.sku)
        second_projection = scenario.projections.exclude(sku=self.sku).get()
        self.assertEqual(first_projection.proposed_qty, Decimal("7"))
        self.assertEqual(second_projection.proposed_qty, Decimal("9"))
        self.assertEqual(first_projection.cogs_snapshot, Decimal("100000"))
        self.assertEqual(first_projection.retail_price_snapshot, Decimal("200000"))
        self.assertEqual(first_projection.net_rate_snapshot, Decimal("0.97"))
        self.assertEqual(first_projection.adit_adjustment, Decimal("7") - first_projection.system_recommendation)
        self.assertEqual(second_projection.adit_adjustment, Decimal("9") - second_projection.system_recommendation)
        first_incoming = IncomingPlan.objects.get(sales_projection=first_projection)
        second_incoming = IncomingPlan.objects.get(sales_projection=second_projection)
        self.assertEqual(first_incoming.proposed_incoming, Decimal("11"))
        self.assertEqual(second_incoming.proposed_incoming, Decimal("14"))

        sales_scenario = SalesPlanningScenario.objects.create(
            name="Sales Target Reference",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        first_sales_plan = SalesPlan.objects.create(
            scenario=sales_scenario,
            month=current_month,
            product=first_projection.sku.product_variant.product,
            quantity_target=70,
        )
        SalesPlanSKU.objects.create(
            plan=first_sales_plan,
            sku=first_projection.sku,
            quantity_target=70,
        )
        second_sales_plan = SalesPlan.objects.create(
            scenario=sales_scenario,
            month=current_month,
            product=second_projection.sku.product_variant.product,
            quantity_target=90,
        )
        SalesPlanSKU.objects.create(
            plan=second_sales_plan,
            sku=second_projection.sku,
            quantity_target=90,
        )
        draft_response = self.client.get(
            "/merchandising/planning-builder/",
            {"view_draft": scenario.id},
        )
        self.assertContains(draft_response, "SCENARIO DRAFT")
        self.assertContains(draft_response, "Save Draft")
        self.assertContains(draft_response, 'form="scenario-draft-form"')
        self.assertContains(draft_response, 'id="scenario-draft-form"')
        self.assertContains(draft_response, "2 Product · 2 SKU")
        self.assertContains(draft_response, "Report Product")
        self.assertContains(draft_response, "Report Product Second")
        self.assertContains(draft_response, "View Draft · 2 SKU")
        self.assertContains(draft_response, 'name="draft_grain"', count=2)
        self.assertContains(draft_response, 'data-draft-grain-panel="sku"')
        self.assertContains(draft_response, 'data-draft-grain-panel="parent_sku"')
        self.assertContains(draft_response, "data-preview-table-scroll", count=2)
        self.assertContains(draft_response, 'class="preview-total-row"', count=2)
        self.assertContains(draft_response, 'data-draft-selection-delete-form')
        self.assertContains(draft_response, 'data-draft-select-all', count=2)
        self.assertContains(draft_response, 'data-draft-row-select', count=4)
        self.assertContains(draft_response, 'data-draft-delete-selected')
        self.assertContains(draft_response, 'title="Delete Selected SKU From Draft"')
        self.assertContains(draft_response, f'value="{first_projection.sku_id}"')
        self.assertContains(draft_response, 'value="REPORT-PARENT"')
        self.assertEqual(len(draft_response.context["draft_parent_rows"]), 2)
        self.assertEqual(draft_response.context["draft_totals"]["sku_row_count"], 2)
        self.assertEqual(draft_response.context["draft_totals"]["parent_row_count"], 2)
        self.assertEqual(draft_response.context["draft_totals"]["proposed_qty"], Decimal("16"))
        self.assertNotIn("month", {header["metric"] for header in draft_response.context["draft_matrix_headers"]})
        self.assertContains(draft_response, "Bulan")
        self.assertContains(draft_response, "Metric")
        self.assertContains(draft_response, "Sub Metric")
        self.assertContains(draft_response, 'name="draft_submetric"', count=4)
        self.assertNotContains(draft_response, "immutable planning price snapshot")

        matrix_response = self.client.get(
            "/merchandising/planning-builder/",
            {
                "view_draft": scenario.id,
                "draft_month": current_month.strftime("%Y-%m"),
                "draft_metric": "sales",
            },
        )
        self.assertEqual(len(matrix_response.context["draft_matrix_headers"]), 5)
        self.assertEqual(
            [header["metric"] for header in matrix_response.context["draft_matrix_headers"]],
            ["historical_sales", "historical_sales", "historical_sales", "sales_target", "sales"],
        )
        self.assertEqual(
            [row["cells"][3]["value"] for row in matrix_response.context["draft_sku_matrix_rows"]],
            [Decimal("70"), Decimal("90")],
        )
        self.assertEqual(
            [row["cells"][4]["value"] for row in matrix_response.context["draft_sku_matrix_rows"]],
            [Decimal("7"), Decimal("9")],
        )
        self.assertEqual(matrix_response.context["draft_matrix_summary"]["cells"][3]["value"], Decimal("160"))
        self.assertEqual(matrix_response.context["draft_matrix_summary"]["cells"][4]["value"], Decimal("16"))
        self.assertContains(matrix_response, "Target Sales (Sales) QTY")
        self.assertEqual(
            [row["cells"][3]["value"] for row in matrix_response.context["draft_parent_matrix_rows"]],
            [Decimal("70"), Decimal("90")],
        )

        financial_matrix_response = self.client.get(
            "/merchandising/planning-builder/",
            {
                "view_draft": scenario.id,
                "draft_month": current_month.strftime("%Y-%m"),
                "draft_metric": "sales",
                "draft_submetric": ["qty", "cogs", "gross", "net"],
            },
        )
        self.assertEqual(
            [header["submetric"] for header in financial_matrix_response.context["draft_matrix_headers"]],
            ["qty", "qty", "qty", "qty", "qty", "cogs", "gross", "net"],
        )
        first_financial_row = next(
            row
            for row in financial_matrix_response.context["draft_sku_matrix_rows"]
            if row["identity"] == first_projection.sku.sku
        )
        self.assertEqual(
            [cell["value"] for cell in first_financial_row["cells"]],
            [
                Decimal("2"), Decimal("2"), Decimal("2"), Decimal("70"), Decimal("7"),
                Decimal("700000"), Decimal("1400000"), Decimal("1358000"),
            ],
        )
        self.assertContains(financial_matrix_response, "Sales Projection COGS")
        self.assertContains(financial_matrix_response, "Rp 1.400.000")

        stock_chain_response = self.client.get(
            "/merchandising/planning-builder/",
            {
                "view_draft": scenario.id,
                "draft_month": current_month.strftime("%Y-%m"),
                "draft_metric": ["beginning", "sales", "ending", "incoming_recommendation"],
            },
        )
        self.assertEqual(
            [header["metric"] for header in stock_chain_response.context["draft_matrix_headers"]],
            [
                "historical_sales", "historical_sales", "historical_sales",
                "beginning", "sales_target", "sales", "ending", "incoming_recommendation",
            ],
        )
        stock_rows = {
            row["identity"]: [cell["value"] for cell in row["cells"]]
            for row in stock_chain_response.context["draft_sku_matrix_rows"]
        }
        self.assertEqual(
            stock_rows[first_projection.sku.sku],
            [
                Decimal("2"), Decimal("2"), Decimal("2"),
                first_projection.beginning_qty + Decimal("11"),
                Decimal("70"),
                Decimal("7"),
                first_projection.beginning_qty + Decimal("11") - Decimal("7"),
                Decimal("11"),
            ],
        )

        delete_response = self.client.post(
            f"/merchandising/planning-builder/scenario/{scenario.id}/draft/items/delete/",
            {
                "selection_grain": "sku",
                "selected_item": [str(first_projection.sku_id)],
            },
        )
        self.assertRedirects(
            delete_response,
            f"/merchandising/planning-builder/?view_draft={scenario.id}#draft-projection",
        )
        self.assertFalse(SalesProjection.objects.filter(pk=first_projection.id).exists())
        self.assertFalse(IncomingPlan.objects.filter(pk=first_incoming.id).exists())
        self.assertTrue(SalesProjection.objects.filter(pk=second_projection.id).exists())

    def test_planning_builder_shows_three_prior_sales_months_in_preview_and_draft(self):
        scenario = ProjectionScenario.objects.create(
            name="September Historical Sales UAT",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        payload = {
            "form_name": "builder",
            "scenario": scenario.id,
            "target_month": "2026-09",
            "scope_type": ProjectionRule.ScopeType.PRODUCT,
            "product_status": self.product.status_id,
            "category": self.product.category_id,
            "product": [self.product.id],
            "planning_activity": "ALL",
            "method": ProjectionRule.Method.TARGET_STOCK_RATIO,
            "parameter": "2",
            "reason": "Historical sales UAT",
        }

        preview = self.client.post(
            "/merchandising/planning-builder/",
            {**payload, "action": "preview"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            preview.context["preview_history_months"],
            [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)],
        )
        preview_row = preview.context["preview_rows"][0]
        self.assertEqual(
            [cell["value"] for cell in preview_row["history_cells"]],
            [Decimal("2"), Decimal("2"), preview_row["baseline_qty"]],
        )
        self.assertContains(preview, "Historical Sales QTY Juni")
        self.assertContains(preview, "Historical Sales QTY Juli")
        self.assertContains(preview, "Baseline Sales QTY Agustus")

        draft = self.client.post(
            "/merchandising/planning-builder/",
            {**payload, "action": "draft"},
        )
        self.assertRedirects(
            draft,
            f"/merchandising/planning-builder/?view_draft={scenario.id}#draft-projection",
        )
        projection = scenario.projections.get(sku=self.sku, month=date(2026, 9, 1))
        draft_view = self.client.get(
            "/merchandising/planning-builder/",
            {
                "view_draft": scenario.id,
                "draft_month": "2026-09",
                "draft_metric": "sales",
            },
        )
        headers = draft_view.context["draft_matrix_headers"]
        self.assertEqual(
            [(header["month"], header["metric"]) for header in headers[:3]],
            [
                (date(2026, 6, 1), "historical_sales"),
                (date(2026, 7, 1), "historical_sales"),
                (date(2026, 8, 1), "historical_sales"),
            ],
        )
        self.assertEqual(
            [cell["value"] for cell in draft_view.context["draft_sku_matrix_rows"][0]["cells"][:3]],
            [Decimal("2"), Decimal("2"), projection.baseline_qty],
        )

    def test_planning_activity_uses_sales_or_inbound_or_prior_ending(self):
        sales_product = Product.objects.create(
            code="ACTIVITY-SALES",
            name="Activity Sales",
            status=self.product.status,
            category=self.product.category,
        )
        sales_variant = ProductVariant.objects.create(product=sales_product, name="Black", color="Black")
        sales_sku = SKU.objects.create(
            sku="ACTIVITY-SALES-SKU",
            product_variant=sales_variant,
            current_retail_price=Decimal("100000"),
            current_master_cogs=Decimal("50000"),
        )
        create_manual_sale(
            source_label="Offline",
            order_number="ACTIVITY-SALES-ORDER",
            order_datetime=timezone.make_aware(datetime(2026, 8, 15, 10, 0)),
            sku=sales_sku,
            quantity=1,
            net_unit_price=90000,
            status="Selesai",
            actor=self.user,
        )

        inbound_product = Product.objects.create(
            code="ACTIVITY-INBOUND",
            name="Activity Inbound",
            status=self.product.status,
            category=self.product.category,
        )
        inbound_variant = ProductVariant.objects.create(product=inbound_product, name="Black", color="Black")
        inbound_sku = SKU.objects.create(sku="ACTIVITY-INBOUND-SKU", product_variant=inbound_variant)
        InventoryMovement.objects.create(
            movement_key="ACTIVITY-INBOUND-MOVEMENT",
            movement_date=date(2026, 8, 16),
            movement_type=InventoryMovement.MovementType.INCOMING,
            direction=InventoryMovement.Direction.IN,
            sku=inbound_sku,
            quantity=2,
            source_reference="ACTIVITY-INBOUND",
            posted_by=self.user,
        )

        inactive_product = Product.objects.create(
            code="ACTIVITY-INACTIVE",
            name="Activity Inactive",
            status=self.product.status,
            category=self.product.category,
        )
        inactive_variant = ProductVariant.objects.create(product=inactive_product, name="Black", color="Black")
        SKU.objects.create(sku="ACTIVITY-INACTIVE-SKU", product_variant=inactive_variant)

        snapshot = planning_activity_snapshot(as_of_date=date(2026, 8, 22))
        self.assertIn(self.product.id, snapshot["active_product_ids"])
        self.assertIn(sales_product.id, snapshot["active_product_ids"])
        self.assertIn(inbound_product.id, snapshot["active_product_ids"])
        self.assertNotIn(inactive_product.id, snapshot["active_product_ids"])

        products = Product.objects.filter(id__in=[
            self.product.id, sales_product.id, inbound_product.id, inactive_product.id,
        ])
        active_names = set(filter_products_by_planning_activity(products, "ACTIVE", snapshot).values_list("name", flat=True))
        inactive_names = set(filter_products_by_planning_activity(products, "INACTIVE", snapshot).values_list("name", flat=True))
        self.assertEqual(active_names, {"Report Product", "Activity Sales", "Activity Inbound"})
        self.assertEqual(inactive_names, {"Activity Inactive"})

        september_snapshot = planning_activity_snapshot(
            as_of_date=date(2026, 8, 22),
            target_month=date(2026, 9, 1),
        )
        self.assertEqual(september_snapshot["prior_month"], date(2026, 8, 1))
        self.assertEqual(september_snapshot["ending_by_sku"][self.sku.id], Decimal("18"))

    def test_all_products_respects_status_and_category_intersection(self):
        jacket = Category.objects.create(code="JACKET-FILTER", name="Jacket Filter")
        shirt = Category.objects.create(code="SHIRT-FILTER", name="Shirt Filter")
        other_status = ProductStatus.objects.create(code="OTHER-FILTER", name="Other Filter")

        matching_product = Product.objects.create(
            code="MATCHING-JACKET",
            name="Matching Regular Jacket",
            status=self.product.status,
            category=jacket,
        )
        matching_variant = ProductVariant.objects.create(product=matching_product, name="Black", color="Black")
        SKU.objects.create(
            sku="MATCHING-JACKET-SKU",
            product_variant=matching_variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        wrong_category_product = Product.objects.create(
            code="WRONG-CATEGORY",
            name="Wrong Category Product",
            status=self.product.status,
            category=shirt,
        )
        wrong_category_variant = ProductVariant.objects.create(product=wrong_category_product, name="Black", color="Black")
        SKU.objects.create(sku="WRONG-CATEGORY-SKU", product_variant=wrong_category_variant)
        wrong_status_product = Product.objects.create(
            code="WRONG-STATUS",
            name="Wrong Status Product",
            status=other_status,
            category=jacket,
        )
        wrong_status_variant = ProductVariant.objects.create(product=wrong_status_product, name="Black", color="Black")
        SKU.objects.create(sku="WRONG-STATUS-SKU", product_variant=wrong_status_variant)

        current_month = timezone.localdate().replace(day=1)
        scenario = ProjectionScenario.objects.create(
            name="Intersection Filter",
            start_month=current_month,
            end_month=current_month,
            created_by=self.user,
        )
        response = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "builder",
                "scenario": scenario.id,
                "target_month": current_month.strftime("%Y-%m"),
                "scope_type": ProjectionRule.ScopeType.PRODUCT_STATUS,
                "product_status": self.product.status_id,
                "category": jacket.id,
                "planning_activity": "ALL",
                "method": ProjectionRule.Method.INCREASE_PERCENT,
                "parameter": "0",
                "action": "preview",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 Product · 1 SKU terdampak")
        self.assertContains(response, "MATCHING-JACKET-SKU")
        self.assertNotContains(response, "WRONG-CATEGORY-SKU")
        self.assertNotContains(response, "WRONG-STATUS-SKU")

    def test_first_future_month_uses_official_current_projection_without_august_approval(self):
        current_month = timezone.localdate().replace(day=1)
        target_month = date(
            current_month.year + (1 if current_month.month == 12 else 0),
            1 if current_month.month == 12 else current_month.month + 1,
            1,
        )
        result = recommendation_for(
            sku=self.sku,
            target_month=target_month,
            method=ProjectionRule.Method.INCREASE_PERCENT,
            parameter=Decimal("5"),
            today=timezone.localdate(),
        )
        self.assertEqual(result["baseline_month"], current_month)
        self.assertEqual(result["baseline_qty"], Decimal("0"))
        self.assertEqual(result["beginning_qty"], Decimal("18"))
        self.assertEqual(result["recommendation"], Decimal("0"))

    def test_scenario_library_allows_edit_and_delete_for_unused_drafts(self):
        scenario = ProjectionScenario.objects.create(
            name="Duplicate September",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        response = self.client.get("/merchandising/planning-builder/")
        self.assertContains(response, "Scenario yang sudah dibuat")
        self.assertContains(response, "Duplicate September")

        edit_response = self.client.post(
            f"/merchandising/planning-builder/scenario/{scenario.id}/edit/",
            {"name": "September Revised", "start_month": "2026-09", "end_month": "2026-10"},
        )
        self.assertRedirects(edit_response, "/merchandising/planning-builder/")
        scenario.refresh_from_db()
        self.assertEqual(scenario.name, "September Revised")
        self.assertEqual(scenario.end_month, date(2026, 10, 1))

        delete_response = self.client.post(f"/merchandising/planning-builder/scenario/{scenario.id}/delete/")
        self.assertRedirects(delete_response, "/merchandising/planning-builder/")
        self.assertFalse(ProjectionScenario.objects.filter(pk=scenario.id).exists())

    def test_duplicate_scenario_is_blocked_but_used_draft_can_be_deleted(self):
        existing = ProjectionScenario.objects.create(
            name="September Duplicate Guard",
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            created_by=self.user,
        )
        duplicate_response = self.client.post(
            "/merchandising/planning-builder/",
            {
                "form_name": "scenario",
                "name": "September Duplicate Guard",
                "start_month": "2026-09",
                "end_month": "2026-09",
            },
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertContains(duplicate_response, "Scenario dengan nama dan periode yang sama sudah ada")
        self.assertEqual(ProjectionScenario.objects.filter(name="September Duplicate Guard").count(), 1)

        ProjectionRule.objects.create(
            scenario=existing,
            target_month=date(2026, 9, 1),
            scope_type=ProjectionRule.ScopeType.ALL_PRODUCTS,
            method=ProjectionRule.Method.INCREASE_PERCENT,
            parameter=Decimal("5"),
            created_by=self.user,
        )
        delete_response = self.client.post(f"/merchandising/planning-builder/scenario/{existing.id}/delete/")
        self.assertRedirects(delete_response, "/merchandising/planning-builder/")
        self.assertFalse(ProjectionScenario.objects.filter(pk=existing.id).exists())
        self.assertFalse(ProjectionRule.objects.filter(scenario_id=existing.id).exists())
        self.assertTrue(AuditEvent.objects.filter(
            action="projection_scenario_draft_deleted",
            entity_id=str(existing.id),
        ).exists())
