from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from audit.services import record_audit
from accounts.access import can_access_tab, module_level

from .catalog import FEATURES, FINANCE_NAV_SECTIONS
from .forms import AccountForm, JournalEntryForm, JournalLineFormSet
from .models import (
    Account,
    FINANCE_CUTOVER_DATE,
    FINANCE_OPENING_DATE,
    JournalEntry,
    ProductSalesAccount,
)
from .services import (
    account_balances,
    account_opening_balance,
    next_journal_number,
    post_journal,
    set_account_opening_balance,
)


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
    edit_id = (request.POST.get("account_id") if request.method == "POST" else request.GET.get("edit")) or ""
    editing_account = None
    if edit_id and can_edit:
        editing_account = get_object_or_404(Account.objects.select_related("parent"), pk=edit_id)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden("Akun ini hanya memiliki akses lihat.")
        before_values = None
        if editing_account:
            opening_before = account_opening_balance(editing_account)
            before_values = {
                "code": editing_account.code,
                "name": editing_account.name,
                "account_type": editing_account.account_type,
                "parent": editing_account.parent.code if editing_account.parent else "",
                "currency": editing_account.currency,
                "is_postable": editing_account.is_postable,
                "is_active": editing_account.is_active,
                "opening_balance": str(opening_before["opening_balance"]),
                "opening_side": opening_before["opening_side"],
            }
        account_form = AccountForm(request.POST, instance=editing_account)
        if account_form.is_valid():
            try:
                with transaction.atomic():
                    account = account_form.save()
                    set_account_opening_balance(
                        account=account,
                        amount=account_form.cleaned_data["opening_balance"],
                        side=account_form.cleaned_data["opening_side"],
                        actor=request.user,
                    )
                    action = "finance_account_updated" if editing_account else "finance_account_created"
                    after_values = {
                        "code": account.code,
                        "name": account.name,
                        "account_type": account.account_type,
                        "parent": account.parent.code if account.parent else "",
                        "currency": account.currency,
                        "is_postable": account.is_postable,
                        "is_active": account.is_active,
                        "opening_balance": str(account_form.cleaned_data["opening_balance"] or Decimal("0")),
                        "opening_side": account_form.cleaned_data["opening_side"],
                    }
                    record_audit(
                        actor=request.user,
                        action=action,
                        entity_type="finance.account",
                        entity_id=account.id,
                        before_values=before_values,
                        after_values=after_values,
                    )
            except (ValidationError, Account.DoesNotExist) as exc:
                message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Akun Equitas Saldo Awal tidak ditemukan."
                account_form.add_error("opening_balance", message)
            else:
                verb = "diperbarui" if editing_account else "ditambahkan"
                messages.success(request, f"COA {account.code} · {account.name} berhasil {verb}.")
                return redirect("finance:accounts")
    else:
        account_form = AccountForm(instance=editing_account, initial=account_opening_balance(editing_account))
    query = request.GET.get("q", "").strip()
    accounts = Account.objects.select_related("parent")
    if query:
        accounts = accounts.filter(Q(code__icontains=query) | Q(name__icontains=query))
    accounts = list(accounts)
    opening_by_account = {
        row["account"].id: row
        for row in account_balances(
            end_date=FINANCE_OPENING_DATE,
            include_draft=True,
            source=JournalEntry.Source.OPENING,
        )
    }
    for account in accounts:
        opening = opening_by_account.get(account.id, {})
        account.opening_debit = opening.get("debit_balance", Decimal("0"))
        account.opening_credit = opening.get("credit_balance", Decimal("0"))
    return render(
        request,
        "finance/accounts.html",
        {
            "accounts": accounts,
            "query": query,
            "can_edit": can_edit,
            "account_form": account_form,
            "editing_account": editing_account,
            "show_account_form": account_form.is_bound or request.GET.get("add") == "1" or editing_account,
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
    rows = account_balances(
        end_date=as_of,
        include_draft=include_draft,
        include_opening_draft=True,
    )
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


def _month_value(value, fallback):
    try:
        return date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError):
        return fallback.replace(day=1)


def _shift_month(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _profit_loss_data(start, end, accounts):
    rows = account_balances(start_date=start, end_date=end, exclude_opening=True)
    revenue_by_account = {}
    expense_by_account = {}

    def add_amount(bucket, account, amount):
        if not account or not amount:
            return
        row = bucket.setdefault(account.id, {"account": account, "amount": Decimal("0")})
        row["amount"] += amount

    for row in rows:
        account = row["account"]
        if not account.is_postable or not row["net"]:
            continue
        if account.account_type in {"REVE", "OINC"}:
            add_amount(revenue_by_account, account, -row["net"])
        elif account.account_type in {"COGS", "EXPS", "OEXP"}:
            add_amount(expense_by_account, account, row["net"])

    from inventory.models import PhysicalReturnReceipt
    from sales.models import SalesOrderLine

    accounts_by_id = {account.id: account for account in accounts}
    accounts_by_code = {account.code: account for account in accounts}
    operation_start = max(start, FINANCE_OPENING_DATE)
    sales_totals = {"gross": Decimal("0"), "net": Decimal("0"), "cogs": Decimal("0"), "missing_cogs": 0}
    return_totals = {"value": Decimal("0"), "cogs": Decimal("0"), "count": 0, "missing_cogs": 0}
    sales_invoice_count = 0
    unmapped_sales_count = 0
    unmapped_sales_amount = Decimal("0")
    if operation_start <= end:
        sales_lines = SalesOrderLine.objects.filter(
            is_counted=True,
            order__order_date__range=(operation_start, end),
        )
        sales_totals = sales_lines.aggregate(
            gross=Sum("total_gross_sales"),
            net=Sum("total_net_sales"),
            cogs=Sum("total_cogs"),
            missing_cogs=Count("id", filter=Q(total_cogs__isnull=True)),
        )
        sales_totals = {
            "gross": sales_totals["gross"] or Decimal("0"),
            "net": sales_totals["net"] or Decimal("0"),
            "cogs": sales_totals["cogs"] or Decimal("0"),
            "missing_cogs": sales_totals["missing_cogs"],
        }
        sales_invoice_count = sales_lines.values("order_id").distinct().count()
        unmapped_sales = sales_lines.filter(
            sku__product_variant__product__finance_sales_setting__isnull=True
        )
        unmapped_sales_count = unmapped_sales.count()
        unmapped_sales_amount = (
            unmapped_sales.aggregate(value=Sum("total_gross_sales"))["value"] or Decimal("0")
        )
        for group in sales_lines.values(
            "sku__product_variant__product__finance_sales_setting__sales_account_id"
        ).annotate(gross=Sum("total_gross_sales")):
            account = accounts_by_id.get(
                group["sku__product_variant__product__finance_sales_setting__sales_account_id"]
            )
            if account:
                add_amount(revenue_by_account, account, group["gross"] or Decimal("0"))

        return_value = ExpressionWrapper(
            F("quantity") * F("sales_line__net_unit_price"),
            output_field=DecimalField(max_digits=24, decimal_places=4),
        )
        receipts = PhysicalReturnReceipt.objects.filter(received_date__range=(operation_start, end))
        return_totals = receipts.aggregate(
            value=Sum(return_value),
            cogs=Sum(
                "movement__allocated_cost",
                filter=Q(condition=PhysicalReturnReceipt.Condition.SELLABLE),
            ),
            count=Count("id"),
            missing_cogs=Count(
                "id",
                filter=Q(
                    condition=PhysicalReturnReceipt.Condition.SELLABLE,
                    movement__isnull=True,
                ),
            ),
        )
        return_totals = {
            "value": return_totals["value"] or Decimal("0"),
            "cogs": return_totals["cogs"] or Decimal("0"),
            "count": return_totals["count"],
            "missing_cogs": return_totals["missing_cogs"],
        }

    add_amount(
        revenue_by_account,
        accounts_by_code.get("440101"),
        -(sales_totals["gross"] - sales_totals["net"]),
    )
    add_amount(revenue_by_account, accounts_by_code.get("440103"), -return_totals["value"])
    add_amount(
        expense_by_account,
        accounts_by_code.get("5101"),
        sales_totals["cogs"] - return_totals["cogs"],
    )

    revenue = sorted(revenue_by_account.values(), key=lambda row: row["account"].code)
    expense = sorted(expense_by_account.values(), key=lambda row: row["account"].code)

    def grouped_accounts(account_rows):
        groups = {}
        for account_row in account_rows:
            account = account_row["account"]
            parent = account.parent or account
            group = groups.setdefault(
                parent.id,
                {
                    "account": parent,
                    "label": "Gross Sales" if parent.code == "4100" else parent.name,
                    "rows": [],
                    "subtotal": Decimal("0"),
                },
            )
            group["rows"].append(account_row)
            group["subtotal"] += account_row["amount"]
        return sorted(groups.values(), key=lambda group: group["account"].code)

    revenue_groups = grouped_accounts(revenue)
    expense_groups = grouped_accounts(expense)
    revenue_total = sum((row["amount"] for row in revenue), Decimal("0"))
    expense_total = sum((row["amount"] for row in expense), Decimal("0"))
    gross_sales_total = next(
        (group["subtotal"] for group in revenue_groups if group["account"].code == "4100"),
        Decimal("0"),
    )
    return {
        "revenue": revenue,
        "expense": expense,
        "revenue_groups": revenue_groups,
        "expense_groups": expense_groups,
        "amount_by_account": {
            row["account"].id: row["amount"] for row in revenue + expense
        },
        "gross_sales_total": gross_sales_total,
        "revenue_total": revenue_total,
        "expense_total": expense_total,
        "profit": revenue_total - expense_total,
        "sales_invoice_count": sales_invoice_count,
        "return_receipt_count": return_totals["count"],
        "unmapped_sales_count": unmapped_sales_count,
        "unmapped_sales_amount": unmapped_sales_amount,
        "missing_cogs_count": sales_totals["missing_cogs"] + return_totals["missing_cogs"],
    }


def _comparison_groups(reports, key):
    group_definitions = {}
    for report in reports:
        for group in report[key]:
            definition = group_definitions.setdefault(
                group["account"].id,
                {"account": group["account"], "label": group["label"], "accounts": {}},
            )
            for row in group["rows"]:
                definition["accounts"][row["account"].id] = row["account"]

    groups = []
    for definition in sorted(group_definitions.values(), key=lambda item: item["account"].code):
        rows = []
        for account in sorted(definition["accounts"].values(), key=lambda item: item.code):
            amounts = [report["amount_by_account"].get(account.id, Decimal("0")) for report in reports]
            rows.append({"account": account, "amounts": amounts, "total": sum(amounts, Decimal("0"))})
        subtotals = [sum((row["amounts"][index] for row in rows), Decimal("0")) for index in range(len(reports))]
        groups.append(
            {
                "account": definition["account"],
                "label": definition["label"],
                "rows": rows,
                "subtotals": subtotals,
                "total": sum(subtotals, Decimal("0")),
            }
        )
    return groups


@login_required
def profit_loss(request):
    today = date.today()
    mode = request.GET.get("mode", "standard")
    if mode not in {"standard", "multi_period", "multi_year"}:
        mode = "standard"

    start = _selected_date(request, "start", date(today.year, today.month, 1))
    end = _selected_date(request, "end", today)
    if start > end:
        start, end = end, start

    current_month = date(today.year, today.month, 1)
    start_month = _month_value(request.GET.get("start_month"), _shift_month(current_month, -2))
    end_month = _month_value(request.GET.get("end_month"), current_month)
    if start_month > end_month:
        start_month, end_month = end_month, start_month

    try:
        report_year = int(request.GET.get("year", today.year))
    except ValueError:
        report_year = today.year
    if report_year < 2000 or report_year > 2100:
        report_year = today.year

    accounts = list(Account.objects.select_related("parent").all())
    if mode == "multi_period":
        periods = []
        month = start_month
        while month <= end_month:
            periods.append(
                {
                    "label": month.strftime("%b %Y"),
                    "start": month,
                    "end": date(month.year, month.month, monthrange(month.year, month.month)[1]),
                }
            )
            month = _shift_month(month, 1)
    elif mode == "multi_year":
        periods = [
            {
                "label": str(year),
                "start": date(year, 1, 1),
                "end": date(year, 12, 31),
            }
            for year in range(report_year - 2, report_year + 1)
        ]
    else:
        periods = [{"label": f"{start:%d %b %Y} – {end:%d %b %Y}", "start": start, "end": end}]

    reports = [_profit_loss_data(period["start"], period["end"], accounts) for period in periods]
    report = reports[0] if mode == "standard" else None
    comparison = None
    if mode != "standard":
        comparison = {
            "periods": periods,
            "revenue_groups": _comparison_groups(reports, "revenue_groups"),
            "expense_groups": _comparison_groups(reports, "expense_groups"),
            "revenue_totals": [item["revenue_total"] for item in reports],
            "expense_totals": [item["expense_total"] for item in reports],
            "profits": [item["profit"] for item in reports],
            "revenue_total": sum((item["revenue_total"] for item in reports), Decimal("0")),
            "expense_total": sum((item["expense_total"] for item in reports), Decimal("0")),
            "profit": sum((item["profit"] for item in reports), Decimal("0")),
        }

    context = {
        "mode": mode,
        "start": start,
        "end": end,
        "start_month": start_month,
        "end_month": end_month,
        "report_year": report_year,
        "comparison": comparison,
        "unmapped_sales_count": sum((item["unmapped_sales_count"] for item in reports), 0),
        "missing_cogs_count": sum((item["missing_cogs_count"] for item in reports), 0),
    }
    if report:
        context.update(report)
    return render(
        request,
        "finance/profit_loss.html",
        context,
    )


def _money_rows(rows, account_types):
    return [
        row
        for row in rows
        if row["account"].is_postable and row["account"].account_type in account_types and row["net"]
    ]


@login_required
def sales_settings(request):
    if not can_access_tab(request.user, "finance", "sales_settings"):
        return HttpResponseForbidden("Akun ini tidak memiliki akses ke tab tersebut.")
    can_edit = request.user.is_superuser or module_level(request.user, "finance") in {"edit", "approve"}

    from master_data.models import Category, Product, ProductStatus, Subcategory

    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden("Akun ini hanya memiliki akses lihat.")
        action = request.POST.get("action", "single_update")
        account_id = request.POST.get("sales_account_id", "").strip()
        if action == "bulk_update":
            product_ids = list(dict.fromkeys(request.POST.getlist("product_ids")))
            if not product_ids:
                messages.error(request, "Pilih minimal satu Product untuk diubah massal.")
                return redirect(request.get_full_path())
            account = get_object_or_404(
                Account,
                pk=account_id,
                account_type="REVE",
                is_active=True,
                is_postable=True,
                parent__code="4100",
            )
            selected_products = list(Product.objects.filter(pk__in=product_ids).order_by("name", "code"))
            if len(selected_products) != len(product_ids):
                messages.error(request, "Sebagian Product yang dipilih tidak ditemukan.")
                return redirect(request.get_full_path())
            with transaction.atomic():
                current_settings = {
                    str(setting.product_id): setting
                    for setting in ProductSalesAccount.objects.select_related("sales_account").filter(
                        product_id__in=product_ids
                    )
                }
                for product in selected_products:
                    setting = current_settings.get(str(product.id))
                    before_values = {"sales_account": setting.sales_account.code if setting else ""}
                    setting = setting or ProductSalesAccount(product=product)
                    setting.sales_account = account
                    setting.full_clean()
                    setting.save()
                    record_audit(
                        actor=request.user,
                        action="finance_sales_account_mapping_updated",
                        entity_type="master_data.product",
                        entity_id=product.id,
                        before_values=before_values,
                        after_values={"sales_account": account.code},
                    )
            messages.success(
                request,
                f"Sales Account untuk {len(selected_products)} Product berhasil diubah massal.",
            )
            return redirect(request.get_full_path())

        product = get_object_or_404(Product, pk=request.POST.get("product_id"))
        with transaction.atomic():
            current = ProductSalesAccount.objects.select_related("sales_account").filter(product=product).first()
            before_values = {
                "sales_account": current.sales_account.code if current else "",
            }
            if account_id:
                account = get_object_or_404(
                    Account,
                    pk=account_id,
                    account_type="REVE",
                    is_active=True,
                    is_postable=True,
                    parent__code="4100",
                )
                setting = current or ProductSalesAccount(product=product)
                setting.sales_account = account
                setting.full_clean()
                setting.save()
                after_values = {"sales_account": account.code}
            else:
                if current:
                    current.delete()
                after_values = {"sales_account": ""}
            record_audit(
                actor=request.user,
                action="finance_sales_account_mapping_updated",
                entity_type="master_data.product",
                entity_id=product.id,
                before_values=before_values,
                after_values=after_values,
            )
        messages.success(request, f"Sales Account untuk {product.name} berhasil disimpan.")
        return redirect(request.get_full_path())

    products = Product.objects.select_related("status", "category", "subcategory")
    status_options = ProductStatus.objects.filter(products__isnull=False).distinct().order_by("name")
    selected_status = request.GET.get("product_status", "")
    if selected_status and not status_options.filter(pk=selected_status).exists():
        selected_status = ""

    category_options = Category.objects.filter(products__isnull=False)
    if selected_status:
        category_options = category_options.filter(products__status_id=selected_status)
    category_options = category_options.distinct().order_by("name")
    selected_category = request.GET.get("category", "")
    if selected_category and not category_options.filter(pk=selected_category).exists():
        selected_category = ""

    subcategory_options = Subcategory.objects.filter(products__isnull=False)
    if selected_status:
        subcategory_options = subcategory_options.filter(products__status_id=selected_status)
    if selected_category:
        subcategory_options = subcategory_options.filter(category_id=selected_category)
    subcategory_options = subcategory_options.distinct().order_by("name")
    selected_subcategory = request.GET.get("subcategory", "")
    if selected_subcategory and not subcategory_options.filter(pk=selected_subcategory).exists():
        selected_subcategory = ""

    if selected_status:
        products = products.filter(status_id=selected_status)
    if selected_category:
        products = products.filter(category_id=selected_category)
    if selected_subcategory:
        products = products.filter(subcategory_id=selected_subcategory)
    query = request.GET.get("q", "").strip()
    if query:
        products = products.filter(
            Q(code__icontains=query)
            | Q(parent_sku__icontains=query)
            | Q(article__icontains=query)
            | Q(name__icontains=query)
        )

    filtered_count = products.count()
    mapped_count = ProductSalesAccount.objects.filter(product__in=products).count()
    products = products.select_related("finance_sales_setting__sales_account").order_by("name", "code")
    page = Paginator(products, 100).get_page(request.GET.get("page"))
    for product in page.object_list:
        setting = getattr(product, "finance_sales_setting", None)
        product.sales_account_id = setting.sales_account_id if setting else None

    pagination_query = request.GET.copy()
    pagination_query.pop("page", None)
    return render(
        request,
        "finance/sales_settings.html",
        {
            "page": page,
            "account_options": Account.objects.filter(
                account_type="REVE", is_active=True, is_postable=True, parent__code="4100"
            ).select_related("parent").order_by("code"),
            "status_options": status_options,
            "category_options": category_options,
            "subcategory_options": subcategory_options,
            "selected_status": selected_status,
            "selected_category": selected_category,
            "selected_subcategory": selected_subcategory,
            "query": query,
            "filtered_count": filtered_count,
            "mapped_count": mapped_count,
            "total_products": Product.objects.count(),
            "can_edit": can_edit,
            "current_query": request.GET.urlencode(),
            "pagination_prefix": f"{pagination_query.urlencode()}&" if pagination_query else "",
        },
    )


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

    if slug in {"sales-invoice", "sales-invoice-list"}:
        from sales.models import SalesOrder, SalesOrderLine
        from sales.views import (
            _apply_source_filters,
            _pareto_period_bounds,
            _pareto_period_options,
            _source_options,
        )

        all_lines = SalesOrderLine.objects.filter(is_counted=True)
        latest = all_lines.order_by("-order__order_date").values_list("order__order_date", flat=True).first() or today
        earliest = all_lines.order_by("order__order_date").values_list("order__order_date", flat=True).first() or latest
        period_options = _pareto_period_options(earliest, latest)
        period_type = request.GET.get("period_type", "custom")
        if period_type not in {*period_options, "custom"}:
            period_type = "custom"
        if period_type == "custom":
            period_value = ""
            start = _selected_date(request, "date_from", latest.replace(day=1))
            end = _selected_date(request, "date_to", latest)
            if start > end:
                start, end = end, start
        else:
            valid_periods = {item["value"] for item in period_options[period_type]}
            period_value = request.GET.get("period", "")
            if period_value not in valid_periods:
                period_value = period_options[period_type][-1]["value"]
            start, end = _pareto_period_bounds(period_type, period_value)

        source_groups = [
            item for item in request.GET.getlist("source_group")
            if item in {"Marketplace", "Other"}
        ]
        source_options = _source_options(all_lines, source_groups)
        allowed_sources = {item["value"] for item in source_options}
        sources = [
            item for item in request.GET.getlist("source")
            if item and item in allowed_sources
        ]
        lines = _apply_source_filters(all_lines, sources, source_groups).filter(
            order__order_date__range=(start, end)
        )
        totals = lines.aggregate(
            orders=Count("order_id", distinct=True),
            gross=Sum("total_gross_sales"),
            net=Sum("total_net_sales"),
            cogs=Sum("total_cogs"),
        )
        orders = SalesOrder.objects.filter(id__in=lines.values("order_id")).annotate(
            gross=Sum("lines__total_gross_sales", filter=Q(lines__is_counted=True)),
            net=Sum("lines__total_net_sales", filter=Q(lines__is_counted=True)),
            cogs=Sum("lines__total_cogs", filter=Q(lines__is_counted=True)),
        ).order_by("-order_datetime", "source", "order_number")
        page = Paginator(orders, 100).get_page(request.GET.get("page"))
        pagination_query = request.GET.copy()
        pagination_query.pop("page", None)
        context.update(
            sales_invoice=True,
            sales_orders=page.object_list,
            page=page,
            pagination_prefix=f"{pagination_query.urlencode()}&" if pagination_query else "",
            date_from=start,
            date_to=end,
            period_type=period_type,
            period_value=period_value,
            period_options=period_options,
            source_groups=("Marketplace", "Other"),
            source_options=source_options,
            selected_sources=sources,
            selected_source_groups=source_groups,
            sales_totals={key: value or 0 for key, value in totals.items()},
            data_note="Menggunakan transaksi dan COGS snapshot canonical dari modul Sales. Posting jurnal Finance akan diaktifkan terpisah sesuai cutover 31 Agustus 2026.",
        )
    elif slug in {"sales-return", "sales-return-per-item"}:
        from inventory.models import PhysicalReturnReceipt

        query = request.GET.get("q", "").strip()
        receipt_status = request.GET.get("receipt_status", "")
        receipt_statuses = dict(PhysicalReturnReceipt.Condition.choices)
        if receipt_status not in receipt_statuses:
            receipt_status = ""
        received_returns = PhysicalReturnReceipt.objects.select_related(
            "sales_line__order",
            "warehouse",
            "recorded_by",
        ).order_by("-received_date", "-created_at")
        if receipt_status:
            received_returns = received_returns.filter(condition=receipt_status)
        if query:
            received_returns = received_returns.filter(
                Q(sales_line__order__order_number__icontains=query)
                | Q(sales_line__sku_code_snapshot__icontains=query)
                | Q(sales_line__product_name_snapshot__icontains=query)
            )
        totals = received_returns.aggregate(receipts=Count("id"), quantity=Sum("quantity"))
        context.update(
            sales_return=True,
            query=query,
            receipt_status=receipt_status,
            receipt_status_options=PhysicalReturnReceipt.Condition.choices,
            columns=(
                "Tanggal Receive",
                "Source",
                "No. Pesanan",
                "SKU",
                "Product",
                "Qty Receive",
                "Status Receive",
                "Warehouse",
                "Received By",
            ),
            rows=[
                (
                    receipt.received_date,
                    receipt.sales_line.order.display_source,
                    receipt.sales_line.order.order_number,
                    receipt.sales_line.sku_code_snapshot,
                    receipt.sales_line.product_name_snapshot,
                    int(receipt.quantity),
                    receipt.get_condition_display(),
                    receipt.warehouse.name,
                    receipt.recorded_by.get_full_name() or receipt.recorded_by.username,
                )
                for receipt in received_returns[:300]
            ],
            metrics=(
                ("Return received", totals["receipts"] or 0),
                ("Qty received", totals["quantity"] or 0),
            ),
            data_note="Hanya Sales Return yang sudah diterima tim Warehouse. Status Receive mengikuti kondisi yang dicatat di Return Log.",
        )
    elif spec["workflow"]:
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
