from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from audit.services import record_audit
from accounts.access import module_level

from .forms import JournalEntryForm, JournalLineFormSet
from .models import Account, FINANCE_CUTOVER_DATE, JournalEntry
from .services import account_balances, next_journal_number, post_journal


def _selected_date(request, key, fallback):
    return parse_date(request.GET.get(key, "")) or fallback


@login_required
def dashboard(request):
    opening = JournalEntry.objects.filter(source=JournalEntry.Source.OPENING).first()
    posted_lines = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).aggregate(
        debit=Sum("lines__debit"), credit=Sum("lines__credit")
    )
    return render(
        request,
        "finance/dashboard.html",
        {
            "cutover_date": FINANCE_CUTOVER_DATE,
            "opening": opening,
            "account_count": Account.objects.count(),
            "postable_count": Account.objects.filter(is_postable=True).count(),
            "draft_count": JournalEntry.objects.filter(status=JournalEntry.Status.DRAFT).count(),
            "posted_count": JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).count(),
            "posted_debit": posted_lines["debit"] or Decimal("0"),
            "can_edit": request.user.is_superuser or module_level(request.user, "finance") in {"edit", "approve"},
        },
    )


@login_required
def account_list(request):
    query = request.GET.get("q", "").strip()
    accounts = Account.objects.select_related("parent")
    if query:
        accounts = accounts.filter(Q(code__icontains=query) | Q(name__icontains=query))
    return render(request, "finance/accounts.html", {"accounts": accounts, "query": query})


@login_required
def journal_list(request):
    return render(
        request,
        "finance/journals.html",
        {
            "journals": JournalEntry.objects.select_related("created_by", "posted_by").prefetch_related("lines")[:200],
            "can_edit": request.user.is_superuser or module_level(request.user, "finance") in {"edit", "approve"},
        },
    )


@login_required
def journal_create(request):
    entry = JournalEntry(source=JournalEntry.Source.MANUAL)
    if request.method == "POST":
        form = JournalEntryForm(request.POST, instance=entry)
        formset = JournalLineFormSet(request.POST, instance=entry)
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                entry = form.save(commit=False)
                entry.number = next_journal_number(entry.entry_date)
                entry.created_by = request.user
                entry.full_clean()
                entry.save()
                lines = formset.save(commit=False)
                for index, line in enumerate(lines, start=1):
                    line.entry = entry
                    line.line_number = index
                    line.full_clean()
                    line.save()
                record_audit(
                    actor=request.user,
                    action="finance_journal_created",
                    entity_type="finance.journal_entry",
                    entity_id=entry.id,
                    after_values={"number": entry.number},
                )
            messages.success(request, "Jurnal tersimpan sebagai Draft.")
            return redirect("finance:journal_detail", entry_id=entry.id)
    else:
        form = JournalEntryForm(instance=entry, initial={"entry_date": date.today()})
        formset = JournalLineFormSet(instance=entry)
    return render(request, "finance/journal_form.html", {"form": form, "formset": formset})


@login_required
def journal_detail(request, entry_id):
    entry = get_object_or_404(
        JournalEntry.objects.select_related("created_by", "posted_by").prefetch_related("lines__account"),
        pk=entry_id,
    )
    return render(
        request,
        "finance/journal_detail.html",
        {
            "entry": entry,
            "can_approve": request.user.is_superuser or module_level(request.user, "finance") == "approve",
            "posting_blocked": entry.source == JournalEntry.Source.OPENING
            and entry.source_metadata.get("reconciliation_status") != "RECONCILED",
        },
    )


@login_required
def journal_approve(request, entry_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        post_journal(entry_id, request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Jurnal berhasil diposting dan masuk ke laporan Finance.")
    return redirect("finance:journal_detail", entry_id=entry_id)


@login_required
def trial_balance(request):
    as_of = _selected_date(request, "as_of", date.today())
    include_draft = request.GET.get("mode") == "preview"
    rows = [row for row in account_balances(end_date=as_of, include_draft=include_draft) if row["debit"] or row["credit"]]
    debit = sum((max(row["net"], 0) for row in rows if row["account"].is_postable), Decimal("0"))
    credit = sum((max(-row["net"], 0) for row in rows if row["account"].is_postable), Decimal("0"))
    return render(
        request,
        "finance/trial_balance.html",
        {"rows": rows, "as_of": as_of, "include_draft": include_draft, "debit": debit, "credit": credit},
    )


@login_required
def balance_sheet(request):
    as_of = _selected_date(request, "as_of", date.today())
    include_draft = request.GET.get("mode") == "preview"
    rows = account_balances(end_date=as_of, include_draft=include_draft)
    groups = {
        "assets": [dict(row, amount=row["net"]) for row in rows if row["account"].is_postable and row["account"].account_type in {"BANK", "AREC", "INTR", "OASS", "OCAS", "FASS", "DEPR"} and row["net"]],
        "liabilities": [dict(row, amount=-row["net"]) for row in rows if row["account"].is_postable and row["account"].account_type in {"APAY", "OCLY", "LTLY"} and row["net"]],
        "equity": [dict(row, amount=-row["net"]) for row in rows if row["account"].is_postable and row["account"].account_type == "EQTY" and row["net"]],
    }
    current_earnings = sum(
        (-row["net"] for row in rows if row["account"].is_postable and row["account"].account_type in {"REVE", "OINC", "COGS", "EXPS", "OEXP"}),
        Decimal("0"),
    )
    totals = {
        "assets": sum((row["amount"] for row in groups["assets"]), Decimal("0")),
        "liabilities": sum((row["amount"] for row in groups["liabilities"]), Decimal("0")),
        "equity": sum((row["amount"] for row in groups["equity"]), Decimal("0")) + current_earnings,
    }
    return render(
        request,
        "finance/balance_sheet.html",
        {
            "groups": groups,
            "as_of": as_of,
            "include_draft": include_draft,
            "current_earnings": current_earnings,
            "totals": totals,
            "difference": totals["assets"] - totals["liabilities"] - totals["equity"],
        },
    )


@login_required
def profit_loss(request):
    start = _selected_date(request, "start", date(date.today().year, date.today().month, 1))
    end = _selected_date(request, "end", date.today())
    rows = account_balances(start_date=start, end_date=end, exclude_opening=True)
    revenue = [dict(row, amount=-row["net"]) for row in rows if row["account"].is_postable and row["account"].account_type in {"REVE", "OINC"} and row["net"]]
    expense = [dict(row, amount=row["net"]) for row in rows if row["account"].is_postable and row["account"].account_type in {"COGS", "EXPS", "OEXP"} and row["net"]]
    revenue_total = sum((row["amount"] for row in revenue), Decimal("0"))
    expense_total = sum((row["amount"] for row in expense), Decimal("0"))
    return render(
        request,
        "finance/profit_loss.html",
        {
            "start": start,
            "end": end,
            "revenue": revenue,
            "expense": expense,
            "revenue_total": revenue_total,
            "expense_total": expense_total,
            "profit": revenue_total - expense_total,
        },
    )
