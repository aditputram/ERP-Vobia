from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch


class DashboardAccessTests(TestCase):
    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('dashboard:index')}",
        )

    def test_authenticated_superadmin_can_open_dashboard(self):
        user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vobia Space")
        self.assertContains(response, "<title>Vobia Space - Pilih Modul</title>", html=True)
        self.assertContains(response, "vobia-tiktok-app-icon.png")
        self.assertContains(response, "Play For Legacy")
        self.assertContains(response, "module-space-brand")
        self.assertContains(response, "Vobia Business Connected")
        self.assertContains(response, "Tempat Untuk Bekerja, Berkarya dan Tertawa")
        self.assertContains(response, "VOBIA SPACE")
        self.assertContains(response, "User Setting")
        self.assertContains(response, reverse("accounts:user_list"))
        self.assertContains(response, "Log Out")
        self.assertNotContains(response, "Pilih modul kerja")
        self.assertNotContains(response, "Mulai dari Sales")
        for module_name in ("Sales", "Operation", "RnD", "Marketing", "Finance", "Human Resource"):
            self.assertContains(response, module_name)
        for image_name in (
            "module-sales.jpg",
            "module-operation.jpg",
            "module-rnd.jpg",
            "module-marketing.jpg",
            "module-finance.jpg",
            "module-human-resource.jpg",
        ):
            self.assertContains(response, image_name)

    def test_regular_user_setting_opens_password_change(self):
        user = get_user_model().objects.create_user(
            username="staff",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:index"))

        self.assertContains(response, reverse("accounts:password_change"))
        self.assertNotContains(response, reverse("accounts:user_list"))

    def test_inaccessible_module_uses_warning_dialog_and_server_guard(self):
        user = get_user_model().objects.create_user(
            username="sales.manager",
            password="AmanSekali-ERP-2026!",
            module_access={"sales": "approve", "operation": "none", "marketing": "none"},
        )
        self.client.force_login(user)

        dashboard = self.client.get(reverse("dashboard:index"))
        self.assertContains(dashboard, '<button class="module-card-action" type="button" data-access-warning>', count=3)
        self.assertContains(dashboard, "yang tidak berkepentingan dilarang masuk!")

        denied = self.client.get(reverse("dashboard:enter_module", args=["operation"]), follow=True)
        self.assertRedirects(denied, reverse("dashboard:index"))
        self.assertContains(denied, "yang tidak berkepentingan dilarang masuk!")

    def test_marketing_user_can_open_marketing_pages(self):
        user = get_user_model().objects.create_user(
            username="marketing.staff",
            password="AmanSekali-ERP-2026!",
            module_access={"marketing": "approve"},
        )
        self.client.force_login(user)

        with patch("dashboard.instagram_report.get_report", return_value=(None, "")):
            marketing = self.client.get(reverse("dashboard:instagram_dashboard"))
            self.assertEqual(marketing.status_code, 200)
            self.assertContains(marketing, '<summary class="secondary-button">Kelola koneksi</summary>', html=True)
            self.assertContains(marketing, ">Apply</button>", html=False)
            self.assertContains(marketing, reverse("dashboard:instagram_connection"))
            self.assertContains(marketing, reverse("dashboard:tiktok_connection"))
        self.assertEqual(self.client.get(reverse("dashboard:campaign_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard:partnership_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard:instagram_connection")).status_code, 403)

    def test_sales_module_sets_context_and_opens_sales_dashboard(self):
        user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard:enter_module", args=["sales"]))
        self.assertRedirects(response, reverse("sales:dashboard"))
        self.assertEqual(self.client.session["active_module"], "sales")

    def test_operation_module_sets_context_and_opens_merchandising(self):
        user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard:enter_module", args=["operation"]))
        self.assertRedirects(response, reverse("merchandising:overview"))
        self.assertEqual(self.client.session["active_module"], "operation")

    def test_rnd_module_sets_context_and_opens_foundation_dashboard(self):
        user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:enter_module", args=["rnd"]))

        self.assertRedirects(response, reverse("rnd:dashboard"))
        self.assertEqual(self.client.session["active_module"], "rnd")
        dashboard = self.client.get(reverse("rnd:dashboard"))
        self.assertContains(dashboard, "Collection R&amp;D")
        self.assertContains(dashboard, "Buat Collection")

    def test_rnd_dashboard_respects_module_access(self):
        user = get_user_model().objects.create_user(
            username="rnd.blocked",
            password="AmanSekali-ERP-2026!",
            module_access={"rnd": "none"},
        )
        self.client.force_login(user)

        self.assertEqual(self.client.get(reverse("rnd:dashboard")).status_code, 403)

    def test_existing_user_without_rnd_setting_does_not_gain_access(self):
        user = get_user_model().objects.create_user(
            username="legacy.user",
            password="AmanSekali-ERP-2026!",
            module_access={"sales": "edit"},
        )
        self.client.force_login(user)

        selector = self.client.get(reverse("dashboard:index"))
        rnd_module = next(item for item in selector.context["modules"] if item["slug"] == "rnd")
        self.assertFalse(rnd_module["accessible"])
        self.assertEqual(self.client.get(reverse("rnd:dashboard")).status_code, 403)

    def test_future_module_stays_on_selector_with_clear_status(self):
        user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)
        response = self.client.get(
            reverse("dashboard:enter_module", args=["finance"]),
            follow=True,
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertContains(response, "Modul Finance sudah masuk roadmap")

    def test_all_operational_modules_render_for_superadmin(self):
        user = get_user_model().objects.create_superuser(
            username="vobiasuperadmin",
            password="AmanSekali-ERP-2026!",
        )
        self.client.force_login(user)
        for url_name in (
            "sales:dashboard",
            "imports:sales_list",
            "traffic:overview",
            "merchandising:overview",
            "purchasing:overview",
            "inventory:overview",
            "reconciliation:overview",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
