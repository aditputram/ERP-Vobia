from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent

from .models import LoginThrottle


class LocalAuthenticationTests(TestCase):
    def setUp(self):
        self.password = "AmanSekali-ERP-2026!"
        self.user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password=self.password,
        )
        self.login_url = reverse("accounts:login")

    def test_login_page_uses_vobia_space_branding(self):
        response = self.client.get(self.login_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VOBIA SPACE")
        self.assertContains(
            response,
            "Business Connected, tempat untuk bekerja, berkarya dan tertawa.",
        )
        self.assertContains(response, "Play For Legacy")
        self.assertContains(response, "Masuk ke Vobia Space")
        self.assertContains(response, "img/logo-vobia.png")

    def test_password_is_hashed(self):
        self.assertNotEqual(self.user.password, self.password)
        self.assertTrue(self.user.check_password(self.password))

    def test_successful_login_redirects_to_dashboard_and_is_audited(self):
        response = self.client.post(
            self.login_url,
            {"username": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertTrue(
            AuditEvent.objects.filter(action="login_success", actor=self.user).exists()
        )

    def test_login_is_locked_after_five_failures(self):
        for _ in range(5):
            response = self.client.post(
                self.login_url,
                {"username": self.user.username, "password": "salah"},
            )
            self.assertEqual(response.status_code, 401)

        throttle = LoginThrottle.objects.get(username=self.user.username)
        self.assertGreater(throttle.locked_until, timezone.now())

        blocked = self.client.post(
            self.login_url,
            {"username": self.user.username, "password": self.password},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertFalse(blocked.wsgi_request.user.is_authenticated)

    def test_expired_lock_allows_valid_login(self):
        LoginThrottle.objects.create(
            username=self.user.username,
            ip_address="127.0.0.1",
            failure_count=5,
            locked_until=timezone.now() - timedelta(minutes=1),
        )
        response = self.client.post(
            self.login_url,
            {"username": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("dashboard:index"))


class SuperadminSetupCommandTests(TestCase):
    def test_command_creates_hashed_superadmin_and_audit_event(self):
        password = "Setup-Aman-ERP-2026!"
        with patch(
            "accounts.management.commands.setup_superadmin.getpass",
            side_effect=[password, password],
        ):
            call_command("setup_superadmin")

        user = get_user_model().objects.get(username="vobiasuperadmin")
        self.assertTrue(user.is_superuser)
        self.assertNotEqual(user.password, password)
        self.assertTrue(user.check_password(password))
        self.assertTrue(
            AuditEvent.objects.filter(action="superadmin_created", actor=user).exists()
        )


class InitialSuperadminBrowserSetupTests(TestCase):
    def setUp(self):
        self.setup_url = reverse("accounts:initial_setup")
        self.password = "Browser-Setup-Aman-2026!"

    def test_local_setup_creates_hashed_user_and_logs_in(self):
        response = self.client.post(
            self.setup_url,
            {"password1": self.password, "password2": self.password},
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        user = get_user_model().objects.get(username="vobiasuperadmin")
        self.assertTrue(user.check_password(self.password))
        self.assertNotEqual(user.password, self.password)
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(user.pk))
        self.assertTrue(
            AuditEvent.objects.filter(
                action="superadmin_created",
                actor=user,
                metadata__setup_method="localhost_first_run",
            ).exists()
        )

    def test_setup_is_disabled_after_user_exists(self):
        get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password=self.password,
        )
        response = self.client.get(self.setup_url, REMOTE_ADDR="127.0.0.1")
        self.assertRedirects(response, reverse("accounts:login"))

    def test_setup_rejects_non_local_request(self):
        response = self.client.get(self.setup_url, REMOTE_ADDR="203.0.113.10")
        self.assertEqual(response.status_code, 403)

    @override_settings(ALLOW_INITIAL_SETUP_PAGE=False)
    def test_setup_is_disabled_by_configuration(self):
        response = self.client.get(self.setup_url, REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response.status_code, 403)


class HealthCheckTests(TestCase):
    def test_health_check_confirms_application_and_database(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
