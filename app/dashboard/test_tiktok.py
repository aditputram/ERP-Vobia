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
        api_request.side_effect = [
            {"access_token": "BUSINESS_ACCESS", "refresh_token": "BUSINESS_REFRESH", "open_id": "open-business", "expires_in": 86400},
            {"scope": "user.info.basic,user.insights"},
        ]
        session = self.client.session
        session["tiktok_business_oauth_state"] = "business-state"
        session.save()
        response = self.client.get(reverse("dashboard:tiktok_business_callback"), {"code": "code-1", "state": "business-state"})
        self.assertRedirects(response, reverse("dashboard:tiktok_connection"))
        saved = json.loads(tiktok_business.store_path().read_text())
        self.assertEqual(saved["open_id"], "open-business")
        self.assertEqual(os.stat(tiktok_business.store_path()).st_mode & 0o777, 0o600)

    @patch.object(tiktok_business, "api_request")
    def test_business_callback_accepts_signed_state_without_session(self, api_request):
        api_request.side_effect = [
            {"access_token": "ACCESS", "refresh_token": "REFRESH", "open_id": "open-business"},
            {"scope": "user.info.basic,user.insights"},
        ]
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
