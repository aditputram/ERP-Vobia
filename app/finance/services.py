from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from audit.services import record_audit

from .models import Account, JournalEntry, JournalLine, JournalNumberSequence


MONEY_QUANTUM = Decimal("0.000001")
OPENING_ENTRY_NUMBER = "OPENING-20260831"
OPENING_OFFSET_ACCOUNT_CODE = "300001"


def account_opening_balance(account):
    if not account:
        return {"opening_balance": Decimal("0"), "opening_side": "DEBIT"}
    line = JournalLine.objects.filter(entry__number=OPENING_ENTRY_NUMBER, account=account).first()
    if not line:
        return {"opening_balance": Decimal("0"), "opening_side": "DEBIT"}
    return {
        "opening_balance": line.debit or line.credit,
        "opening_side": "DEBIT" if line.debit else "CREDIT",
    }


def _write_opening_line(entry, account, net, line=None):
    if not net:
        if line:
            line.delete()
        return
    line = line or JournalLine(
        entry=entry,
        account=account,
        line_number=(entry.lines.aggregate(value=Max("line_number"))["value"] or 0) + 1,
    )
    line.description = "Opening 1 September 2026"
    line.debit = max(net, Decimal("0"))
    line.credit = max(-net, Decimal("0"))
    line.full_clean()
    line.save()


@transaction.atomic
def set_account_opening_balance(*, account, amount, side, actor):
    amount = (amount or Decimal("0")).quantize(MONEY_QUANTUM)
    entry = JournalEntry.objects.select_for_update().filter(number=OPENING_ENTRY_NUMBER).first()
    if not entry:
        if amount:
            raise ValidationError("Opening journal Finance belum tersedia.")
        return
    if entry.status != JournalEntry.Status.DRAFT:
        raise ValidationError("Saldo awal tidak dapat diubah karena opening journal sudah Posted.")

    direct_lines = list(entry.lines.select_for_update().filter(account=account))
    if len(direct_lines) > 1:
        raise ValidationError("Akun ini memiliki lebih dari satu baris opening dan harus direkonsiliasi.")
    direct_line = direct_lines[0] if direct_lines else None
    old_net = (direct_line.debit - direct_line.credit) if direct_line else Decimal("0")
    new_net = amount if side == "DEBIT" else -amount
    if account.code == OPENING_OFFSET_ACCOUNT_CODE:
        if old_net != new_net:
            raise ValidationError("Saldo akun Equitas Saldo Awal dikelola otomatis oleh sistem.")
        return
    if old_net == new_net:
        return

    offset = Account.objects.select_for_update().get(code=OPENING_OFFSET_ACCOUNT_CODE)
    if not offset.is_active or not offset.is_postable:
        raise ValidationError("Akun Equitas Saldo Awal harus aktif dan dapat dipakai transaksi.")
    offset_lines = list(entry.lines.select_for_update().filter(account=offset))
    if len(offset_lines) > 1:
        raise ValidationError("Akun Equitas Saldo Awal memiliki baris ganda dan harus direkonsiliasi.")
    offset_line = offset_lines[0] if offset_lines else None
    offset_net = (offset_line.debit - offset_line.credit) if offset_line else Decimal("0")

    _write_opening_line(entry, account, new_net, direct_line)
    _write_opening_line(entry, offset, offset_net - (new_net - old_net), offset_line)
    record_audit(
        actor=actor,
        action="finance_opening_balance_updated",
        entity_type="finance.account",
        entity_id=account.id,
        before_values={"amount": str(abs(old_net)), "side": "DEBIT" if old_net >= 0 else "CREDIT"},
        after_values={"amount": str(amount), "side": side},
        metadata={"journal_number": entry.number, "offset_account": offset.code},
    )


@transaction.atomic
def next_journal_number(entry_date):
    period = entry_date.replace(day=1)
    sequence, _ = JournalNumberSequence.objects.select_for_update().get_or_create(period=period)
    sequence.last_number += 1
    sequence.save(update_fields=("last_number", "updated_at"))
    return f"JV-{entry_date:%Y%m}-{sequence.last_number:04d}"


@transaction.atomic
def post_journal(entry_id, actor):
    entry = JournalEntry.objects.select_for_update().get(pk=entry_id)
    if entry.status != JournalEntry.Status.DRAFT:
        raise ValidationError("Jurnal ini sudah diposting.")
    if (
        entry.source == JournalEntry.Source.OPENING
        and entry.source_metadata.get("reconciliation_status") != "RECONCILED"
    ):
        raise ValidationError("Opening journal belum boleh diposting sebelum akun kontrol selesai direkonsiliasi.")
    lines = list(entry.lines.select_related("account"))
    if len(lines) < 2:
        raise ValidationError("Jurnal minimal memiliki dua baris.")
    for line in lines:
        line.full_clean()
    debit = sum((line.debit for line in lines), Decimal("0")).quantize(MONEY_QUANTUM)
    credit = sum((line.credit for line in lines), Decimal("0")).quantize(MONEY_QUANTUM)
    if debit <= 0 or debit != credit:
        raise ValidationError("Total Debit dan Kredit harus sama dan lebih dari nol.")
    entry.status = JournalEntry.Status.POSTED
    entry.posted_by = actor
    entry.posted_at = timezone.now()
    entry.save(update_fields=("status", "posted_by", "posted_at"))
    record_audit(
        actor=actor,
        action="finance_journal_posted",
        entity_type="finance.journal_entry",
        entity_id=entry.id,
        after_values={"number": entry.number, "debit": str(debit), "credit": str(credit)},
    )
    return entry


def account_balances(
    *,
    start_date=None,
    end_date=None,
    include_draft=False,
    exclude_opening=False,
    source=None,
):
    entries = JournalEntry.objects.all()
    if not include_draft:
        entries = entries.filter(status=JournalEntry.Status.POSTED)
    if exclude_opening:
        entries = entries.exclude(source=JournalEntry.Source.OPENING)
    if source:
        entries = entries.filter(source=source)
    if start_date:
        entries = entries.filter(entry_date__gte=start_date)
    if end_date:
        entries = entries.filter(entry_date__lte=end_date)
    direct = {
        row["lines__account_id"]: {
            "debit": row["debit"] or Decimal("0"),
            "credit": row["credit"] or Decimal("0"),
        }
        for row in entries.values("lines__account_id")
        .annotate(debit=Sum("lines__debit"), credit=Sum("lines__credit"))
        .values("lines__account_id", "debit", "credit")
        if row["lines__account_id"]
    }
    accounts = list(Account.objects.select_related("parent"))
    by_id = {account.id: account for account in accounts}
    totals = defaultdict(lambda: {"debit": Decimal("0"), "credit": Decimal("0")})
    for account_id, amount in direct.items():
        current = by_id.get(account_id)
        while current:
            totals[current.id]["debit"] += amount["debit"]
            totals[current.id]["credit"] += amount["credit"]
            current = current.parent
    return [
        {
            "account": account,
            "debit": totals[account.id]["debit"],
            "credit": totals[account.id]["credit"],
            "net": totals[account.id]["debit"] - totals[account.id]["credit"],
            "debit_balance": max(totals[account.id]["debit"] - totals[account.id]["credit"], Decimal("0")),
            "credit_balance": max(totals[account.id]["credit"] - totals[account.id]["debit"], Decimal("0")),
        }
        for account in accounts
    ]
