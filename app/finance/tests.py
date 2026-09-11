from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from audit.models import AuditEvent

from .importers import stage_finance_cutover
from .catalog import FEATURES
from .models import Account, JournalEntry, JournalLine
from .services import account_balances, post_journal


class FinanceJournalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="finance-admin", password="test")
        self.cash, _ = Account.objects.update_or_create(
            code="110101", defaults={"name": "Cash", "account_type": "BANK", "is_postable": True}
        )
        self.capital, _ = Account.objects.update_or_create(
            code="300001", defaults={"name": "Capital", "account_type": "EQTY", "is_postable": True}
        )
        self.entry = JournalEntry.objects.create(
            number="JV-202609-0001",
            entry_date=date(2026, 9, 1),
            description="Setoran modal",
            created_by=self.user,
        )
        JournalLine.objects.create(entry=self.entry, line_number=1, account=self.cash, debit=100, credit=0)
        JournalLine.objects.create(entry=self.entry, line_number=2, account=self.capital, debit=0, credit=100)

    def test_balanced_journal_posts_and_updates_trial_balance(self):
        post_journal(self.entry.id, self.user)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, JournalEntry.Status.POSTED)
        balances = {row["account"].code: row for row in account_balances(end_date=date(2026, 9, 1))}
        self.assertEqual(balances["110101"]["debit_balance"], Decimal("100"))
        self.assertEqual(balances["300001"]["credit_balance"], Decimal("100"))
        self.assertTrue(AuditEvent.objects.filter(action="finance_journal_posted").exists())

    def test_unbalanced_journal_cannot_be_posted(self):
        self.entry.lines.filter(line_number=2).update(credit=90)
        with self.assertRaises(ValidationError):
            post_journal(self.entry.id, self.user)

    def test_opening_journal_cannot_post_before_control_reconciliation(self):
        self.entry.source = JournalEntry.Source.OPENING
        self.entry.source_metadata = {"reconciliation_status": "PENDING_CONTROL_ACCOUNT_RECONCILIATION"}
        self.entry.save(update_fields=("source", "source_metadata"))

        with self.assertRaisesMessage(ValidationError, "akun kontrol"):
            post_journal(self.entry.id, self.user)

    def test_period_balance_can_exclude_opening_journal(self):
        self.entry.status = JournalEntry.Status.POSTED
        self.entry.save(update_fields=("status",))
        opening = JournalEntry.objects.create(
            number="OPENING-TEST",
            entry_date=date(2026, 9, 1),
            description="Opening",
            source=JournalEntry.Source.OPENING,
            status=JournalEntry.Status.POSTED,
        )
        JournalLine.objects.create(entry=opening, line_number=1, account=self.cash, debit=50, credit=0)
        JournalLine.objects.create(entry=opening, line_number=2, account=self.capital, debit=0, credit=50)

        balances = {
            row["account"].code: row
            for row in account_balances(
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30),
                exclude_opening=True,
            )
        }
        self.assertEqual(balances["110101"]["debit_balance"], Decimal("100"))

    def test_finance_pages_render(self):
        self.client.force_login(self.user)
        for name in ("dashboard", "accounts", "journals", "trial_balance", "balance_sheet", "profit_loss"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"finance:{name}")).status_code, 200)

        self.assertContains(self.client.get(reverse("finance:dashboard")), "Finance UAT")

    def test_superadmin_can_add_coa_from_account_page(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "code": "990001",
                "name": "UAT Finance Account",
                "account_type": "OEXP",
                "currency": "IDR",
                "is_postable": "on",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("finance:accounts"))
        self.assertTrue(Account.objects.filter(code="990001", name="UAT Finance Account").exists())
        self.assertTrue(AuditEvent.objects.filter(action="finance_account_created").exists())

    def test_view_only_user_cannot_add_coa(self):
        user = get_user_model().objects.create_user(
            username="finance-viewer",
            password="test",
            module_access={"finance": "view"},
            tab_access={"finance": ["accounts"]},
        )
        self.client.force_login(user)

        page = self.client.get(reverse("finance:accounts"))

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Tambah COA")
        self.assertEqual(self.client.post(reverse("finance:accounts"), {}).status_code, 403)

    def test_inventory_account_is_included_as_balance_sheet_asset(self):
        inventory, _ = Account.objects.update_or_create(
            code="110401", defaults={"name": "Inventory", "account_type": "INTR", "is_postable": True}
        )
        entry = JournalEntry.objects.create(
            number="JV-202609-0002",
            entry_date=date(2026, 9, 1),
            description="Inventory opening",
            status=JournalEntry.Status.POSTED,
        )
        JournalLine.objects.create(entry=entry, line_number=1, account=inventory, debit=50, credit=0)
        JournalLine.objects.create(entry=entry, line_number=2, account=self.capital, debit=0, credit=50)
        self.client.force_login(self.user)

        response = self.client.get(reverse("finance:balance_sheet"), {"as_of": "2026-09-01"})

        self.assertContains(response, "Inventory")
        self.assertEqual(response.context["difference"], Decimal("0"))

    def test_regular_user_does_not_gain_finance_access_by_default(self):
        user = get_user_model().objects.create_user(username="legacy", password="test")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("finance:dashboard")).status_code, 403)

    def test_every_requested_finance_workspace_renders(self):
        self.client.force_login(self.user)
        for slug in FEATURES:
            with self.subTest(slug=slug):
                self.assertEqual(self.client.get(reverse("finance:feature", args=[slug])).status_code, 200)

    def test_finance_workspace_respects_exact_tab_permission(self):
        user = get_user_model().objects.create_user(
            username="cashier",
            password="test",
            module_access={"finance": "view"},
            tab_access={"finance": ["other_payment"]},
        )
        self.client.force_login(user)

        self.assertEqual(self.client.get(reverse("finance:feature", args=["other-payment"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("finance:feature", args=["other-deposit"])).status_code, 403)

    def test_cash_workspace_prefills_matching_journal_workflow(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("finance:journal_create"), {"workflow": "bank-transfer"})

        self.assertContains(response, 'name="workflow" value="bank-transfer"')
        self.assertEqual(response.context["form"].initial["description"], "Bank Transfer")

    def test_created_journal_is_marked_as_uat(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("finance:journal_create"),
            {
                "entry_date": "2026-10-10",
                "description": "UAT payment",
                "reference": "UAT-001",
                "lines-TOTAL_FORMS": "2",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-account": str(self.cash.id),
                "lines-0-description": "",
                "lines-0-debit": "100",
                "lines-0-credit": "0",
                "lines-1-account": str(self.capital.id),
                "lines-1-description": "",
                "lines-1-debit": "0",
                "lines-1-credit": "100",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = JournalEntry.objects.filter(source=JournalEntry.Source.MANUAL).exclude(pk=self.entry.pk).get()
        self.assertEqual(created.source_metadata["environment"], "UAT")

    def test_account_page_shows_opening_balance_for_leaf_and_parent(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("finance:accounts"), {"q": "1101"})

        rows = {account.code: account for account in response.context["accounts"]}
        self.assertEqual(rows["110101"].opening_debit, Decimal("15181"))
        self.assertGreater(rows["1101"].opening_debit, rows["110101"].opening_debit)
        self.assertContains(response, "Saldo Awal")

    def test_superadmin_can_edit_coa_with_audit_history(self):
        self.client.force_login(self.user)
        previous_name = self.cash.name

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "account_id": str(self.cash.id),
                "code": self.cash.code,
                "name": "Petty Cash UAT",
                "account_type": self.cash.account_type,
                "parent": str(self.cash.parent_id),
                "currency": "IDR",
                "is_postable": "on",
                "is_active": "on",
                "opening_balance": "15181",
                "opening_side": "DEBIT",
            },
        )

        self.assertRedirects(response, reverse("finance:accounts"))
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.name, "Petty Cash UAT")
        audit = AuditEvent.objects.get(action="finance_account_updated", entity_id=self.cash.id)
        self.assertEqual(audit.before_values["name"], previous_name)
        self.assertEqual(audit.after_values["name"], "Petty Cash UAT")

    def test_new_coa_can_set_balanced_opening_balance(self):
        self.client.force_login(self.user)
        form_page = self.client.get(reverse("finance:accounts"), {"add": "1"})
        self.assertContains(form_page, 'name="opening_balance"')
        self.assertContains(form_page, 'name="opening_side"')
        self.assertContains(form_page, 'name="is_subaccount"')
        self.assertNotContains(form_page, 'name="is_postable"')

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "code": "990002",
                "name": "UAT Opening Account",
                "account_type": "BANK",
                "is_subaccount": "on",
                "parent": str(self.cash.parent_id),
                "currency": "IDR",
                "is_active": "on",
                "opening_balance": "250000",
                "opening_side": "DEBIT",
            },
        )

        self.assertRedirects(response, reverse("finance:accounts"))
        account = Account.objects.get(code="990002")
        opening = JournalEntry.objects.get(number="OPENING-20260831")
        line = opening.lines.get(account=account)
        offset = opening.lines.get(account__code="300001")
        self.assertEqual(line.debit, Decimal("250000"))
        self.assertEqual(offset.credit, Decimal("250000"))
        self.assertEqual(opening.debit_total, opening.credit_total)
        self.assertEqual(opening.status, JournalEntry.Status.DRAFT)
        self.assertTrue(account.is_postable)
        self.assertEqual(account.parent_id, self.cash.parent_id)
        self.assertTrue(
            AuditEvent.objects.filter(action="finance_opening_balance_updated", entity_id=account.id).exists()
        )

    def test_new_coa_without_subaccount_is_created_as_parent(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "code": "990003",
                "name": "UAT Parent Account",
                "account_type": "OASS",
                "parent": str(self.cash.parent_id),
                "currency": "IDR",
                "is_active": "on",
                "opening_balance": "0",
                "opening_side": "DEBIT",
            },
        )

        self.assertRedirects(response, reverse("finance:accounts"))
        account = Account.objects.get(code="990003")
        self.assertFalse(account.is_postable)
        self.assertIsNone(account.parent_id)

    def test_new_subaccount_requires_parent(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "code": "990004",
                "name": "UAT Child Without Parent",
                "account_type": "OASS",
                "is_subaccount": "on",
                "currency": "IDR",
                "is_active": "on",
                "opening_balance": "0",
                "opening_side": "DEBIT",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilih akun induk untuk membuat Sub-account.")
        self.assertFalse(Account.objects.filter(code="990004").exists())

    def test_edit_coa_prefills_its_direct_opening_balance(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("finance:accounts"), {"edit": self.cash.id})

        self.assertEqual(response.context["account_form"]["opening_balance"].value(), Decimal("15181.000000"))
        self.assertEqual(response.context["account_form"]["opening_side"].value(), "DEBIT")

    def test_used_account_cannot_be_changed_into_parent(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "account_id": str(self.cash.id),
                "code": self.cash.code,
                "name": self.cash.name,
                "account_type": self.cash.account_type,
                "parent": str(self.cash.parent_id),
                "currency": "IDR",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sudah dipakai jurnal")
        self.cash.refresh_from_db()
        self.assertTrue(self.cash.is_postable)

    def test_parent_account_cannot_be_changed_into_transaction_account(self):
        self.client.force_login(self.user)
        parent = self.cash.parent

        response = self.client.post(
            reverse("finance:accounts"),
            {
                "account_id": str(parent.id),
                "code": parent.code,
                "name": parent.name,
                "account_type": parent.account_type,
                "currency": "IDR",
                "is_postable": "on",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "memiliki akun turunan")
        parent.refresh_from_db()
        self.assertFalse(parent.is_postable)


class FinanceCutoverImportTests(TestCase):
    def setUp(self):
        JournalLine.objects.all().delete()
        JournalEntry.objects.all().delete()
        Account.objects.update(parent=None)
        Account.objects.all().delete()

    def _write_sources(self, directory):
        coa_path = Path(directory) / "coa.xlsx"
        coa = Workbook()
        sheet = coa.active
        sheet.append(["No.", "Tipe Akun", "Kode Perkiraan", "Nama", "Akun Induk", "Mata Uang"])
        sheet.append([1, "OASS", "1000", "Assets", "", "IDR"])
        sheet.append([2, "BANK", "1001", "Cash", "1000", "IDR"])
        sheet.append([3, "EQTY", "3001", "Capital", "", "IDR"])
        coa.save(coa_path)

        tb_path = Path(directory) / "tb.xlsx"
        tb = Workbook()
        sheet = tb.active
        sheet.cell(5, 2, "Kode")
        sheet.cell(5, 4, "Nama")
        sheet.cell(5, 6, "Saldo Awal (Debit)")
        sheet.cell(5, 8, "Saldo Awal (Kredit)")
        sheet.cell(5, 10, "Perubahan Debit")
        sheet.cell(5, 12, "Perubahan Kredit")
        sheet.cell(5, 14, "Saldo Akhir (Debit)")
        sheet.cell(5, 16, "Saldo Akhir (Kredit)")
        for row_number, code, name, debit, credit in (
            (6, "1000", "Assets", 100, 0),
            (7, "1001", "Cash", 100, 0),
            (8, "3001", "Capital", 0, 100),
        ):
            sheet.cell(row_number, 2, code)
            sheet.cell(row_number, 4, name)
            sheet.cell(row_number, 10, debit)
            sheet.cell(row_number, 12, credit)
            sheet.cell(row_number, 14, debit)
            sheet.cell(row_number, 16, credit)
        tb.save(tb_path)
        return coa_path, tb_path

    def test_cutover_import_stages_balanced_opening_without_posting(self):
        with TemporaryDirectory() as directory:
            coa_path, tb_path = self._write_sources(directory)
            entry = stage_finance_cutover(
                coa_path=coa_path,
                trial_balance_path=tb_path,
                cutoff_date=date(2026, 8, 31),
            )
        self.assertEqual(Account.objects.count(), 3)
        self.assertFalse(Account.objects.get(code="1000").is_postable)
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)
        self.assertEqual(entry.entry_date, date(2026, 9, 1))
        self.assertEqual(entry.lines.count(), 2)
        self.assertEqual(entry.debit_total, entry.credit_total)
        self.assertEqual(entry.source_metadata["cutoff_date"], "2026-08-31")
