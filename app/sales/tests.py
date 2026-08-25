from datetime import date, datetime
from decimal import Decimal
import uuid

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import InventoryException, InventoryMovement
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU

from .models import SalesOrder, SalesOrderLine


class SalesReportRouteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="vobiasuperadmin", password="strong-test-password")
        self.client.force_login(self.user)

    def test_sales_report_routes_render(self):
        for name in ("sales:product_performance", "sales:pareto", "sales:transactions", "sales:input_transaction"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_report_filters_use_auto_apply_without_apply_buttons(self):
        for name in ("sales:dashboard", "sales:product_performance", "sales:pareto"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "data-auto-submit")
                self.assertNotContains(response, ">Apply Filters<")

    def test_pareto_supports_month_quarter_semester_and_year_periods(self):
        fixtures = (
            (date(2026, 1, 10), "PARETO-JAN", "100000"),
            (date(2026, 4, 10), "PARETO-APR", "200000"),
            (date(2026, 8, 10), "PARETO-AUG", "400000"),
        )
        for order_day, order_number, net in fixtures:
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=order_number,
                order_datetime=timezone.make_aware(datetime.combine(order_day, datetime.min.time())),
                order_date=order_day,
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            net_value = Decimal(net)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=order_number,
                product_name_snapshot=order_number,
                quantity=1,
                net_unit_price=net_value,
                retail_price_snapshot=net_value,
                sales_cogs_snapshot=Decimal("50000"),
                total_gross_sales=net_value,
                total_net_sales=net_value,
                total_cogs=Decimal("50000"),
                gpm=net_value - Decimal("50000"),
            )

        default_response = self.client.get(reverse("sales:pareto"))
        self.assertEqual(default_response.context["period_type"], "year")
        self.assertEqual(default_response.context["period_value"], "2026")
        self.assertEqual(default_response.context["totals"]["net"], Decimal("700000"))
        self.assertContains(default_response, ">2026</option>")

        cases = (
            ("month", "2026-01", Decimal("100000")),
            ("quarter", "2026-Q2", Decimal("200000")),
            ("semester", "2026-S2", Decimal("400000")),
            ("year", "2026", Decimal("700000")),
        )
        for period_type, period, expected_net in cases:
            with self.subTest(period_type=period_type, period=period):
                response = self.client.get(reverse("sales:pareto"), {
                    "period_type": period_type,
                    "period": period,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["period_type"], period_type)
                self.assertEqual(response.context["period_value"], period)
                self.assertEqual(response.context["totals"]["net"], expected_net)

    def test_dashboard_custom_date_range_filters_all_sales_metrics(self):
        for day, order_number, net in ((5, "AUG-05", "100000"), (15, "AUG-15", "250000")):
            net_value = Decimal(net)
            gross_value = net_value + Decimal("10000")
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=order_number,
                order_datetime=timezone.make_aware(datetime(2026, 8, day, 10, 0)),
                order_date=date(2026, 8, day),
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=f"SKU-{day}",
                product_name_snapshot="Test Product",
                quantity=1,
                net_unit_price=net_value,
                retail_price_snapshot=gross_value,
                sales_cogs_snapshot=Decimal("50000"),
                total_gross_sales=gross_value,
                total_net_sales=net_value,
                total_cogs=Decimal("50000"),
                gpm=net_value - Decimal("50000"),
            )

        response = self.client.get(reverse("sales:dashboard"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-10",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["date_from"], date(2026, 8, 1))
        self.assertEqual(response.context["date_to"], date(2026, 8, 10))
        self.assertEqual(response.context["totals"]["orders"], 1)
        self.assertEqual(response.context["totals"]["net"], Decimal("100000"))
        self.assertEqual(response.context["source_rows"][0]["orders"], 1)
        self.assertEqual(response.context["period_type"], "custom")
        self.assertEqual(response.context["period_trend_grain"], "day")
        self.assertEqual(len(response.context["period_trend"]), 10)
        self.assertEqual(response.context["period_trend"][4]["day"], date(2026, 8, 5))
        self.assertEqual(response.context["period_trend"][4]["label"], "05 Agu")
        self.assertEqual(response.context["period_trend"][4]["net"], Decimal("100000"))
        self.assertEqual(response.context["period_trend"][4]["gross"], Decimal("110000"))
        self.assertEqual(response.context["period_trend"][5]["net"], 0)
        self.assertContains(response, "Daily Gross Sales")
        self.assertNotContains(response, "Daily Gross Sales dalam periode")
        self.assertContains(response, 'name="date_from"')
        self.assertContains(response, 'name="date_to"')
        self.assertContains(response, 'data-max-visible-rows="25"')

    def test_dashboard_supports_month_quarter_semester_year_and_trend_grain(self):
        for order_day, order_number, gross in (
            (date(2026, 1, 10), "DASH-JAN", "100000"),
            (date(2026, 4, 10), "DASH-APR", "200000"),
            (date(2026, 8, 10), "DASH-AUG", "400000"),
        ):
            gross_value = Decimal(gross)
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=order_number,
                order_datetime=timezone.make_aware(datetime.combine(order_day, datetime.min.time())),
                order_date=order_day,
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=order_number,
                product_name_snapshot="Dashboard Period Product",
                quantity=1,
                net_unit_price=gross_value,
                retail_price_snapshot=gross_value,
                sales_cogs_snapshot=Decimal("0"),
                total_gross_sales=gross_value,
                total_net_sales=gross_value,
                total_cogs=Decimal("0"),
                gpm=gross_value,
            )

        cases = (
            ("month", "2026-08", "day", 31, Decimal("400000"), "Daily Gross Sales"),
            ("quarter", "2026-Q2", "month", 3, Decimal("200000"), "Monthly Gross Sales Trend"),
            ("semester", "2026-S2", "month", 6, Decimal("400000"), "Monthly Gross Sales Trend"),
            ("year", "2026", "month", 12, Decimal("700000"), "Monthly Gross Sales Trend"),
        )
        for period_type, period, grain, row_count, expected_gross, title in cases:
            with self.subTest(period_type=period_type, period=period):
                response = self.client.get(reverse("sales:dashboard"), {
                    "period_type": period_type,
                    "period": period,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["period_type"], period_type)
                self.assertEqual(response.context["period_value"], period)
                self.assertEqual(response.context["period_trend_grain"], grain)
                self.assertEqual(len(response.context["period_trend"]), row_count)
                self.assertEqual(response.context["totals"]["gross"], expected_gross)
                self.assertContains(response, title)

    def test_dashboard_monthly_gross_chart_uses_rolling_twelve_months(self):
        for order_day, order_number, gross in (
            (date(2026, 1, 10), "JAN-2026", "1000000000"),
            (date(2026, 3, 10), "MAR-2026", "2000000000"),
            (date(2027, 2, 10), "FEB-2027", "3000000000"),
        ):
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=order_number,
                order_datetime=timezone.make_aware(datetime.combine(order_day, datetime.min.time())),
                order_date=order_day,
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            gross_value = Decimal(gross)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=order_number,
                product_name_snapshot="Test Product",
                quantity=1,
                net_unit_price=gross_value,
                retail_price_snapshot=gross_value,
                sales_cogs_snapshot=Decimal("0"),
                total_gross_sales=gross_value,
                total_net_sales=gross_value,
                total_cogs=Decimal("0"),
                gpm=gross_value,
            )

        response = self.client.get(reverse("sales:dashboard"))
        monthly = response.context["monthly_gross"]

        self.assertEqual(len(monthly), 12)
        self.assertEqual(monthly[0]["month"], date(2026, 3, 1))
        self.assertEqual(monthly[-1]["month"], date(2027, 2, 1))
        self.assertEqual(monthly[0]["gross_billion"], Decimal("2"))
        self.assertEqual(monthly[-1]["gross_billion"], Decimal("3"))
        self.assertEqual(response.context["mtd_cutoff_day"], 10)
        self.assertEqual(response.context["mtd_gross"][0]["gross_billion"], Decimal("2"))
        self.assertEqual(response.context["mtd_gross"][-1]["gross_billion"], Decimal("3"))
        self.assertNotContains(response, "Jan: Rp 1.000.000.000")
        self.assertContains(response, 'class="panel monthly-sales-panel"')
        self.assertContains(response, "MTD Gross Sales")
        self.assertContains(response, 'class="panel trend-panel"')
        self.assertLess(response.content.index(b"Daily Gross Sales"), response.content.index(b"Monthly Gross Sales"))
        self.assertLess(response.content.index(b"Monthly Gross Sales"), response.content.index(b"MTD Gross Sales"))
        self.assertLess(response.content.index(b"MTD Gross Sales"), response.content.index(b"Status periode"))

    def test_dashboard_source_group_and_source_are_cascading_multi_filters(self):
        fixtures = (
            (SalesOrder.Source.SHOPEE, "Shopee", "100000"),
            (SalesOrder.Source.TIKTOK, "Tiktok", "200000"),
            (SalesOrder.Source.OTHER, "Whatsapp", "400000"),
        )
        for index, (source, source_label, gross) in enumerate(fixtures, start=1):
            order = SalesOrder.objects.create(
                source=source,
                source_label=source_label,
                order_number=f"DASH-SOURCE-{index}",
                order_datetime=timezone.make_aware(datetime(2026, 8, index, 10, 0)),
                order_date=date(2026, 8, index),
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            gross_value = Decimal(gross)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=f"DASH-SOURCE-SKU-{index}",
                product_name_snapshot="Dashboard Source Product",
                quantity=1,
                net_unit_price=gross_value,
                retail_price_snapshot=gross_value,
                sales_cogs_snapshot=Decimal("0"),
                total_gross_sales=gross_value,
                total_net_sales=gross_value,
                total_cogs=Decimal("0"),
                gpm=gross_value,
            )

        response = self.client.get(reverse("sales:dashboard"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "source_group": ["Marketplace"],
            "source": ["Shopee", "Tiktok"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_source_groups"], ["Marketplace"])
        self.assertEqual(response.context["selected_sources"], ["Shopee", "Tiktok"])
        self.assertEqual(response.context["totals"]["orders"], 2)
        self.assertEqual(response.context["totals"]["gross"], Decimal("300000"))
        self.assertEqual(response.context["monthly_gross"][-1]["gross"], Decimal("300000"))
        self.assertEqual(
            {item["value"] for item in response.context["source_options"]},
            {"Shopee", "Tiktok"},
        )
        self.assertEqual(response.content.count(b"data-multi-select data-all-label"), 2)
        self.assertContains(response, 'data-source-group="Marketplace"')
        self.assertContains(response, "data-source-cascade")

        stale_source = self.client.get(reverse("sales:dashboard"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "source_group": ["Marketplace"],
            "source": ["Whatsapp"],
        })
        self.assertEqual(stale_source.context["selected_sources"], [])
        self.assertEqual(stale_source.context["totals"]["orders"], 2)

        all_sources = self.client.get(reverse("sales:dashboard"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
        })
        self.assertEqual(all_sources.context["selected_source_groups"], [])
        self.assertEqual(all_sources.context["selected_sources"], [])
        self.assertEqual(all_sources.context["totals"]["orders"], 3)
        self.assertEqual(all_sources.context["totals"]["gross"], Decimal("700000"))

    def test_dashboard_mtd_gross_uses_latest_sales_day_as_same_monthly_cutoff(self):
        for order_day, order_number, gross in (
            (date(2026, 1, 19), "JAN-19", "1000000000"),
            (date(2026, 1, 20), "JAN-20", "2000000000"),
            (date(2026, 2, 19), "FEB-19", "6000000000"),
            (date(2026, 3, 19), "MAR-19", "3000000000"),
        ):
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=order_number,
                order_datetime=timezone.make_aware(datetime.combine(order_day, datetime.min.time())),
                order_date=order_day,
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            gross_value = Decimal(gross)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=order_number,
                product_name_snapshot="Test Product",
                quantity=1,
                net_unit_price=gross_value,
                retail_price_snapshot=gross_value,
                sales_cogs_snapshot=Decimal("0"),
                total_gross_sales=gross_value,
                total_net_sales=gross_value,
                total_cogs=Decimal("0"),
                gpm=gross_value,
            )

        response = self.client.get(reverse("sales:dashboard"))
        monthly = {row["month"]: row for row in response.context["monthly_gross"]}
        mtd = {row["month"]: row for row in response.context["mtd_gross"]}

        self.assertEqual(response.context["mtd_cutoff_day"], 19)
        self.assertEqual(monthly[date(2026, 1, 1)]["gross"], Decimal("3000000000"))
        self.assertEqual(mtd[date(2026, 1, 1)]["gross"], Decimal("1000000000"))
        self.assertEqual(mtd[date(2026, 2, 1)]["gross"], Decimal("6000000000"))
        self.assertEqual(mtd[date(2026, 2, 1)]["growth_pct"], Decimal("500"))
        self.assertEqual(mtd[date(2026, 2, 1)]["growth_class"], "positive")
        self.assertEqual(mtd[date(2026, 3, 1)]["growth_pct"], Decimal("-50.0"))
        self.assertEqual(mtd[date(2026, 3, 1)]["growth_class"], "negative")
        self.assertEqual(monthly[date(2026, 2, 1)]["growth_pct"], Decimal("100"))
        self.assertEqual(monthly[date(2026, 2, 1)]["growth_class"], "positive")
        self.assertEqual(monthly[date(2026, 3, 1)]["growth_pct"], Decimal("-50.0"))
        self.assertEqual(monthly[date(2026, 3, 1)]["growth_class"], "negative")
        self.assertContains(response, "Tanggal 1–19 setiap bulan")
        self.assertContains(response, 'class="monthly-growth-divider"')
        self.assertContains(response, 'class="growth-positive"')
        self.assertContains(response, 'class="growth-negative"')
        self.assertContains(response, 'aria-label="Growth MTD Gross Sales bulanan"')

    def test_product_performance_accepts_multiple_product_filters(self):
        for index, (product_name, net) in enumerate((
            ("Product Alpha", "100000"),
            ("Product Beta", "200000"),
            ("Product Gamma", "400000"),
        ), start=1):
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=f"MULTI-{index}",
                order_datetime=timezone.make_aware(datetime(2026, 8, index, 10, 0)),
                order_date=date(2026, 8, index),
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            net_value = Decimal(net)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=f"MULTI-SKU-{index}",
                product_name_snapshot=product_name,
                quantity=1,
                net_unit_price=net_value,
                retail_price_snapshot=net_value,
                sales_cogs_snapshot=Decimal("50000"),
                total_gross_sales=net_value,
                total_net_sales=net_value,
                total_cogs=Decimal("50000"),
                gpm=net_value - Decimal("50000"),
            )

        response = self.client.get(reverse("sales:product_performance"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "product": ["Product Alpha", "Product Beta"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_products"], ["Product Alpha", "Product Beta"])
        self.assertEqual(response.context["totals"]["orders"], 2)
        self.assertEqual(response.context["totals"]["net"], Decimal("300000"))
        self.assertContains(response, "2 selected")
        self.assertContains(response, 'data-filter-search')

    def test_product_performance_accepts_multiple_product_status_filters(self):
        for index, (product_status, net) in enumerate((
            ("Regular", "100000"),
            ("Essential+", "200000"),
            ("Discontinue", "400000"),
        ), start=1):
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=f"MULTI-STATUS-{index}",
                order_datetime=timezone.make_aware(datetime(2026, 8, index, 10, 0)),
                order_date=date(2026, 8, index),
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            net_value = Decimal(net)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=f"MULTI-STATUS-SKU-{index}",
                product_name_snapshot=f"Product Status {index}",
                product_status_snapshot=product_status,
                quantity=1,
                net_unit_price=net_value,
                retail_price_snapshot=net_value,
                sales_cogs_snapshot=Decimal("50000"),
                total_gross_sales=net_value,
                total_net_sales=net_value,
                total_cogs=Decimal("50000"),
                gpm=net_value - Decimal("50000"),
            )

        response = self.client.get(reverse("sales:product_performance"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "product_status": ["Regular", "Essential+"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_product_statuses"], ["Regular", "Essential+"])
        self.assertEqual(response.context["totals"]["orders"], 2)
        self.assertEqual(response.context["totals"]["net"], Decimal("300000"))
        self.assertContains(response, "Status Produk")
        self.assertContains(response, "2 selected")

    def test_product_performance_accepts_multiple_source_group_and_category_filters(self):
        fixtures = (
            (SalesOrder.Source.SHOPEE, "Shopee", "Category Alpha", "100000"),
            (SalesOrder.Source.TIKTOK, "Tiktok", "Category Beta", "200000"),
            (SalesOrder.Source.OTHER, "Whatsapp", "Category Gamma", "400000"),
        )
        for index, (source, source_label, category, net) in enumerate(fixtures, start=1):
            order = SalesOrder.objects.create(
                source=source,
                source_label=source_label,
                order_number=f"MULTI-DIM-{index}",
                order_datetime=timezone.make_aware(datetime(2026, 8, index, 10, 0)),
                order_date=date(2026, 8, index),
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            net_value = Decimal(net)
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=f"MULTI-DIM-SKU-{index}",
                product_name_snapshot=f"Dimension Product {index}",
                category_snapshot=category,
                quantity=1,
                net_unit_price=net_value,
                retail_price_snapshot=net_value,
                sales_cogs_snapshot=Decimal("50000"),
                total_gross_sales=net_value,
                total_net_sales=net_value,
                total_cogs=Decimal("50000"),
                gpm=net_value - Decimal("50000"),
            )

        response = self.client.get(reverse("sales:product_performance"), {
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "source": ["Shopee", "Tiktok"],
            "source_group": ["Marketplace"],
            "category": ["Category Alpha", "Category Beta"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_sources"], ["Shopee", "Tiktok"])
        self.assertEqual(response.context["selected_source_groups"], ["Marketplace"])
        self.assertEqual(response.context["selected_categories"], ["Category Alpha", "Category Beta"])
        self.assertEqual(response.context["totals"]["orders"], 2)
        self.assertEqual(response.context["totals"]["net"], Decimal("300000"))
        self.assertEqual(response.content.count(b"data-multi-select data-all-label"), 5)

    def test_product_performance_cascades_status_category_and_product_options(self):
        fixtures = (
            ("Regular", "Knitwear", "Product Knit"),
            ("Regular", "Shirt", "Product Shirt"),
            ("Seasonal New", "Socks", "Product Socks"),
        )
        for index, (product_status, category, product_name) in enumerate(fixtures, start=1):
            order = SalesOrder.objects.create(
                source=SalesOrder.Source.SHOPEE,
                source_label="Shopee",
                order_number=f"CASCADE-{index}",
                order_datetime=timezone.make_aware(datetime(2026, 8, index, 10, 0)),
                order_date=date(2026, 8, index),
                current_status="Selesai",
                source_status="Selesai",
                is_final=True,
                first_seen_batch_id=uuid.uuid4(),
                latest_batch_id=uuid.uuid4(),
            )
            SalesOrderLine.objects.create(
                order=order,
                sku_code_snapshot=f"CASCADE-SKU-{index}",
                product_name_snapshot=product_name,
                product_status_snapshot=product_status,
                category_snapshot=category,
                quantity=1,
                net_unit_price=Decimal("100000"),
                retail_price_snapshot=Decimal("100000"),
                sales_cogs_snapshot=Decimal("50000"),
                total_gross_sales=Decimal("100000"),
                total_net_sales=Decimal("100000"),
                total_cogs=Decimal("50000"),
                gpm=Decimal("50000"),
            )

        stale = self.client.get(
            reverse("sales:product_performance"),
            {
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "product_status": ["Regular"],
                "category": ["Socks"],
                "product": ["Product Socks"],
            },
        )
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.context["categories"], ("Knitwear", "Shirt"))
        self.assertEqual(stale.context["selected_categories"], [])
        self.assertEqual(stale.context["products"], ("Product Knit", "Product Shirt"))
        self.assertEqual(stale.context["selected_products"], [])
        self.assertEqual(stale.context["totals"]["orders"], 2)

        valid = self.client.get(
            reverse("sales:product_performance"),
            {
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "product_status": ["Regular"],
                "category": ["Knitwear"],
            },
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.context["products"], ("Product Knit",))
        self.assertEqual(valid.context["selected_categories"], ["Knitwear"])
        self.assertEqual(valid.context["totals"]["orders"], 1)

from .services.manual import create_manual_sale
from .services.requirements import import_requirements, summarize_import_requirements


class ManualSalesAndRequirementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sales", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="APPAREL", name="Apparel")
        product = Product.objects.create(code="P-1", name="Product", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black")
        self.sku = SKU.objects.create(
            sku="SKU-1",
            product_variant=variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )

    def test_manual_sale_preserves_actual_source_and_posts_fifo_exception(self):
        line = create_manual_sale(
            source_label="Whatsapp",
            order_number="WA-001",
            order_datetime=timezone.make_aware(datetime(2026, 8, 19, 10, 0)),
            sku=self.sku,
            quantity=2,
            net_unit_price=Decimal("200000"),
            status="Selesai",
            actor=self.user,
        )
        self.assertEqual(line.business_key, "Whatsapp|WA-001|SKU-1")
        self.assertEqual(line.retail_price_snapshot, Decimal("200000"))
        self.assertEqual(line.total_gross_sales, line.total_net_sales)
        movement = InventoryMovement.objects.get(sales_line=line)
        self.assertEqual(movement.movement_key, "SALES|Whatsapp|WA-001|SKU-1")
        self.assertEqual(movement.allocated_cost, Decimal("0"))
        self.assertEqual(InventoryException.objects.get(movement=movement).quantity, Decimal("2"))

    def test_manual_sale_adjusts_only_transaction_snapshot_and_marks_special_case(self):
        line = create_manual_sale(
            source_label="Whatsapp",
            order_number="WA-ABOVE-RETAIL",
            order_datetime=timezone.make_aware(datetime(2026, 8, 19, 10, 0)),
            sku=self.sku,
            quantity=1,
            net_unit_price=Decimal("220000"),
            status="Selesai",
            actor=self.user,
        )
        self.sku.refresh_from_db()
        self.assertEqual(self.sku.current_retail_price, Decimal("200000"))
        self.assertEqual(line.retail_price_snapshot, Decimal("220000"))
        self.assertTrue(line.retail_price_special_case)
        self.assertEqual(line.total_gross_sales, line.total_net_sales)

    def test_manual_sale_special_case_shows_warning_notification(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("sales:input_transaction"),
            {
                "source_label": "Whatsapp",
                "order_number": "WA-NOTIFICATION",
                "order_datetime": "2026-08-19T10:00",
                "sku": self.sku.pk,
                "quantity": 1,
                "net_unit_price": "220000",
                "status": "Selesai",
                "shipped": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SPECIAL CASE HARGA")
        self.assertContains(response, "Retail Price master tetap Rp 200.000")

    def test_manual_transaction_is_separate_from_data_import(self):
        self.client.force_login(self.user)

        manual_response = self.client.get(reverse("sales:input_transaction"))
        import_response = self.client.get(reverse("imports:sales_list"))

        self.assertContains(manual_response, "Input Transaction")
        self.assertContains(manual_response, "Post transaksi")
        self.assertNotContains(import_response, "Input transaksi manual")
        self.assertNotContains(import_response, "Post transaksi")

    def test_nonfinal_old_month_remains_in_import_requirement(self):
        SalesOrder.objects.create(
            source=SalesOrder.Source.SHOPEE,
            source_label="Shopee",
            order_number="OLD-001",
            order_datetime=timezone.make_aware(datetime(2026, 4, 10, 10, 0)),
            order_date=datetime(2026, 4, 10).date(),
            current_status="Sedang Dikirim",
            source_status="Sedang Dikirim",
            is_final=False,
            first_seen_batch_id="11111111-1111-1111-1111-111111111111",
            latest_batch_id="11111111-1111-1111-1111-111111111111",
        )
        requirements = import_requirements(as_of_date=datetime(2026, 8, 20).date())
        april = next(row for row in requirements if row["source"] == "Shopee" and row["period_start"].month == 4)
        self.assertEqual(april["period_start"], date(2026, 4, 10))
        self.assertEqual(april["nonfinal_count"], 1)
        self.assertIn("Sedang Dikirim", april["nonfinal_statuses"])

        summary = summarize_import_requirements(requirements, as_of_date=date(2026, 8, 20))
        shopee_summary = next(row for row in summary if row["source"] == SalesOrder.Source.SHOPEE)
        self.assertEqual(
            shopee_summary,
            {
                "source": SalesOrder.Source.SHOPEE,
                "period_start": date(2026, 4, 10),
                "period_end": date(2026, 8, 19),
            },
        )

    def test_requirement_summary_collapses_months_into_one_range_per_marketplace(self):
        requirements = [
            {
                "source": SalesOrder.Source.TIKTOK,
                "period_start": date(2026, 7, 1),
                "period_end": date(2026, 7, 31),
            },
            {
                "source": SalesOrder.Source.SHOPEE,
                "period_start": date(2026, 8, 1),
                "period_end": date(2026, 8, 19),
            },
            {
                "source": SalesOrder.Source.TIKTOK,
                "period_start": date(2026, 6, 1),
                "period_end": date(2026, 8, 19),
            },
        ]

        self.assertEqual(
            summarize_import_requirements(requirements, as_of_date=date(2026, 8, 20)),
            [
                {
                    "source": SalesOrder.Source.SHOPEE,
                    "period_start": date(2026, 8, 1),
                    "period_end": date(2026, 8, 19),
                },
                {
                    "source": SalesOrder.Source.TIKTOK,
                    "period_start": date(2026, 6, 1),
                    "period_end": date(2026, 8, 19),
                },
            ],
        )
