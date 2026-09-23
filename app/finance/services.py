from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from audit.services import record_audit

from .models import (
    Account,
    FINANCE_OPENING_DATE,
    JournalEntry,
    JournalLine,
    JournalNumberSequence,
    ProductSalesAccount,
    SalesJournalAllocation,
)


MONEY_QUANTUM = Decimal("0.000001")
OPENING_ENTRY_NUMBER = "OPENING-20260831"
OPENING_OFFSET_ACCOUNT_CODE = "300001"
SALES_JOURNAL_WORKFLOW = "SALES_JOURNAL_BATCH"


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
    while JournalEntry.objects.filter(
        number=f"JV-{entry_date:%Y%m}-{sequence.last_number:04d}"
    ).exists():
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
    include_opening_draft=False,
    exclude_opening=False,
    source=None,
    exclude_system_workflows=(),
):
    entries = JournalEntry.objects.all()
    if not include_draft:
        if include_opening_draft:
            entries = entries.filter(
                Q(status=JournalEntry.Status.POSTED) | Q(source=JournalEntry.Source.OPENING)
            )
        else:
            entries = entries.filter(status=JournalEntry.Status.POSTED)
    if exclude_opening:
        entries = entries.exclude(source=JournalEntry.Source.OPENING)
    if source:
        entries = entries.filter(source=source)
    if exclude_system_workflows:
        entries = entries.exclude(
            source=JournalEntry.Source.SYSTEM,
            source_metadata__workflow__in=tuple(exclude_system_workflows),
        )
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


def _validated_account(account_id, *, allowed_types, parent_code=None, label):
    account = Account.objects.select_related("parent").filter(
        pk=account_id,
        is_active=True,
        is_postable=True,
        account_type__in=allowed_types,
    ).first()
    if not account or (parent_code and getattr(account.parent, "code", None) != parent_code):
        raise ValidationError(f"{label} tidak valid atau tidak dapat dipakai transaksi.")
    return account


@transaction.atomic
def create_sales_journal_draft(
    *,
    start_date,
    end_date,
    receipt_account_id,
    actor,
    source_groups=(),
    sources=(),
    categories=(),
):
    if start_date > end_date:
        raise ValidationError("Tanggal mulai tidak boleh melewati tanggal selesai.")
    if start_date < FINANCE_OPENING_DATE:
        raise ValidationError("Jurnal Sales Finance dimulai 1 September 2026.")
    receipt_account = _validated_account(
        receipt_account_id,
        allowed_types={"AREC", "BANK"},
        label="Akun receipt/piutang",
    )
    discount_account = _validated_account(
        Account.objects.filter(code="440101").values_list("id", flat=True).first(),
        allowed_types={"REVE"},
        parent_code="4401",
        label="Akun diskon",
    )
    cogs_account = _validated_account(
        Account.objects.filter(code="5101").values_list("id", flat=True).first(),
        allowed_types={"COGS"},
        label="Akun COGS",
    )
    inventory_account = _validated_account(
        Account.objects.filter(code="110401").values_list("id", flat=True).first(),
        allowed_types={"INTR"},
        label="Akun persediaan",
    )

    from master_data.models import SKU
    from sales.models import SalesOrderLine

    sales_lines = SalesOrderLine.objects.select_for_update().filter(
        is_counted=True,
        order__order_date__range=(start_date, end_date),
        finance_journal_allocation__isnull=True,
    )
    source_groups = tuple(dict.fromkeys(group for group in source_groups if group in {"Marketplace", "Other"}))
    sources = tuple(dict.fromkeys(source for source in sources if source))
    categories = tuple(dict.fromkeys(category for category in categories if category))
    if sources:
        sales_lines = sales_lines.filter(order__source_label__in=sources)
    if source_groups:
        group_filter = Q()
        if "Marketplace" in source_groups:
            group_filter |= Q(order__source__in=["Shopee", "Tiktok"])
        if "Other" in source_groups:
            group_filter |= Q(order__source="Other")
        sales_lines = sales_lines.filter(group_filter)
    if categories:
        sales_lines = sales_lines.filter(category_snapshot__in=categories)
    sales_lines = list(
        sales_lines
        .select_related("order")
        .order_by("order__order_date", "order__order_number", "sku_code_snapshot")
    )
    if not sales_lines:
        raise ValidationError("Tidak ada transaksi Sales yang belum dijurnal untuk periode dan filter ini.")

    snapshot_product_ids = dict(
        SKU.objects.filter(sku__in={line.sku_code_snapshot for line in sales_lines})
        .values_list("sku", "product_variant__product_id")
    )
    product_ids = {
        snapshot_product_ids.get(line.sku_code_snapshot)
        for line in sales_lines
    }
    product_ids.discard(None)
    account_id_by_product = dict(
        ProductSalesAccount.objects.filter(product_id__in=product_ids)
        .values_list("product_id", "sales_account_id")
    )
    sales_accounts = {
        account_id: _validated_account(
            account_id,
            allowed_types={"REVE"},
            parent_code="4100",
            label="Akun Sales dari Sales Setting",
        )
        for account_id in set(account_id_by_product.values())
    }

    sales_values = defaultdict(lambda: Decimal("0"))
    gross_total = Decimal("0")
    net_total = Decimal("0")
    cogs_total = Decimal("0")
    missing_sales_settings = set()
    missing_cogs_lines = []
    allocation_values = []
    for line in sales_lines:
        gross = (line.total_gross_sales or Decimal("0")).quantize(MONEY_QUANTUM)
        net = (line.total_net_sales or Decimal("0")).quantize(MONEY_QUANTUM)
        if line.total_cogs is None:
            missing_cogs_lines.append(line.sku_code_snapshot or line.product_name_snapshot)
            continue
        cogs = line.total_cogs.quantize(MONEY_QUANTUM)
        product_id = snapshot_product_ids.get(line.sku_code_snapshot)
        sales_account_id = account_id_by_product.get(product_id)
        if not sales_account_id:
            missing_sales_settings.add(line.sku_code_snapshot or line.product_name_snapshot)
        else:
            sales_values[sales_account_id] += gross
        gross_total += gross
        net_total += net
        cogs_total += cogs
        allocation_values.append((line, gross, net, cogs))

    if missing_cogs_lines:
        sample = ", ".join(sorted(set(missing_cogs_lines))[:5])
        raise ValidationError(f"COGS belum tersedia untuk transaksi: {sample}.")
    if missing_sales_settings:
        raise ValidationError(
            "Atur Sales Account di Sales Setting untuk: "
            + ", ".join(sorted(missing_sales_settings)[:5])
            + "."
        )
    discount_total = (gross_total - net_total).quantize(MONEY_QUANTUM)
    if discount_total < 0:
        raise ValidationError("Net Sales tidak boleh melebihi Gross Sales.")
    if gross_total <= 0:
        raise ValidationError("Gross Sales periode terpilih bernilai nol.")

    entry = JournalEntry(
        number=next_journal_number(end_date),
        entry_date=end_date,
        description=f"Sales {start_date:%d %b %Y} – {end_date:%d %b %Y}",
        reference=f"Sales {start_date:%Y-%m-%d}/{end_date:%Y-%m-%d}",
        source=JournalEntry.Source.SYSTEM,
        source_metadata={
            "workflow": SALES_JOURNAL_WORKFLOW,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "sales_mode": "product_setting",
            "cogs_mode": "total",
            "source_groups": list(source_groups),
            "sources": list(sources),
            "categories": list(categories),
            "sales_line_count": len(sales_lines),
        },
        created_by=actor,
    )
    entry.full_clean()
    entry.save()

    journal_lines = []
    if net_total > 0:
        journal_lines.append((receipt_account, net_total, Decimal("0"), "Sales Receivable / Receipt"))
    if discount_total > 0:
        journal_lines.append((discount_account, discount_total, Decimal("0"), "Diskon Penjualan"))
    for account_id in sorted(sales_values, key=lambda value: sales_accounts[value].code):
        account = sales_accounts[account_id]
        journal_lines.append(
            (account, Decimal("0"), sales_values[account_id], f"Gross Sales · {account.name}")
        )
    if cogs_total > 0:
        journal_lines.append((cogs_account, cogs_total, Decimal("0"), "COGS · Total"))
    if cogs_total > 0:
        journal_lines.append((inventory_account, Decimal("0"), cogs_total, "Persediaan keluar karena Sales"))

    for number, (account, debit, credit, description) in enumerate(journal_lines, 1):
        journal_line = JournalLine(
            entry=entry,
            line_number=number,
            account=account,
            description=description,
            debit=debit,
            credit=credit,
        )
        journal_line.full_clean()
        journal_line.save()

    debit_total = sum((line[1] for line in journal_lines), Decimal("0")).quantize(MONEY_QUANTUM)
    credit_total = sum((line[2] for line in journal_lines), Decimal("0")).quantize(MONEY_QUANTUM)
    if debit_total != credit_total:
        raise ValidationError("Jurnal Sales tidak seimbang dan tidak disimpan.")

    SalesJournalAllocation.objects.bulk_create(
        [
            SalesJournalAllocation(
                entry=entry,
                sales_line=line,
                gross_sales=gross,
                net_sales=net,
                cogs=cogs,
            )
            for line, gross, net, cogs in allocation_values
        ]
    )
    record_audit(
        actor=actor,
        action="finance_sales_journal_draft_created",
        entity_type="finance.journal_entry",
        entity_id=entry.id,
        after_values={
            "journal_number": entry.number,
            "sales_lines": len(sales_lines),
            "gross_sales": str(gross_total),
            "net_sales": str(net_total),
            "discount": str(discount_total),
            "cogs": str(cogs_total),
        },
        metadata=entry.source_metadata,
    )
    return entry
