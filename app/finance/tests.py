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
from .models import Account, JournalEntry, JournalLine
from .services import account_balances, post_journal


class FinanceJournalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="finance-admin", password="test")
        self.cash = Account.objects.create(code="110101", name="Cash", account_type="BANK")
        self.capital = Account.objects.create(code="300001", name="Capital", account_type="EQTY")
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
            number="OPENING-20260831",
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

    def test_inventory_account_is_included_as_balance_sheet_asset(self):
        inventory = Account.objects.create(code="110401", name="Inventory", account_type="INTR")
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


class FinanceCutoverImportTests(TestCase):
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
