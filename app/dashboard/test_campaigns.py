import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU
from sales.models import SalesOrder, SalesOrderLine

from .campaigns import _end_date, _instagram_embed_url, _post_key
from .models import Campaign, CampaignCreative, CampaignProduct


class CampaignTests(TestCase):
    def test_post_key_ignores_share_parameters_and_www(self):
        self.assertEqual(
            _post_key("https://www.instagram.com/p/DbvfhPrmCyd/?hl=en&img_index=1"),
            _post_key("https://instagram.com/p/DbvfhPrmCyd/"),
        )
        self.assertEqual(
            _post_key("https://www.instagram.com/p/DclRwdFBAOH/?hl=en"),
            _post_key("https://www.instagram.com/reel/DclRwdFBAOH/"),
        )
        self.assertEqual(
            _instagram_embed_url("https://www.instagram.com/reel/Db29JxwJXFB/?hl=en"),
            "https://www.instagram.com/reel/Db29JxwJXFB/embed/",
        )

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("campaignadmin", "c@example.com", "testpass")
        self.client.force_login(self.user)
        status = ProductStatus.objects.create(code="active", name="Active")
        category = Category.objects.create(code="shirt", name="Shirt")
        self.product = Product.objects.create(code="P1", name="Product One", status=status, category=category)
        variant = ProductVariant.objects.create(product=self.product, name="Black")
        self.sku = SKU.objects.create(sku="P1-BLK-M", product_variant=variant, size="M", current_retail_price=100000)
        self.campaign = Campaign.objects.create(
            name="Launch One", description="Test", campaign_plan_url="https://example.com/plan", approval_date=date(2026, 1, 1),
            sample_date=date(2026, 1, 2), creative_date=date(2026, 1, 3),
            prelaunch_date=date(2026, 1, 5), launch_date=date(2026, 1, 10),
            budget=1000000, actual_spent=100000, created_by=self.user,
        )
        CampaignProduct.objects.create(campaign=self.campaign, product=self.product, target_qty=10, retail_price_snapshot=100000, target_gross_sales=1000000)

    def sale(self, day, gross):
        order = SalesOrder.objects.create(
            source="Other", source_label="Website", order_number=str(uuid.uuid4()), order_datetime=day,
            order_date=day.date(), current_status="Selesai", source_status="Selesai", is_final=True,
            first_seen_batch_id=uuid.uuid4(), latest_batch_id=uuid.uuid4(),
        )
        SalesOrderLine.objects.create(order=order, sku=self.sku, sku_code_snapshot=self.sku.sku, product_name_snapshot=self.product.name,
                                      quantity=1, net_unit_price=gross, retail_price_snapshot=gross, total_gross_sales=gross,
                                      total_net_sales=gross, is_counted=True)

    @patch("dashboard.campaigns.get_report", return_value=(None, ""))
    def test_calendar_month_and_sales_window_report(self, report):
        from datetime import datetime, timezone
        self.assertEqual(_end_date(date(2026, 1, 1)), date(2026, 1, 31))
        self.assertEqual(_end_date(date(2026, 1, 15)), date(2026, 2, 14))
        self.sale(datetime(2026, 1, 10, tzinfo=timezone.utc), Decimal("100000"))
        self.sale(datetime(2026, 2, 9, tzinfo=timezone.utc), Decimal("100000"))
        self.sale(datetime(2026, 2, 10, tzinfo=timezone.utc), Decimal("900000"))
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(response, "200.000")
        self.assertContains(response, "2,00x")

    @patch("dashboard.campaigns.get_report")
    def test_creative_card_shows_its_media_metrics(self, report):
        CampaignCreative.objects.create(
            campaign=self.campaign, platform="INSTAGRAM",
            post_url="https://www.instagram.com/p/example/?hl=en",
        )
        report.return_value = ({"contents": [{
            "permalink": "https://www.instagram.com/p/example/",
            "metrics": {"views": 12345, "reach": 6000, "total_interactions": 300, "er": 2.5},
            "comments": [{"username": "viewer", "text": "Great post", "like_count": 2}],
            "comments_available": True, "comments_complete": True,
        }]}, "")
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(response, "12.345")
        self.assertContains(response, "6.000")
        self.assertContains(response, "2,50%")
        self.assertContains(response, "View Comments (1)")
        self.assertContains(response, "Great post")

    def test_create_snapshots_target_and_timeline_validation(self):
        response = self.client.post(reverse("dashboard:campaign_create"), {
            "name": "New", "description": "New campaign", "campaign_plan_url": "https://example.com/moodboard", "approval_date": "2026-02-01", "sample_date": "2026-02-02",
            "creative_date": "2026-02-03", "prelaunch_date": "2026-02-04", "launch_date": "2026-02-05",
            "budget": "Rp. 1.000.000", "actual_spent": "0", "products-TOTAL_FORMS": "1", "products-INITIAL_FORMS": "0",
            "products-MIN_NUM_FORMS": "1", "products-MAX_NUM_FORMS": "1000", "products-0-product": str(self.product.id),
            "products-0-target_qty": "5",
        })
        self.assertEqual(response.status_code, 302)
        item = Campaign.objects.get(name="New").products.get()
        self.assertEqual(item.campaign.actual_spent, Decimal("0"))
        self.assertEqual(item.campaign.budget, Decimal("1000000"))
        self.assertEqual(item.campaign.campaign_plan_url, "https://example.com/moodboard")
        self.assertEqual(item.target_gross_sales, Decimal("500000"))
        self.assertEqual(item.retail_price_snapshot, Decimal("100000"))

    def test_edit_campaign_updates_product_target_and_preserves_spent(self):
        item = self.campaign.products.get()
        response = self.client.post(reverse("dashboard:campaign_edit", args=[self.campaign.id]), {
            "name": self.campaign.name, "description": self.campaign.description,
            "campaign_plan_url": self.campaign.campaign_plan_url, "approval_date": "2026-01-01",
            "sample_date": "2026-01-02", "creative_date": "2026-01-03", "prelaunch_date": "2026-01-05",
            "launch_date": "2026-01-10", "budget": "Rp. 1.000.000",
            "products-TOTAL_FORMS": "1", "products-INITIAL_FORMS": "1",
            "products-MIN_NUM_FORMS": "1", "products-MAX_NUM_FORMS": "1000",
            "products-0-id": str(item.id), "products-0-product": str(self.product.id), "products-0-target_qty": "15",
        })
        self.assertRedirects(response, reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        item.refresh_from_db()
        self.campaign.refresh_from_db()
        self.assertEqual(item.target_qty, 15)
        self.assertEqual(item.target_gross_sales, Decimal("1500000"))
        self.assertEqual(self.campaign.actual_spent, Decimal("100000"))
