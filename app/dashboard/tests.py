from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


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
