import csv
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from accounts.models import User
from inventory.models import InventoryMovement
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU
from sales.models import SalesOrder, SalesOrderLine

from .models import SalesImportBatch
from .services.historical_sales import commit_historical_sales_batch, create_historical_sales_import


HEADERS = [
    "Month", "Date", "Source", "Source Group", "No. Pesanan", "Status Pesanan",
    "SKU", "Status Produk", "Category", "Sub Category", "Nama Produk", "Nama Variasi",
    "Qty", "Harga Setelah Diskon", "Retail Price", "COGS", "Total Gross Sales",
    "Total Net Sales", "Total COGS", "Margin", "GPM Rate", "Total Discount",
    "Discount Rate", "", "Traffic Product Mapping",
]


class HistoricalSalesMigrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="vobiasuperadmin", password="strong-test-password")
        status = ProductStatus.objects.create(code="REGULAR", name="Regular")
        category = Category.objects.create(code="SHIRT", name="Shirt")
        product = Product.objects.create(code="P1", parent_sku="P1", article="Product One", name="Product One", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black")
        self.sku = SKU.objects.create(sku="SKU-1", product_variant=variant, size="M", current_retail_price=100000, current_master_cogs=40000)

    def test_history_commits_snapshots_without_inventory_posting(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "history.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=HEADERS)
                writer.writeheader()
                writer.writerow({
                    "Month": "1. January", "Date": "2", "Source": "Shopee", "Source Group": "Marketplace",
                    "No. Pesanan": "ORDER-1", "Status Pesanan": "Selesai", "SKU": "SKU-1",
                    "Status Produk": "Regular", "Category": "Shirt", "Sub Category": "Shirt",
                    "Nama Produk": "Product One", "Nama Variasi": "M", "Qty": "2",
                    "Harga Setelah Diskon": "Rp90,000", "Retail Price": "Rp100,000", "COGS": "Rp40,000",
                    "Total Gross Sales": "Rp200,000", "Total Net Sales": "Rp180,000",
                    "Total COGS": "Rp80,000", "Margin": "Rp100,000",
                })
                writer.writerow({
                    "Month": "2. February", "Date": "3", "Source": "Offline", "Source Group": "Other",
                    "No. Pesanan": "ORDER-2", "Status Pesanan": "Selesai", "SKU": "LEGACY-1",
                    "Qty": "1", "Harga Setelah Diskon": "Rp50,000", "Total Net Sales": "Rp50,000",
                })
            with override_settings(PRIVATE_UPLOAD_ROOT=Path(temp) / "private"):
                batch = create_historical_sales_import(source, self.user)
                self.assertEqual(batch.status, SalesImportBatch.Status.READY)
                self.assertEqual(batch.warning_count, 1)
                _, counts = commit_historical_sales_batch(batch.id, self.user)

        self.assertEqual(counts["orders_created"], 2)
        self.assertEqual(counts["lines_created"], 2)
        self.assertEqual(InventoryMovement.objects.count(), 0)
        self.assertFalse(SalesOrder.objects.get(order_number="ORDER-1").affects_inventory)
        legacy = SalesOrderLine.objects.get(sku_code_snapshot="LEGACY-1")
        self.assertIsNone(legacy.sku)
        self.assertEqual(legacy.total_net_sales, 50000)
        self.assertIsNone(legacy.total_cogs)
