import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from django.test import SimpleTestCase, RequestFactory, override_settings

from . import instagram as ig


class InstagramConnectionTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(INSTAGRAM_CONNECTION_DIR=self.directory.name, USE_SQLITE=True)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.secret = "TEST_SECRET_NEVER_RENDER_123456789"

    def request(self, data=None, superuser=True, address="127.0.0.1"):
        factory = RequestFactory()
        request = factory.post("/marketing/instagram/", data) if data is not None else factory.get("/marketing/instagram/")
        request.META["REMOTE_ADDR"] = address
        request.user = SimpleNamespace(is_authenticated=True, is_superuser=superuser, username="tester")
        request.session = {}
        return request

    def test_private_atomic_storage_and_public_status(self):
        status = {"username": ig.USERNAME, "account_id": ig.ACCOUNT_ID, "insights_ok": True}
        ig.save_connection(self.secret, status)
        self.assertEqual(os.stat(ig.store_path()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(ig.store_path().parent).st_mode & 0o777, 0o700)
        self.assertNotIn(self.secret, json.dumps(ig.status_only()))
        self.assertEqual(json.loads(ig.store_path().read_text())["access_token"], self.secret)

    @patch.object(ig, "api_get")
    def test_correct_account_and_insights(self, api):
        api.side_effect = [{"user_id": ig.ACCOUNT_ID, "username": ig.USERNAME}, {"data": []}]
        self.assertTrue(ig.verify(self.secret)["insights_ok"])
        self.assertEqual(api.call_count, 2)

    @patch.object(ig, "api_get")
    def test_wrong_account_rejected(self, api):
        api.return_value = {"user_id": "wrong", "username": "other"}
        with self.assertRaises(ig.ConnectionError):
            ig.verify(self.secret)
        self.assertFalse(ig.store_path().exists())

    @patch.object(ig, "api_get")
    def test_profile_success_not_misreported_as_insights_success(self, api):
        api.side_effect = [{"user_id": ig.ACCOUNT_ID, "username": ig.USERNAME}, ig.ConnectionError("Denied")]
        self.assertFalse(ig.verify(self.secret)["insights_ok"])

    def test_access_and_local_only(self):
        self.assertEqual(ig.connection(self.request(superuser=False)).status_code, 403)
        self.assertEqual(ig.connection(self.request(address="192.0.2.1")).status_code, 403)
        with override_settings(USE_SQLITE=False):
            self.assertEqual(ig.connection(self.request()).status_code, 403)

    @patch.object(ig, "verify")
    def test_post_and_get_never_render_token(self, verify):
        verify.return_value = {"username": ig.USERNAME, "account_id": ig.ACCOUNT_ID, "insights_ok": True}
        request = self.request({"access_token": self.secret, "consent": "on"})
        response = ig.connection(request)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("access_token", request.POST)
        response = ig.connection(self.request())
        self.assertNotIn(self.secret, response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, 'type="password"')
        self.assertContains(response, "Uji akses Insights berhasil")

    @patch.object(ig, "verify", side_effect=RuntimeError("secret in an unexpected error"))
    def test_failure_preserves_old_token_and_redacts(self, verify):
        ig.save_connection("OLD_TOKEN", {"username": ig.USERNAME})
        response = ig.connection(self.request({"access_token": self.secret, "consent": "on"}))
        self.assertNotIn(self.secret, response.content.decode())
        self.assertNotIn("secret in an unexpected error", response.content.decode())
        self.assertEqual(json.loads(ig.store_path().read_text())["access_token"], "OLD_TOKEN")

    @patch.object(ig, "verify")
    def test_consent_required(self, verify):
        response = ig.connection(self.request({"access_token": self.secret}))
        self.assertEqual(response.status_code, 200)
        verify.assert_not_called()
        self.assertFalse(ig.store_path().exists())

    @patch.object(ig, "build_opener")
    def test_header_only_token_and_no_redirect(self, opener):
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"user_id":"1"}'
        ig.api_get(self.secret, "me", {"fields": "user_id,username"})
        request = opener.return_value.open.call_args.args[0]
        self.assertNotIn(self.secret, request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.secret)
        self.assertIsNone(ig.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    @patch.object(ig, "build_opener")
    def test_http_errors_never_expose_raw_details(self, opener):
        opener.return_value.open.side_effect = HTTPError("url", 401, self.secret, {}, None)
        with self.assertRaises(ig.ConnectionError) as caught:
            ig.api_get(self.secret, "me", {})
        self.assertNotIn(self.secret, str(caught.exception))
