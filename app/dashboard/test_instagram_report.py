import json
import os
import tempfile
import calendar
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone

from . import instagram as ig, instagram_report as report


class InstagramReportTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = override_settings(INSTAGRAM_CONNECTION_DIR=directory.name, USE_SQLITE=True)
        override.enable()
        self.addCleanup(override.disable)
        ig.save_connection("PRIVATE_TEST_TOKEN", {"username": ig.USERNAME})
        self.end = timezone.now().date() - timedelta(days=1)
        self.start = self.end - timedelta(days=6)

    def api(self, token, path, params):
        if path == "me":
            return {"user_id": ig.ACCOUNT_ID, "username": ig.USERNAME, "followers_count": 100, "media_count": 2}
        if path.endswith("/media"):
            item = {"id": "123", "timestamp": self.end.isoformat() + "T12:00:00+00:00", "media_product_type": "REELS", "caption": "<script>bad</script>", "permalink": "https://www.instagram.com/p/example/"}
            if params.get("after"):
                return {"data": [item]}
            return {"data": [item], "paging": {"next": "https://untrusted.invalid/?access_token=SECRET", "cursors": {"after": "next"}}}
        if path.endswith("/comments"):
            return {"data": [{"id": "c1", "from": {"username": "viewer"}, "text": "<script>comment</script>", "timestamp": self.end.isoformat(), "like_count": 2}]}
        names = params["metric"].split(",")
        if "demographics" in names[0] or names[0] == "follows_and_unfollows":
            return {"data": [{"total_value": {"breakdowns": [{"results": [{"dimension_values": ["FOLLOWER"], "value": 6}, {"dimension_values": ["NON_FOLLOWER"], "value": 2}]}]}}]}
        values = {"views": 1000, "total_interactions": 50, "accounts_engaged": 25, "likes": 0, "ig_reels_avg_watch_time": 1500}
        if path == "123/insights":
            return {"data": [{"name": name, "period": "lifetime", "values": [{"value": values.get(name, 10)}]} for name in names if name != "saved"]}
        return {"data": [{"name": name, "total_value": {"value": values.get(name, 10)}} for name in names]}

    def snapshot(self):
        with patch.object(report, "api_get", side_effect=self.api):
            return report.fetch_report(self.start, self.end)

    def test_metrics_uniques_pagination_and_no_secret(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["totals"]["total_interactions"], 50)
        self.assertEqual(snapshot["totals"]["accounts_engaged"], 25)
        self.assertEqual(snapshot["er"], 5)
        self.assertEqual(snapshot["net_follows"], 4)
        self.assertEqual(len(snapshot["contents"]), 1)
        self.assertEqual(len(snapshot["demographics"]), 12)
        self.assertEqual(snapshot["demographics"][0]["rows"][0]["percent"], 75)
        media = snapshot["contents"][0]
        self.assertEqual(media["metrics"]["ig_reels_avg_watch_time_seconds"], 1.5)
        self.assertIsNone(media["metrics"]["saved"])
        self.assertTrue(media["partial"])
        self.assertEqual(media["comments"][0]["username"], "viewer")
        self.assertTrue(media["comments_complete"])
        self.assertTrue(snapshot["library_complete"])
        self.assertNotIn("TOKEN", json.dumps(snapshot))
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_missing_is_not_zero_and_never_sum_unique_series(self):
        values = report.metric_values({"data": [{"name": "reach", "period": "day", "values": [{"value": 10}, {"value": 20}]}, {"name": "likes", "total_value": {"value": 0}}]}, ["reach", "likes", "views"])
        self.assertEqual(values, {"reach": None, "likes": 0, "views": None})
        self.assertIsNone(report.rate(10, 0))
        self.assertEqual(report.rate(0, 10), 0)
        self.assertEqual(report.safe_permalink("javascript:alert(1)"), "")
        self.assertEqual(report.safe_permalink("https://instagram.com.evil.example/"), "")
        self.assertEqual(report.growth(150, 100), 50)
        self.assertEqual(report.growth(50, 100), -50)
        self.assertIsNone(report.growth(50, 0))
        self.assertEqual(report.previous_period(self.start, self.end, "7"), (self.start - timedelta(days=7), self.start - timedelta(days=1)))

    def test_period_validation(self):
        self.assertTrue(report.PeriodForm({"date_from": self.start, "date_to": self.end}).is_valid())
        for start, end in [(self.end, self.start), (self.start, self.end + timedelta(days=1)), (self.end - timedelta(days=90), self.end)]:
            self.assertFalse(report.PeriodForm({"date_from": start, "date_to": end}).is_valid())

    def test_cache_permissions_and_stale_fallback(self):
        snapshot = self.snapshot()
        with patch.object(report, "fetch_report", return_value=snapshot) as fetch:
            first, error = report.get_report(self.start, self.end)
            second, error = report.get_report(self.start, self.end)
            self.assertEqual(first, second)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(error, "")
        path = report.report_path(self.start, self.end)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        snapshot["fetched_at"] = (timezone.now() - timedelta(hours=2)).isoformat()
        report.write_snapshot(path, snapshot)
        os.utime(path.parent / "report.lock", (0, 0))
        with patch.object(report, "fetch_report", side_effect=RuntimeError("PRIVATE_TEST_TOKEN")):
            cached, error = report.get_report(self.start, self.end)
        self.assertEqual(cached, snapshot)
        self.assertNotIn("PRIVATE_TEST_TOKEN", error)

    def test_account_mismatch_rejected(self):
        with patch.object(report, "api_get", return_value={"user_id": "wrong", "username": "other"}):
            with self.assertRaises(ig.ConnectionError):
                report.fetch_report(self.start, self.end)

    def test_dashboard_access_render_and_escaping(self):
        request = RequestFactory().get("/marketing/")
        request.user = SimpleNamespace(is_authenticated=True, is_superuser=True, username="tester")
        request.session = {}
        with patch.object(report, "get_report", return_value=(self.snapshot(), "")):
            response = report.dashboard(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accounts Engaged")
        self.assertContains(response, "lifetime")
        self.assertContains(response, "&lt;script&gt;bad&lt;/script&gt;")
        self.assertContains(response, "<td>—</td>", html=True)
        self.assertContains(response, "<td>0</td>", html=True)
        self.assertNotContains(response, "PRIVATE_TEST_TOKEN")
        self.assertIn("no-store", response["Cache-Control"])
        request.user.is_superuser = False
        self.assertEqual(report.dashboard(request).status_code, 200)
        request.META["REMOTE_ADDR"] = "192.0.2.1"
        self.assertEqual(report.dashboard(request).status_code, 403)

    def test_presets_and_custom(self):
        today = timezone.now().date()
        for days in (7, 14, 30, 60, 90):
            form = report.PeriodForm({"period": str(days), "date_from": "invalid"})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["date_from"], today - timedelta(days=days))
            self.assertEqual(form.cleaned_data["date_to"], today - timedelta(days=1))
        self.assertFalse(report.PeriodForm({"period": "custom"}).is_valid())
        self.assertFalse(report.PeriodForm({"period": "365"}).is_valid())
        form = report.PeriodForm({"date_from": self.start, "date_to": self.end})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["period"], "custom")
        self.assertTrue(report.PeriodForm({"period": "custom", "date_from": today - timedelta(days=90), "date_to": self.end}).is_valid())
        previous_month_end = today.replace(day=1) - timedelta(days=1)
        month_value = previous_month_end.strftime("%Y-%m")
        month_form = report.PeriodForm({"period": "month", "month": month_value})
        self.assertTrue(month_form.is_valid(), month_form.errors)
        self.assertEqual(month_form.cleaned_data["date_from"], previous_month_end.replace(day=1))
        self.assertEqual(month_form.cleaned_data["date_to"], previous_month_end)
        comparison_start, comparison_end = report.previous_period(month_form.cleaned_data["date_from"], month_form.cleaned_data["date_to"], "month")
        self.assertEqual(comparison_end, month_form.cleaned_data["date_from"] - timedelta(days=1))
        self.assertEqual(comparison_start.day, 1)
        historic_month_end = today.replace(day=1) - timedelta(days=91)
        historic_month_form = report.PeriodForm({
            "period": "month",
            "month": historic_month_end.strftime("%Y-%m"),
        })
        self.assertTrue(historic_month_form.is_valid(), historic_month_form.errors)
        self.assertFalse(report.PeriodForm({
            "period": "custom",
            "date_from": historic_month_end.replace(day=1),
            "date_to": historic_month_end,
        }).is_valid())
        current_full_comparison_form = report.PeriodForm({"period": "month", "month": today.strftime("%Y-%m")})
        self.assertTrue(current_full_comparison_form.is_valid(), current_full_comparison_form.errors)
        self.assertEqual(current_full_comparison_form.cleaned_data["date_to"], today - timedelta(days=1))
        full_previous_start, full_previous_end = report.previous_period(current_full_comparison_form.cleaned_data["date_from"], current_full_comparison_form.cleaned_data["date_to"], "month")
        self.assertEqual(full_previous_start.day, 1)
        self.assertEqual(full_previous_end.day, calendar.monthrange(full_previous_end.year, full_previous_end.month)[1])
        current_month_form = report.PeriodForm({"period": "month_mtd", "month": today.strftime("%Y-%m")})
        self.assertTrue(current_month_form.is_valid(), current_month_form.errors)
        self.assertEqual(current_month_form.cleaned_data["date_from"], today.replace(day=1))
        self.assertEqual(current_month_form.cleaned_data["date_to"], today - timedelta(days=1))
        current_previous_start, current_previous_end = report.previous_period(current_month_form.cleaned_data["date_from"], current_month_form.cleaned_data["date_to"], "month_mtd")
        self.assertEqual(current_previous_start.day, 1)
        self.assertEqual(current_previous_end.day, (today - timedelta(days=1)).day)
