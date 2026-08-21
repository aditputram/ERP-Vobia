from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU
from sales.models import SalesOrder, SalesOrderLine

from .models import ReconciliationRun
from .services.engine import run_reconciliation


class ReconciliationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="auditor", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="APPAREL", name="Apparel")
        product = Product.objects.create(code="P-1", name="Product", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black")
        self.sku = SKU.objects.create(sku="SKU-1", product_variant=variant, current_retail_price=200000, current_master_cogs=100000)

    def test_detects_final_sale_without_inventory_movement(self):
        order = SalesOrder.objects.create(
            source="Other", source_label="Offline", order_number="OFF-1",
            order_datetime=timezone.make_aware(datetime(2026, 8, 10, 10)), shipped_datetime=timezone.make_aware(datetime(2026, 8, 10, 10)),
            order_date=date(2026, 8, 10), current_status="Selesai", source_status="Selesai", is_final=True,
            first_seen_batch_id="11111111-1111-1111-1111-111111111111", latest_batch_id="11111111-1111-1111-1111-111111111111",
        )
        SalesOrderLine.objects.create(
            order=order, sku=self.sku, quantity=1, net_unit_price=180000, retail_price_snapshot=200000,
            sales_cogs_snapshot=100000, total_gross_sales=200000, total_net_sales=180000,
            total_cogs=100000, gpm=80000, gpm_rate=Decimal("0.4"),
        )
        run = run_reconciliation(self.user, date(2026, 8, 20))
        self.assertEqual(run.status, ReconciliationRun.Status.FAILED)
        self.assertTrue(run.issues.filter(code="SALES_MOVEMENT_COUNT").exists())

    def test_empty_database_integrity_checks_pass(self):
        run = run_reconciliation(self.user, date(2026, 8, 20))
        self.assertEqual(run.status, ReconciliationRun.Status.PASSED)
