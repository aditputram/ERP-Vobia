from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import instagram_report
from .models import SocialDailyMetric, SocialSyncRun
from .social_sync import daily_series, manual_refresh_state, run_manual_refresh, sync_platform


class SocialSyncTests(TestCase):
    def setUp(self):
        self.day = date(2026, 8, 31)
        self.values = {
            "reach": 10, "impressions": 20, "total_engagement": 0,
            "accounts_engaged": None, "profile_visits": 3, "website_clicks": None,
            "likes": 0, "comments": 1, "shares": 2,
            "new_followers": None, "lost_followers": None,
        }

    @patch("dashboard.social_sync.fetch_instagram_days")
    def test_daily_upsert_preserves_null_and_zero_and_is_idempotent(self, fetch):
        fetch.return_value = [(self.day, self.values)]
        first = sync_platform("INSTAGRAM", self.day, lookback_days=1)
        second = sync_platform("INSTAGRAM", self.day, lookback_days=1)

        self.assertEqual(first.status, SocialSyncRun.Status.COMPLETED)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(fetch.call_count, 1)
        row = SocialDailyMetric.objects.get()
        self.assertEqual(row.total_engagement, 0)
        self.assertIsNone(row.accounts_engaged)

    @patch("dashboard.social_sync.fetch_instagram_days")
    def test_failed_sync_keeps_last_valid_snapshot(self, fetch):
        SocialDailyMetric.objects.create(
            platform="INSTAGRAM", account="vobia.id", date=self.day,
            synced_at=datetime(2026, 9, 1, tzinfo=dt_timezone.utc), **self.values,
        )
        fetch.side_effect = OSError("PRIVATE_TOKEN_MUST_NOT_LEAK")

        run = sync_platform(
            "INSTAGRAM", self.day, lookback_days=1,
            idempotency_key="manual:test-failure",
        )

        self.assertEqual(run.status, SocialSyncRun.Status.FAILED)
        self.assertNotIn("PRIVATE_TOKEN", run.error)
        self.assertEqual(SocialDailyMetric.objects.get().reach, 10)

    def test_chart_payload_is_date_ordered(self):
        for day, reach in ((date(2026, 8, 31), 20), (date(2026, 8, 30), 10)):
            SocialDailyMetric.objects.create(
                platform="INSTAGRAM", account="vobia.id", date=day,
                reach=reach, synced_at=datetime(2026, 9, 1, tzinfo=dt_timezone.utc),
            )
        rows = daily_series("INSTAGRAM", date(2026, 8, 1), self.day)
        self.assertEqual([row["date"] for row in rows], ["2026-08-30", "2026-08-31"])
        self.assertIsNone(rows[0]["impressions"])

    @override_settings(SOCIAL_SYNC_SECRET="scheduler-secret")
    @patch("dashboard.social_sync.sync_daily")
    def test_scheduled_endpoint_requires_secret(self, sync_daily):
        self.assertEqual(self.client.post(reverse("dashboard:scheduled_social_sync")).status_code, 403)
        run = SocialSyncRun(
            platform="INSTAGRAM", status="COMPLETED", cutoff=self.day,
        )
        sync_daily.return_value = [run]
        response = self.client.post(
            reverse("dashboard:scheduled_social_sync"),
            HTTP_X_VOBIA_SCHEDULER_SECRET="scheduler-secret",
        )
        self.assertEqual(response.status_code, 200)
        sync_daily.assert_called_once_with(source="scheduler")

    @override_settings(SOCIAL_SYNC_SECRET="scheduler-secret", USE_SQLITE=False)
    def test_scheduled_endpoint_requires_https_outside_local_uat(self):
        response = self.client.post(
            reverse("dashboard:scheduled_social_sync"),
            HTTP_X_VOBIA_SCHEDULER_SECRET="scheduler-secret",
        )
        self.assertEqual(response.status_code, 403)

    @patch("dashboard.tiktok.fetch_available_report")
    @patch("dashboard.instagram_report.fetch_report")
    def test_dashboard_get_never_calls_external_api_and_refresh_is_admin_only(self, ig_fetch, tt_fetch):
        user = get_user_model().objects.create_user(
            "marketing-viewer", password="Strong-Test-2026!", module_access={"marketing": "view"},
        )
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard:instagram_dashboard"))
        self.assertEqual(response.status_code, 200)
        ig_fetch.assert_not_called()
        tt_fetch.assert_not_called()
        self.assertNotContains(response, "Refresh data")
        self.assertEqual(self.client.post(reverse("dashboard:instagram_dashboard"), {"period": "7"}).status_code, 403)

    @patch("dashboard.social_sync.fetch_tiktok_days")
    @patch("dashboard.social_sync.fetch_instagram_days")
    def test_manual_refresh_is_global_once_per_wib_day(self, instagram_days, tiktok_days):
        instagram_days.return_value = [(self.day, self.values)]
        tiktok_days.return_value = [(self.day, self.values)]

        first, runs, claimed = run_manual_refresh("marketing.user")
        second, second_runs, second_claimed = run_manual_refresh("another.user")

        self.assertTrue(claimed)
        self.assertEqual(first.status, SocialSyncRun.Status.COMPLETED)
        self.assertEqual(len(runs), 2)
        self.assertFalse(second_claimed)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second_runs, [])
        self.assertEqual(manual_refresh_state().actor, "marketing.user")

    @patch("dashboard.instagram_report.get_tiktok_report", return_value=(None, ""))
    @patch("dashboard.instagram_report.get_report", return_value=(None, ""))
    @patch("dashboard.instagram_report.run_manual_refresh")
    def test_marketing_edit_user_can_start_refresh(self, manual_refresh, _instagram, _tiktok):
        now = datetime(2026, 9, 1, tzinfo=dt_timezone.utc)
        manual_refresh.return_value = (
            SocialSyncRun(status=SocialSyncRun.Status.FAILED, started_at=now), [], True,
        )
        user = get_user_model().objects.create_user(
            "marketing-editor", password="Strong-Test-2026!", module_access={"marketing": "edit"},
        )
        self.client.force_login(user)
        response = self.client.post(reverse("dashboard:instagram_dashboard"), {"period": "7"})
        self.assertEqual(response.status_code, 200)
        manual_refresh.assert_called_once_with("marketing-editor")
