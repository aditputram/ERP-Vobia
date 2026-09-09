import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent

from .access import VIEW_TABS
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


class UserManagementTests(TestCase):
    def setUp(self):
        self.password = "AmanSekali-ERP-2026!"
        self.admin = get_user_model().objects.create_superuser(
            username="admin", password=self.password
        )
        self.client.force_login(self.admin)

    def test_superadmin_can_create_user_with_module_access(self):
        response = self.client.post(
            reverse("accounts:user_create"),
            {
                "username": "marketing.team",
                "first_name": "Marketing",
                "last_name": "Team",
                "email": "marketing@vobia.id",
                "job_title": "Marketing",
                "is_active": "on",
                "password": "Marketing-Aman-2026!",
                "access_sales": "view",
                "access_operation": "none",
                "access_rnd": "none",
                "access_marketing": "edit",
                "access_master_data": "view",
                "access_reconciliation": "none",
                "access_guide": "view",
                "tabs_sales": ["dashboard"],
                "tabs_marketing": ["dashboard", "campaigns"],
                "tabs_master_data": ["master_data"],
                "tabs_guide": ["guide"],
            },
        )
        self.assertRedirects(response, reverse("accounts:user_list"))
        user = get_user_model().objects.get(username="marketing.team")
        self.assertEqual(user.module_access["marketing"], "edit")
        self.assertEqual(user.tab_access["marketing"], ["dashboard", "campaigns"])
        self.assertTrue(user.check_password("Marketing-Aman-2026!"))
        self.assertTrue(AuditEvent.objects.filter(action="user_created", actor=self.admin).exists())

    def test_user_management_uses_system_layout_without_module_sidebar(self):
        response = self.client.get(reverse("accounts:user_list"))

        self.assertContains(response, "system-shell")
        self.assertNotContains(response, 'class="sidebar"')
        self.assertNotContains(response, "Ganti modul")
        self.assertContains(response, reverse("dashboard:index"))
        self.assertContains(response, 'aria-label="Kembali ke dashboard"')

    def test_non_superuser_cannot_open_user_management(self):
        user = get_user_model().objects.create_user(username="staff", password=self.password)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("accounts:user_list")).status_code, 302)

    def test_module_middleware_enforces_none_view_and_approve_levels(self):
        user = get_user_model().objects.create_user(
            username="staff",
            password=self.password,
            module_access={"sales": "view", "operation": "none"},
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("sales:dashboard")).status_code, 200)
        self.assertEqual(self.client.post(reverse("sales:dashboard")).status_code, 403)
        self.assertEqual(self.client.get(reverse("merchandising:overview")).status_code, 403)

    def test_existing_user_without_access_map_keeps_legacy_access(self):
        user = get_user_model().objects.create_user(username="legacy", password=self.password)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("sales:dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("merchandising:overview")).status_code, 200)

    def test_operation_tab_allowlist_hides_and_blocks_other_tabs(self):
        user = get_user_model().objects.create_user(
            username="field-team",
            password=self.password,
            module_access={"operation": "edit"},
            tab_access={"operation": ["production_activity"]},
        )
        self.client.force_login(user)

        response = self.client.get(reverse("production:activity"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("production:activity"))
        self.assertNotContains(response, reverse("production:planning"))
        self.assertNotContains(response, reverse("production:monitoring"))
        self.assertNotContains(response, reverse("merchandising:dashboard"))
        self.assertEqual(self.client.get(reverse("production:planning")).status_code, 403)
        self.assertEqual(self.client.get(reverse("merchandising:dashboard")).status_code, 403)

    def test_user_form_lists_operation_subtabs(self):
        user = get_user_model().objects.create_user(
            username="field-team",
            password=self.password,
            module_access={"operation": "edit"},
            tab_access={"operation": ["production_activity"]},
        )

        response = self.client.get(reverse("accounts:user_edit", args=[user.id]))

        self.assertContains(response, "Production · Production Activity")
        self.assertContains(response, 'name="tabs_operation"', count=17)
        self.assertContains(
            response,
            'value="production_activity" id="id_tabs_operation_10" checked',
        )

    def test_rnd_product_documents_are_shared_by_collection_and_development_tabs(self):
        self.assertEqual(
            VIEW_TABS["rnd:product_detail"],
            ("rnd", ["collections", "development"]),
        )

    def test_enter_module_opens_first_allowed_tab(self):
        user = get_user_model().objects.create_user(
            username="field-team",
            password=self.password,
            module_access={"operation": "edit"},
            tab_access={"operation": ["production_activity"]},
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:enter_module", args=["operation"]))

        self.assertRedirects(response, reverse("production:activity"))

    def test_active_module_requires_at_least_one_selected_tab(self):
        response = self.client.post(
            reverse("accounts:user_create"),
            {
                "username": "field-team",
                "first_name": "Tim",
                "last_name": "Lapangan",
                "is_active": "on",
                "password": "Field-Team-Aman-2026!",
                "access_sales": "none",
                "access_operation": "edit",
                "access_rnd": "none",
                "access_marketing": "none",
                "access_master_data": "none",
                "access_reconciliation": "none",
                "access_guide": "none",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilih minimal satu tab untuk modul Operation")
        self.assertFalse(get_user_model().objects.filter(username="field-team").exists())

    def test_master_import_respects_master_data_access_level(self):
        user = get_user_model().objects.create_user(
            username="master-viewer",
            password=self.password,
            module_access={"master_data": "view"},
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("master_data:overview")).status_code, 200)
        self.assertEqual(self.client.get(reverse("master_data:export_bank_data")).status_code, 200)
        self.assertEqual(self.client.post(reverse("imports:master_upload")).status_code, 403)

        user.module_access = {"master_data": "edit"}
        user.save(update_fields=["module_access"])
        self.assertEqual(
            self.client.post(reverse("imports:master_approve", args=[uuid.uuid4()])).status_code,
            403,
        )
