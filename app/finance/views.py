from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from audit.services import record_audit
from accounts.access import can_access_tab, module_level

from .catalog import FEATURES, FINANCE_NAV_SECTIONS
from .forms import AccountForm, JournalEntryForm, JournalLineFormSet
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
            "sections": FINANCE_NAV_SECTIONS,
        },
    )


@login_required
def account_list(request):
    can_edit = request.user.is_superuser or module_level(request.user, "finance") in {"edit", "approve"}
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden("Akun ini hanya memiliki akses lihat.")
        account_form = AccountForm(request.POST)
        if account_form.is_valid():
            account = account_form.save()
            record_audit(
                actor=request.user,
                action="finance_account_created",
                entity_type="finance.account",
                entity_id=account.id,
                after_values={
                    "code": account.code,
                    "name": account.name,
                    "account_type": account.account_type,
                    "parent": account.parent.code if account.parent else "",
                    "currency": account.currency,
                    "is_postable": account.is_postable,
                },
            )
            messages.success(request, f"COA {account.code} · {account.name} berhasil ditambahkan.")
            return redirect("finance:accounts")
    else:
        account_form = AccountForm()
    query = request.GET.get("q", "").strip()
    accounts = Account.objects.select_related("parent")
    if query:
        accounts = accounts.filter(Q(code__icontains=query) | Q(name__icontains=query))
    return render(
        request,
        "finance/accounts.html",
        {
            "accounts": accounts,
            "query": query,
            "can_edit": can_edit,
            "account_form": account_form,
            "show_account_form": account_form.is_bound or request.GET.get("add") == "1",
        },
    )


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
    workflow = (request.POST.get("workflow") or request.GET.get("workflow") or "").strip()
    workflow_labels = {
        feature["workflow"]: feature["title"] for feature in FEATURES.values() if feature["workflow"]
    }
    if workflow not in workflow_labels:
        workflow = ""
    entry = JournalEntry(source=JournalEntry.Source.MANUAL)
    if request.method == "POST":
        form = JournalEntryForm(request.POST, instance=entry)
        formset = JournalLineFormSet(request.POST, instance=entry)
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                entry = form.save(commit=False)
                entry.number = next_journal_number(entry.entry_date)
                entry.created_by = request.user
                entry.source_metadata = {
                    **({"workflow": workflow} if workflow else {}),
                    **({"environment": "UAT"} if settings.FINANCE_UAT_MODE else {}),
                }
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
        form = JournalEntryForm(
            instance=entry,
            initial={
                "entry_date": date.today(),
                "description": workflow_labels.get(workflow, ""),
            },
        )
        formset = JournalLineFormSet(instance=entry)
    return render(
        request,
        "finance/journal_form.html",
        {"form": form, "formset": formset, "workflow": workflow, "workflow_label": workflow_labels.get(workflow)},
    )


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


def _money_rows(rows, account_types):
    return [
        row
        for row in rows
        if row["account"].is_postable and row["account"].account_type in account_types and row["net"]
    ]


@login_required
def feature(request, slug):
    spec = FEATURES.get(slug)
    if not spec:
        return HttpResponseForbidden("Fitur Finance tidak ditemukan.")
    if not can_access_tab(request.user, "finance", spec["tab"]):
        return HttpResponseForbidden("Akun ini tidak memiliki akses ke tab tersebut.")

    today = date.today()
    start = _selected_date(request, "start", date(today.year, today.month, 1))
    end = _selected_date(request, "end", today)
    as_of = _selected_date(request, "as_of", today)
    context = {
        "feature": spec,
        "slug": slug,
        "start": start,
        "end": end,
        "as_of": as_of,
        "columns": [],
        "rows": [],
        "metrics": [],
        "data_note": "Flow input dan approval akan mengikuti detail kerja tim Finance sebelum diaktifkan.",
    }

    if spec["workflow"]:
        journals = JournalEntry.objects.filter(source_metadata__workflow=spec["workflow"]).prefetch_related("lines")[:200]
        context.update(
            columns=("Tanggal", "No. Jurnal", "Keterangan", "Nilai", "Status"),
            rows=[
                (entry.entry_date, entry.number, entry.description, entry.debit_total, entry.get_status_display())
                for entry in journals
            ],
            action={
                "label": f"Buat {spec['title']}",
                "href": f"{reverse('finance:journal_create')}?workflow={spec['workflow']}",
            },
            data_note="Transaksi disimpan sebagai jurnal Draft dan baru masuk laporan setelah di-Approve & Post.",
        )
    elif slug in {"suppliers", "warehouses", "items-services"}:
        from master_data.models import SKU, Supplier, Warehouse

        if slug == "suppliers":
            rows = Supplier.objects.all()[:300]
            context.update(
                columns=("Kode", "Supplier", "Status"),
                rows=[(row.code, row.name, "Aktif" if row.is_active else "Nonaktif") for row in rows],
                metrics=(("Total supplier", Supplier.objects.count()),),
                data_note="Menggunakan master supplier canonical yang sama dengan Purchase Order.",
            )
        elif slug == "warehouses":
            rows = Warehouse.objects.all()[:100]
            context.update(
                columns=("Kode", "Warehouse", "Status"),
                rows=[(row.code, row.name, "Aktif" if row.is_active else "Nonaktif") for row in rows],
                metrics=(("Total warehouse", Warehouse.objects.count()),),
                data_note="Menggunakan master warehouse canonical yang sama dengan modul Operation.",
            )
        else:
            rows = SKU.objects.select_related("product_variant__product")
            context.update(
                columns=("SKU", "Product", "Variant", "COGS", "Retail Price"),
                rows=[
                    (
                        row.sku,
                        row.product_variant.product.name,
                        row.product_variant.name,
                        row.current_master_cogs,
                        row.current_retail_price,
                    )
                    for row in rows
                ],
                metrics=(("Total SKU", SKU.objects.count()),),
                data_note="Menggunakan Bank Data canonical; perubahan master tetap dilakukan dari Master Data.",
            )
    elif slug in {"sales-invoice", "sales-invoice-list"}:
        from sales.models import SalesOrder

        orders = SalesOrder.objects.annotate(
            gross=Sum("lines__total_gross_sales", filter=Q(lines__is_counted=True)),
            net=Sum("lines__total_net_sales", filter=Q(lines__is_counted=True)),
        )[:200]
        context.update(
            columns=("Tanggal", "Source", "No. Pesanan", "Status", "Gross", "Net"),
            rows=[(row.order_date, row.display_source, row.order_number, row.current_status, row.gross or 0, row.net or 0) for row in orders],
            metrics=(("Invoice/order", SalesOrder.objects.count()),),
            data_note="Daftar awal memakai canonical Sales order. Nomor invoice Finance dan jurnal piutang belum dibuat otomatis.",
        )
    elif slug in {"sales-return", "sales-return-per-item"}:
        from sales.models import SalesOrderLine

        lines = SalesOrderLine.objects.filter(order__current_status__iexact="Retur").select_related("order")[:300]
        context.update(
            columns=("Tanggal", "Source", "No. Pesanan", "SKU", "Product", "Qty", "Net"),
            rows=[
                (
                    row.order.order_date,
                    row.order.display_source,
                    row.order.order_number,
                    row.sku_code_snapshot,
                    row.product_name_snapshot,
                    row.quantity,
                    row.total_net_sales,
                )
                for row in lines
            ],
            metrics=(("Baris return", SalesOrderLine.objects.filter(order__current_status__iexact="Retur").count()),),
            data_note="Return mengikuti status canonical Sales; pengakuan kas/piutang menunggu workflow Finance.",
        )
    elif slug in {"purchase-invoice", "purchase-invoice-list"}:
        from purchasing.models import PurchaseOrder

        value = ExpressionWrapper(
            F("lines__ordered_qty") * F("lines__cogs_snapshot"),
            output_field=DecimalField(max_digits=24, decimal_places=4),
        )
        orders = PurchaseOrder.objects.select_related("supplier").annotate(
            total_qty=Sum("lines__ordered_qty"), total_value=Sum(value)
        )[:200]
        context.update(
            columns=("PO", "Supplier", "Need Month", "Qty", "Nilai PO", "Status"),
            rows=[
                (row.po_number or "Draft", row.supplier.name, row.need_month, row.total_qty or 0, row.total_value or 0, row.get_status_display())
                for row in orders
            ],
            metrics=(("Purchase Order", PurchaseOrder.objects.count()),),
            data_note="Daftar awal memakai Purchase Order canonical. Invoice supplier dan jurnal utang belum diposting otomatis.",
        )
    elif slug in {"cash-flow", "general-ledger-summary", "financial-ratio", "outstanding-invoice", "aging-receivable", "outstanding-purchase-invoice", "account-payable-aging", "account-payable-aging-detail", "supplier-payable-per-month"}:
        if slug == "cash-flow":
            from .models import JournalLine

            lines = JournalLine.objects.filter(
                entry__status=JournalEntry.Status.POSTED,
                entry__entry_date__range=(start, end),
                account__account_type="BANK",
            ).exclude(entry__source=JournalEntry.Source.OPENING).select_related("entry", "account")[:500]
            context.update(
                columns=("Tanggal", "Jurnal", "Akun Kas/Bank", "Keterangan", "Masuk", "Keluar"),
                rows=[(row.entry.entry_date, row.entry.number, row.account.name, row.entry.description, row.debit, row.credit) for row in lines],
                metrics=(("Kas masuk", sum((row.debit for row in lines), Decimal("0"))), ("Kas keluar", sum((row.credit for row in lines), Decimal("0")))),
                data_note="Metode direct dari mutasi akun bertipe Kas & Bank pada jurnal Posted.",
            )
        elif slug == "general-ledger-summary":
            balances = account_balances(start_date=start, end_date=end)
            rows = [row for row in balances if row["account"].is_postable and (row["debit"] or row["credit"])]
            context.update(
                columns=("Kode", "Akun", "Debit", "Kredit", "Saldo"),
                rows=[(row["account"].code, row["account"].name, row["debit"], row["credit"], row["net"]) for row in rows],
                data_note="Hanya transaksi Posted pada periode terpilih.",
            )
        elif slug == "financial-ratio":
            balances = account_balances(end_date=as_of)
            assets = sum((row["net"] for row in _money_rows(balances, {"BANK", "AREC", "INTR", "OASS", "OCAS", "FASS", "DEPR"})), Decimal("0"))
            current_assets = sum((row["net"] for row in _money_rows(balances, {"BANK", "AREC", "INTR", "OASS", "OCAS"})), Decimal("0"))
            liabilities = sum((-row["net"] for row in _money_rows(balances, {"APAY", "OCLY", "LTLY"})), Decimal("0"))
            current_liabilities = sum((-row["net"] for row in _money_rows(balances, {"APAY", "OCLY"})), Decimal("0"))
            equity = assets - liabilities
            context.update(
                columns=("Ratio", "Nilai"),
                rows=(
                    ("Current Ratio", current_assets / current_liabilities if current_liabilities else None),
                    ("Debt to Equity", liabilities / equity if equity else None),
                    ("Debt to Asset", liabilities / assets if assets else None),
                ),
                data_note="Ratio dihitung dari saldo akun Posted per tanggal terpilih.",
            )
        else:
            balances = account_balances(end_date=as_of)
            account_types = {"AREC"} if slug in {"outstanding-invoice", "aging-receivable"} else {"APAY"}
            rows = _money_rows(balances, account_types)
            context.update(
                columns=("Kode", "Akun", "Saldo"),
                rows=[(row["account"].code, row["account"].name, abs(row["net"])) for row in rows],
                metrics=(("Total", sum((abs(row["net"]) for row in rows), Decimal("0"))),),
                data_note="Saldo akun tersedia. Aging per invoice/supplier menunggu tanggal jatuh tempo dari workflow invoice.",
            )
    elif slug == "inventory-valuation":
        from inventory.services.reporting import filtered_skus, inventory_summary_rows

        rows = inventory_summary_rows(filtered_skus(), as_of_date=as_of)
        context.update(
            columns=("SKU", "Product", "Ending Qty", "FIFO Qty", "FIFO Value", "Status"),
            rows=[
                (row["sku"].sku, row["sku"].product_variant.product.name, row["balance"], row["fifo_qty"], row["fifo_value"], row["stock_status"])
                for row in rows
            ],
            metrics=(("FIFO Value", sum((row["fifo_value"] for row in rows), Decimal("0"))), ("SKU", len(rows))),
            data_note="Valuasi memakai FIFO canonical Warehouse pada tanggal terpilih.",
        )
    elif slug == "inventory-aging":
        from inventory.services.aging import po_aging_snapshot
        from purchasing.models import PurchaseOrder

        orders = list(PurchaseOrder.objects.filter(status=PurchaseOrder.Status.RELEASED).select_related("supplier").prefetch_related("lines")[:200])
        context.update(
            columns=("PO", "Supplier", "Remaining Qty", "Age", "Status"),
            rows=[
                (po.po_number, po.supplier.name, snapshot["po_remaining_qty"], f"{snapshot['age_days']} hari", snapshot["status"])
                for po in orders
                for snapshot in (po_aging_snapshot(po, as_of),)
            ],
            metrics=(("PO aktif", len(orders)),),
            data_note="Aging memakai outstanding inbound dan sisa FIFO per PO.",
        )

    return render(request, "finance/feature.html", context)
