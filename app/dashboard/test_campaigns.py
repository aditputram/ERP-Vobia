import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU
from imports.models import RawFile
from sales.models import SalesOrder, SalesOrderLine
from traffic.models import TrafficImportBatch, TrafficProductMetric

from .campaigns import _end_date, _instagram_embed_url, _post_key
from .forms_campaign import CampaignForm
from .models import Campaign, CampaignCreative, CampaignExpense, CampaignProduct, KolPartnership


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

    def test_editing_campaign_with_existing_cover_does_not_revalidate_stored_file(self):
        self.campaign.cover.name = "campaign_covers/existing.jpg"
        self.campaign.save(update_fields=("cover",))
        form = CampaignForm(data={
            "name": self.campaign.name, "description": self.campaign.description,
            "campaign_plan_url": self.campaign.campaign_plan_url,
            "creative_asset_url": "https://drive.google.com/drive/folders/creative",
            "approval_date": "2026-01-01", "sample_date": "2026-01-02",
            "creative_date": "2026-01-03", "prelaunch_date": "2026-01-05",
            "launch_date": "2026-01-10", "budget": "Rp. 1.000.000",
        }, instance=self.campaign)
        self.assertTrue(form.is_valid(), form.errors)

    def sale(self, day, gross):
        order = SalesOrder.objects.create(
            source="Other", source_label="Website", order_number=str(uuid.uuid4()), order_datetime=day,
            order_date=day.date(), current_status="Selesai", source_status="Selesai", is_final=True,
            first_seen_batch_id=uuid.uuid4(), latest_batch_id=uuid.uuid4(),
        )
        SalesOrderLine.objects.create(order=order, sku=self.sku, sku_code_snapshot=self.sku.sku, product_name_snapshot=self.product.name,
                                      quantity=1, net_unit_price=gross, retail_price_snapshot=gross, total_gross_sales=gross,
                                      total_net_sales=gross, is_counted=True)

    def test_campaign_list_shows_newest_created_first(self):
        newest = Campaign.objects.create(
            name="Newest", description="Latest campaign", approval_date=date(2026, 1, 1),
            sample_date=date(2026, 1, 2), creative_date=date(2026, 1, 3),
            prelaunch_date=date(2026, 1, 4), launch_date=date(2025, 1, 5),
            budget=0, created_by=self.user,
        )
        response = self.client.get(reverse("dashboard:campaign_list"))
        self.assertEqual(list(response.context["campaigns"]), [newest, self.campaign])

    @patch("dashboard.campaigns.get_report", return_value=(None, ""))
    def test_calendar_month_and_sales_window_report(self, report):
        from datetime import datetime, timezone
        self.assertEqual(_end_date(date(2026, 1, 1)), date(2026, 1, 31))
        self.assertEqual(_end_date(date(2026, 1, 15)), date(2026, 2, 14))
        self.assertEqual(_end_date(date(2026, 2, 1)), date(2026, 3, 3))
        self.sale(datetime(2026, 1, 10, tzinfo=timezone.utc), Decimal("100000"))
        self.sale(datetime(2026, 2, 9, tzinfo=timezone.utc), Decimal("100000"))
        self.sale(datetime(2026, 2, 10, tzinfo=timezone.utc), Decimal("900000"))
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(response, "200.000")
        self.assertContains(response, "<span>ROI</span><strong>2,00</strong>", html=True)

    @patch("dashboard.campaigns.get_report")
    def test_creative_card_shows_its_media_metrics(self, report):
        CampaignCreative.objects.create(
            campaign=self.campaign, platform="INSTAGRAM",
            post_url="https://www.instagram.com/p/example/?hl=en",
        )
        report.return_value = ({"contents": [{
            "permalink": "https://www.instagram.com/p/example/",
            "metrics": {"views": 12345, "reach": 6000, "likes": 240, "comments": 20,
                        "saved": 25, "shares": 15, "total_interactions": 300, "er": 2.5},
            "comments": [{"username": "viewer", "text": "Great post", "like_count": 2}],
            "comments_available": True, "comments_complete": True,
        }]}, "")
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(response, "12.345")
        self.assertContains(response, "6.000")
        self.assertContains(response, "2,50%")
        self.assertContains(response, "Total Likes")
        self.assertContains(response, "240")
        self.assertContains(response, "25")
        self.assertContains(response, "View Comments (1)")
        self.assertContains(response, "Great post")

    @patch("dashboard.campaigns.tiktok.query_videos")
    def test_tiktok_creative_matches_directly_by_video_id(self, query_videos):
        CampaignCreative.objects.create(
            campaign=self.campaign, platform="TIKTOK",
            post_url="https://www.tiktok.com/@vobia.id/video/123?lang=en",
        )
        query_videos.return_value = {"123": {
            "views": 1000, "likes": 80, "comments": 10, "shares": 10,
            "engagement": 100, "er": 10,
        }}
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(response, "API matched")
        self.assertContains(response, "1.000")
        self.assertContains(response, "10,00%")
        self.assertNotContains(response, "Menunggu koneksi dan persetujuan API TikTok")

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

    def test_edit_campaign_keeps_existing_target_dates_in_date_inputs(self):
        response = self.client.get(reverse("dashboard:campaign_edit", args=[self.campaign.id]))
        html = response.content.decode()
        for field, value in (
            ("approval_date", "2026-01-01"), ("sample_date", "2026-01-02"),
            ("creative_date", "2026-01-03"), ("prelaunch_date", "2026-01-05"),
            ("launch_date", "2026-01-10"),
        ):
            self.assertIn(f'name="{field}" value="{value}"', html)

    @patch("dashboard.campaigns.get_report", return_value=(None, ""))
    def test_actual_timeline_is_edited_separately_from_target(self, report):
        response = self.client.post(reverse("dashboard:campaign_detail", args=[self.campaign.id]), {
            "action": "timeline_actual", "actual_approval_date": "2026-01-02",
            "actual_sample_date": "2026-01-04", "actual_creative_date": "2026-01-06",
            "actual_prelaunch_date": "2026-01-08", "actual_launch_date": "2026-01-12",
        })
        self.assertRedirects(response, reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.approval_date, date(2026, 1, 1))
        self.assertEqual(self.campaign.launch_date, date(2026, 1, 10))
        self.assertEqual(self.campaign.actual_approval_date, date(2026, 1, 2))
        self.assertEqual(self.campaign.actual_launch_date, date(2026, 1, 12))
        page = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(page, "Actual · 12 Jan 2026")
        self.assertContains(page, 'data-edit-toggle="campaign-actual-timeline-form"')

    def test_campaign_spent_is_sum_of_expense_entries(self):
        self.campaign.actual_spent = 0
        self.campaign.save(update_fields=("actual_spent",))
        url = reverse("dashboard:campaign_detail", args=[self.campaign.id])
        for amount, description in (("Rp. 1.000.000", "Production kreatif"), ("Rp. 500.000", "KOL")):
            self.assertRedirects(
                self.client.post(url, {"action": "expense", "amount": amount, "description": description}),
                url,
            )
        self.campaign.refresh_from_db()
        self.assertEqual(CampaignExpense.objects.filter(campaign=self.campaign).count(), 2)
        self.assertEqual(self.campaign.actual_spent, Decimal("1500000"))
        page = self.client.get(url)
        self.assertContains(page, "Production kreatif")
        self.assertContains(page, "Rp1.500.000")
        self.assertContains(page, "Campaign Spent Detail")
        self.assertContains(page, ">Close</button>", html=False)
        self.assertNotContains(page, "Simpan Budget")
        self.assertContains(page, 'value="Rp1.000.000" readonly aria-readonly="true"')
        expense = CampaignExpense.objects.get(campaign=self.campaign, description="KOL")
        self.assertRedirects(
            self.client.post(url, {"action": "delete_expense", "expense_id": str(expense.id)}),
            url,
        )
        self.campaign.refresh_from_db()
        self.assertFalse(CampaignExpense.objects.filter(id=expense.id).exists())
        self.assertEqual(self.campaign.actual_spent, Decimal("1000000"))
        self.assertEqual(self.client.post(url, {"action": "delete_expense", "expense_id": "invalid"}).status_code, 302)

    @patch("dashboard.campaigns.get_report", return_value=(None, ""))
    def test_product_performance_uses_qty_achievement_and_sales_traffic_visitors(self, report):
        raw = RawFile.objects.create(
            dataset_type=RawFile.DatasetType.TRAFFIC_SHOPEE,
            original_filename="traffic.csv", storage_path="tests/traffic.csv",
            checksum_sha256="b" * 64, byte_size=1, detected_format="csv", uploaded_by=self.user,
        )
        batch = TrafficImportBatch.objects.create(
            raw_file=raw, source="Shopee", period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31), status=TrafficImportBatch.Status.COMMITTED,
        )
        TrafficProductMetric.objects.create(
            source="Shopee", period_start=batch.period_start, period_end=batch.period_end,
            product=self.product, traffic_product_key="P1", marketplace_product_code_snapshot="SHOP-P1",
            product_name_snapshot=self.product.name, views=9999, visitors=1234, source_batch=batch,
        )
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertEqual(response.context["rows"][0]["traffic_shopee"], 1234)
        self.assertEqual(response.context["rows"][0]["traffic_tiktok"], 0)
        self.assertContains(response, "1.234")
        self.assertEqual(response.content.decode().count("<th>Achievement</th>"), 1)
        self.assertContains(response, "<th>Total</th>", html=True)
        self.assertEqual(response.context["product_totals"]["achievement"], Decimal("0"))

    def test_create_kol_posting_and_calculate_manual_metrics(self):
        self.assertContains(self.client.get(reverse("dashboard:partnership_create")), "Create KOL Posting")
        response = self.client.post(reverse("dashboard:partnership_create"), {
            "campaign": str(self.campaign.id), "kol_name": "Kevin Michael", "platform": "TIKTOK",
            "budget": "Rp. 2.000.000", "post_url": "https://www.tiktok.com/@kevin/video/123",
            "products-TOTAL_FORMS": "1", "products-INITIAL_FORMS": "0",
            "products-MIN_NUM_FORMS": "1", "products-MAX_NUM_FORMS": "1000",
            "products-0-product": str(self.product.id), "products-0-quantity": "2",
        })
        item = KolPartnership.objects.get(kol_name="Kevin Michael")
        self.assertRedirects(response, reverse("dashboard:partnership_detail", args=[item.id]))
        self.assertEqual(item.budget, Decimal("2000000"))
        self.assertEqual(item.products.get().quantity, 2)
        self.client.post(reverse("dashboard:partnership_detail", args=[item.id]), {
            "action": "metrics", "views": 50000, "likes": 3000, "comments": 100, "saves": 500, "shares": 400,
        })
        item.refresh_from_db()
        self.assertEqual(item.total_engagement, 4000)
        self.assertEqual(item.engagement_rate, 8)
        self.assertEqual(item.cpm, Decimal("40000"))
        self.assertContains(self.client.get(reverse("dashboard:partnership_detail", args=[item.id])), "css/instagram-report.css")
        self.assertContains(self.client.get(reverse("dashboard:partnership_detail", args=[item.id])), "data-dirty-submit")
        self.assertContains(self.client.get(reverse("dashboard:partnership_detail", args=[item.id])), "kol-metric-form")
        detail_html = self.client.get(reverse("dashboard:partnership_detail", args=[item.id])).content.decode()
        self.assertIn('data-dirty-submit-button disabled aria-disabled="true">Simpan Metrik</button>', detail_html)
        self.assertIn('data-edit-toggle="kol-link-form"', detail_html)
        self.assertIn('data-edit-toggle="kol-metric-form"', detail_html)
        self.assertIn('data-edit-cancel="kol-link-form"', detail_html)
        self.assertIn('data-edit-cancel="kol-metric-form"', detail_html)
        self.client.post(reverse("dashboard:partnership_detail", args=[item.id]), {"action": "link", "post_url": "https://www.tiktok.com/@kevin/video/456"})
        item.refresh_from_db()
        self.assertEqual(item.post_url, "https://www.tiktok.com/@kevin/video/456")

    def test_partnership_list_filters_by_kol_and_campaign(self):
        other_campaign = Campaign.objects.create(
            name="Other Campaign", description="Test", campaign_plan_url="https://example.com/other",
            approval_date=date(2026, 2, 1), sample_date=date(2026, 2, 2), creative_date=date(2026, 2, 3),
            prelaunch_date=date(2026, 2, 4), launch_date=date(2026, 2, 5), budget=500000, created_by=self.user,
        )
        KolPartnership.objects.create(campaign=self.campaign, kol_name="Kevin", platform="TIKTOK", budget=100000, created_by=self.user)
        KolPartnership.objects.create(campaign=other_campaign, kol_name="Jethro", platform="INSTAGRAM", budget=100000, created_by=self.user)
        url = reverse("dashboard:partnership_list")
        by_kol = self.client.get(url, {"kol": "Kevin"})
        self.assertContains(by_kol, "<td>Kevin</td>", html=True)
        self.assertNotContains(by_kol, "<td>Jethro</td>", html=True)
        by_campaign = self.client.get(url, {"campaign": str(other_campaign.id)})
        self.assertContains(by_campaign, "<td>Jethro</td>", html=True)
        self.assertNotContains(by_campaign, "<td>Kevin</td>", html=True)
        self.assertEqual(self.client.get(url, {"campaign": "invalid"}).status_code, 200)

    def test_campaign_report_lists_only_its_registered_kol(self):
        other_campaign = Campaign.objects.create(
            name="Other Campaign", description="Test", campaign_plan_url="https://example.com/other",
            approval_date=date(2026, 2, 1), sample_date=date(2026, 2, 2), creative_date=date(2026, 2, 3),
            prelaunch_date=date(2026, 2, 4), launch_date=date(2026, 2, 5), budget=500000, created_by=self.user,
        )
        KolPartnership.objects.create(campaign=self.campaign, kol_name="Kevin", platform="TIKTOK", budget=100000, views=1000, likes=100, created_by=self.user)
        KolPartnership.objects.create(campaign=other_campaign, kol_name="Jethro", platform="INSTAGRAM", budget=100000, created_by=self.user)
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertContains(response, "KOL Partnership")
        self.assertContains(response, "<td><strong>Kevin</strong></td>", html=True)
        self.assertNotContains(response, "<td><strong>Jethro</strong></td>", html=True)
        self.assertEqual(response.context["kol_summary"]["posts"], 1)
        self.assertEqual(response.context["kol_summary"]["budget"], Decimal("100000"))
        self.assertEqual(response.context["kol_summary"]["views"], 1000)
        self.assertEqual(response.context["kol_summary"]["engagement"], 100)
        self.assertEqual(response.context["kol_summary"]["er"], Decimal("10"))
        self.assertEqual(response.context["kol_summary"]["cpm"], Decimal("100000"))

    def test_kol_budget_is_automatically_included_in_campaign_spent_and_roi(self):
        self.campaign.actual_spent = Decimal("1000000")
        self.campaign.save(update_fields=("actual_spent",))
        KolPartnership.objects.create(
            campaign=self.campaign, kol_name="Kevin", platform="TIKTOK",
            budget=Decimal("500000"), created_by=self.user,
        )
        response = self.client.get(reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertEqual(response.context["actual_spent"], Decimal("1500000"))
        self.assertContains(response, "KOL · Kevin · TikTok")
        self.assertContains(response, "Rp1.500.000")

    def test_only_super_admin_can_delete_campaign_and_partnership(self):
        partnership = KolPartnership.objects.create(
            campaign=self.campaign, kol_name="Kevin", platform="TIKTOK", budget=100000, created_by=self.user,
        )
        campaign_delete_url = reverse("dashboard:campaign_delete", args=[self.campaign.id])
        partnership_delete_url = reverse("dashboard:partnership_delete", args=[partnership.id])
        self.assertEqual(self.client.get(campaign_delete_url).status_code, 405)
        blocked = self.client.post(campaign_delete_url)
        self.assertRedirects(blocked, reverse("dashboard:campaign_detail", args=[self.campaign.id]))
        self.assertTrue(Campaign.objects.filter(id=self.campaign.id).exists())
        regular_user = get_user_model().objects.create_user("marketinguser", password="testpass")
        self.client.force_login(regular_user)
        self.assertEqual(self.client.post(partnership_delete_url).status_code, 403)
        self.assertTrue(KolPartnership.objects.filter(id=partnership.id).exists())
        self.client.force_login(self.user)
        self.assertRedirects(self.client.post(partnership_delete_url), reverse("dashboard:partnership_list"))
        self.assertFalse(KolPartnership.objects.filter(id=partnership.id).exists())
        self.assertRedirects(self.client.post(campaign_delete_url), reverse("dashboard:campaign_list"))
        self.assertFalse(Campaign.objects.filter(id=self.campaign.id).exists())

    @patch("dashboard.partnerships.read_public_metrics", return_value={"views": 48300, "likes": 3449, "comments": 26, "saves": 547, "shares": 100})
    def test_refresh_kol_metrics_keeps_last_public_snapshot(self, reader):
        item = KolPartnership.objects.create(
            campaign=self.campaign, kol_name="Kevin", platform="TIKTOK", budget=2000000,
            post_url="https://www.tiktok.com/@kevin/video/123", created_by=self.user,
        )
        response = self.client.post(reverse("dashboard:partnership_detail", args=[item.id]), {"action": "refresh"})
        self.assertRedirects(response, reverse("dashboard:partnership_detail", args=[item.id]))
        item.refresh_from_db()
        self.assertEqual(item.views, 48300)
        self.assertEqual(item.total_engagement, 4122)
        self.assertIsNotNone(item.metrics_updated_at)
