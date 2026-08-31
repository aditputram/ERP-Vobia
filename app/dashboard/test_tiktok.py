import json
import os
import tempfile
from datetime import date
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse

from . import tiktok, tiktok_business


class TikTokConnectionTests(TestCase):
    def test_video_id_from_url_supports_video_and_photo(self):
        self.assertEqual(tiktok.video_id_from_url("https://www.tiktok.com/@vobia.id/video/123?lang=en"), "123")
        self.assertEqual(tiktok.video_id_from_url("https://www.tiktok.com/@vobia.id/photo/456"), "456")

    @patch.object(tiktok, "access_token", return_value="ACCESS_PRIVATE")
    @patch.object(tiktok, "api_request")
    def test_query_videos_calculates_public_metrics(self, api_request, _token):
        api_request.return_value = {"data": {"videos": [{
            "id": "123", "view_count": 1000, "like_count": 80,
            "comment_count": 10, "share_count": 10,
        }]}, "error": {"code": "ok"}}
        result = tiktok.query_videos(["123"])["123"]
        self.assertEqual(result["engagement"], 100)
        self.assertEqual(result["er"], 10)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = override_settings(
            TIKTOK_CONNECTION_DIR=directory.name,
            TIKTOK_CLIENT_KEY="test-client-key",
            TIKTOK_CLIENT_SECRET="TEST_SECRET_NEVER_RENDER",
            TIKTOK_BUSINESS_APP_ID="7680016213652537364",
            TIKTOK_BUSINESS_APP_SECRET="BUSINESS_SECRET_NEVER_RENDER",
            TIKTOK_LIVE_ENABLED=True,
        )
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_superuser(username="admin", password="Strong-Test-2026!")
        self.client.force_login(self.user)

    def test_public_legal_pages_are_available(self):
        self.client.logout()
        self.assertContains(self.client.get(reverse("terms")), "Terms of Service")
        self.assertContains(self.client.get(reverse("privacy")), "Privacy Policy")

    def test_oauth_start_uses_state_and_read_only_scopes(self):
        response = self.client.get(reverse("dashboard:tiktok_oauth_start"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://www.tiktok.com/v2/auth/authorize/"))
        self.assertIn("video.list", response.url)
        self.assertTrue(self.client.session["tiktok_oauth_state"])
        self.assertNotIn("TEST_SECRET_NEVER_RENDER", response.url)

    @patch.object(tiktok, "api_request")
    def test_callback_verifies_state_and_stores_token_privately(self, api_request):
        api_request.side_effect = [
            {"access_token": "ACCESS_PRIVATE", "refresh_token": "REFRESH_PRIVATE", "open_id": "open-1", "scope": tiktok.SCOPES, "expires_in": 86400},
            {"data": {"user": {"open_id": "open-1", "display_name": "Vobia", "username": "vobia.id"}}, "error": {"code": "ok"}},
        ]
        session = self.client.session
        session["tiktok_oauth_state"] = "safe-state"
        session.save()

        response = self.client.get(reverse("dashboard:tiktok_callback"), {"code": "code-1", "state": "safe-state"})

        self.assertRedirects(response, reverse("dashboard:tiktok_connection"))
        saved = json.loads(tiktok.store_path().read_text())
        self.assertEqual(saved["username"], "vobia.id")
        self.assertEqual(saved["refresh_token"], "REFRESH_PRIVATE")
        self.assertEqual(os.stat(tiktok.store_path()).st_mode & 0o777, 0o600)
        page = self.client.get(reverse("dashboard:tiktok_connection"))
        self.assertNotContains(page, "ACCESS_PRIVATE")
        self.assertNotContains(page, "REFRESH_PRIVATE")

    def test_callback_rejects_wrong_state_and_non_admin(self):
        session = self.client.session
        session["tiktok_oauth_state"] = "expected"
        session.save()
        self.assertEqual(self.client.get(reverse("dashboard:tiktok_callback"), {"code": "code", "state": "wrong"}).status_code, 400)
        user = get_user_model().objects.create_user(username="staff", password="Strong-Test-2026!", module_access={"marketing": "approve"})
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("dashboard:tiktok_connection")).status_code, 403)

    def test_connection_shows_business_accounts_api_prototype(self):
        response = self.client.get(reverse("dashboard:tiktok_connection"))
        self.assertContains(response, "BUSINESS ACCOUNTS API")
        self.assertContains(response, "Reached Audience")
        self.assertContains(response, "Hubungkan TikTok Business")

    def test_dashboard_shows_separate_tiktok_update_time(self):
        from django.utils import timezone
        from . import instagram_report

        tiktok_snapshot = ({
            "profile": {}, "videos": [], "views": 0, "engagement": 0,
            "er": None, "fetched_at": timezone.now(), "business": {},
        }, "")
        with patch.object(instagram_report, "get_report", return_value=(None, "Instagram unavailable")), patch.object(instagram_report, "get_tiktok_report", return_value=tiktok_snapshot):
            response = self.client.get(reverse("dashboard:instagram_dashboard"))
        self.assertContains(response, "Login Kit")
        self.assertContains(response, "Update terakhir")

    def test_dashboard_renders_business_suite_account_metrics(self):
        from django.utils import timezone
        from . import instagram_report

        business = {
            "profile": {
                "followers_count": 163134, "total_likes": 728976, "videos_count": 1639,
                "audience_genders": [{"gender": "Female", "percentage_display": 60}],
                "audience_ages": [], "audience_countries": [], "audience_cities": [],
                "audience_activity": [],
            },
            "reach": 319613, "views": 500000, "likes": 1000, "comments": 100,
            "shares": 50, "engagement": 1150, "accounts_engaged": 4351,
            "profile_views": 9000, "website_clicks": 100, "new_followers": 500,
            "lost_followers": 100, "follower_growth": 400, "videos": {},
        }
        comparison = {**business, "reach": 300000, "views": 450000, "engagement": 1000}
        tiktok_snapshot = ({
            "profile": {}, "videos": [], "views": 500000, "engagement": 1150,
            "er": 0.23, "fetched_at": timezone.now(), "business": business,
            "business_error": "",
        }, "")
        with (
            patch.object(instagram_report, "get_report", return_value=(None, "Instagram unavailable")),
            patch.object(instagram_report, "get_tiktok_report", return_value=tiktok_snapshot),
            patch.object(tiktok_business, "fetch_profile_report", return_value=comparison),
        ):
            response = self.client.get(reverse("dashboard:instagram_dashboard"), {"period": "7"})

        self.assertContains(response, "319.613")
        self.assertContains(response, "Reached audience")
        self.assertContains(response, "Demografi &amp; aktivitas audiens")
        self.assertContains(response, "Accounts Engaged")

    def test_business_oauth_start_uses_separate_app_and_state(self):
        response = self.client.get(reverse("dashboard:tiktok_business_oauth_start"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://www.tiktok.com/v2/auth/authorize?"))
        self.assertIn("client_key=7680016213652537364", response.url)
        self.assertIn("user.insights", response.url)
        self.assertIn("video.insights", response.url)
        self.assertIn("comment.list", response.url)
        self.assertNotIn("comment.list.manage", response.url)
        self.assertNotIn("video.publish", response.url)
        self.assertNotIn("video.upload", response.url)
        self.assertNotIn("biz.spark.auth", response.url)
        self.assertNotIn("discovery.search.words", response.url)
        self.assertIn("redirect_uri=", response.url)
        self.assertTrue(self.client.session["tiktok_business_oauth_state"])
        self.assertNotIn("BUSINESS_SECRET_NEVER_RENDER", response.url)

    @patch.object(tiktok_business, "urlopen")
    def test_business_api_surfaces_tiktok_http_error(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://business-api.tiktok.com/open_api/v1.3/tt_user/oauth2/token/",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"code": 40002, "message": "Invalid client secret"}'),
        )

        with self.assertRaisesMessage(
            tiktok.TikTokConnectionError,
            "Invalid client secret (code 40002)",
        ):
            tiktok_business.api_request("https://business-api.tiktok.com/test")

    @patch.object(tiktok_business, "urlopen")
    def test_business_api_surfaces_non_json_http_status(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://business-api.tiktok.com/open_api/v1.3/tt_user/oauth2/token/",
            403,
            "Forbidden",
            {},
            BytesIO(b"Forbidden"),
        )

        with self.assertRaisesMessage(tiktok.TikTokConnectionError, "HTTP 403"):
            tiktok_business.api_request("https://business-api.tiktok.com/test")

    @patch.object(tiktok_business, "api_request")
    def test_business_callback_stores_token_privately(self, api_request):
        api_request.return_value = {"access_token": "BUSINESS_ACCESS", "refresh_token": "BUSINESS_REFRESH", "open_id": "open-business", "expires_in": 86400, "scope": "user.info.basic,user.insights"}
        session = self.client.session
        session["tiktok_business_oauth_state"] = "business-state"
        session.save()
        response = self.client.get(reverse("dashboard:tiktok_business_callback"), {"code": "code-1", "state": "business-state"})
        self.assertRedirects(response, reverse("dashboard:tiktok_connection"))
        saved = json.loads(tiktok_business.store_path().read_text())
        self.assertEqual(saved["open_id"], "open-business")
        self.assertEqual(saved["scope"], "user.info.basic,user.insights")
        api_request.assert_called_once()
        self.assertEqual(os.stat(tiktok_business.store_path()).st_mode & 0o777, 0o600)

    @patch.object(tiktok_business, "api_request")
    def test_business_callback_accepts_signed_state_without_session(self, api_request):
        api_request.return_value = {"access_token": "ACCESS", "refresh_token": "REFRESH", "open_id": "open-business", "scope": "user.info.basic,user.insights"}
        state = signing.dumps(
            {"user_id": str(self.user.pk), "nonce": "nonce"},
            salt=tiktok_business.STATE_SALT,
            compress=True,
        )

        response = self.client.get(
            reverse("dashboard:tiktok_business_callback"),
            {"code": "code-1", "state": state},
        )

        self.assertRedirects(response, reverse("dashboard:tiktok_connection"))

    @patch.object(tiktok_business, "api_request")
    @patch.object(tiktok_business, "access_token", return_value="BUSINESS_ACCESS")
    @patch.object(tiktok_business, "load_connection", return_value={"open_id": "business-1"})
    def test_business_report_returns_only_real_available_metrics(self, load_connection, access_token, api_request):
        api_request.side_effect = [
            {
                "followers_count": 100,
                "total_likes": 500,
                "videos_count": 20,
                "audience_genders": [{"gender": "Female", "percentage": 0.35}],
                "audience_countries": [{"country": "ID", "percentage": 0.85}],
                "audience_ages": [{"age": "25-34", "percentage": 0.45}],
                "audience_cities": [{"city_name": "Jakarta", "percentage": 0.4}],
                "metrics": [
                    {"date": "2026-08-29", "unique_video_views": 80, "video_views": 100,
                     "likes": 10, "comments": 1, "shares": 2, "engaged_audience": 7,
                     "profile_views": 4, "bio_link_clicks": 1,
                     "daily_new_followers": 6, "daily_lost_followers": 2},
                    {"date": "2026-08-30", "unique_video_views": 90, "video_views": 120,
                     "likes": 15, "comments": 2, "shares": 3, "engaged_audience": 8,
                     "profile_views": 5, "bio_link_clicks": 2,
                     "daily_new_followers": 7, "daily_lost_followers": 1},
                ],
            },
            {
                "videos": [{"item_id": "video-1", "create_time": "1788048000", "reach": 80}],
                "has_more": False,
            },
        ]

        report = tiktok_business.fetch_report(date(2026, 8, 29), date(2026, 8, 30))

        self.assertEqual(report["reach"], 170)
        self.assertEqual(report["views"], 220)
        self.assertEqual(report["likes"], 25)
        self.assertEqual(report["comments"], 3)
        self.assertEqual(report["shares"], 5)
        self.assertEqual(report["engagement"], 33)
        self.assertEqual(report["accounts_engaged"], 15)
        self.assertEqual(report["profile_views"], 9)
        self.assertEqual(report["website_clicks"], 3)
        self.assertEqual(report["follower_growth"], 10)
        self.assertEqual(report["profile"]["audience_genders"][0]["percentage_display"], 35)
        profile_url = api_request.call_args_list[0].args[0]
        self.assertIn("start_date=2026-08-29", profile_url)
        self.assertIn("end_date=2026-08-30", profile_url)

    def test_business_profile_ranges_never_exceed_sixty_days(self):
        ranges = list(tiktok_business.profile_date_ranges(date(2026, 6, 1), date(2026, 8, 29)))
        self.assertEqual(ranges, [
            (date(2026, 6, 1), date(2026, 7, 30)),
            (date(2026, 7, 31), date(2026, 8, 29)),
        ])

    def test_dashboard_falls_back_to_accounts_api_when_login_kit_fails(self):
        tiktok.save_connection({"access_token": "DISPLAY"})
        tiktok_business.save_connection({"access_token": "BUSINESS"})
        business = {
            "profile": {"followers_count": 100}, "videos": {},
            "reach": 80, "views": 100, "engagement": 10,
        }
        with (
            patch.object(tiktok, "fetch_report", side_effect=tiktok.TikTokConnectionError("Display gagal")),
            patch.object(tiktok_business, "fetch_report", return_value=business),
        ):
            report, error = tiktok.get_report(date(2026, 8, 2), date(2026, 8, 29))

        self.assertEqual(error, "")
        self.assertEqual(report["business"]["reach"], 80)
        self.assertEqual(report["views"], 100)
        self.assertIn("Accounts API", report["display_error"])

    @patch.object(tiktok, "access_token", return_value="ACCESS_PRIVATE")
    @patch.object(tiktok, "api_request")
    def test_report_uses_real_video_metrics(self, api_request, access_token):
        api_request.side_effect = [
            {"data": {"user": {"username": "vobia.id", "follower_count": 90000, "likes_count": 120000, "video_count": 1}}, "error": {"code": "ok"}},
            {"data": {"videos": [{"id": "1", "create_time": 1788134400, "share_url": "https://www.tiktok.com/@vobia.id/video/1", "view_count": 1000, "like_count": 80, "comment_count": 10, "share_count": 10}], "has_more": False}, "error": {"code": "ok"}},
        ]
        report = tiktok.fetch_report(date(2026, 8, 30), date(2026, 8, 31))
        self.assertEqual(report["views"], 1000)
        self.assertEqual(report["engagement"], 100)
        self.assertEqual(report["er"], 10)
