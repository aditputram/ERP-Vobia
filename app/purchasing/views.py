from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponseNotAllowed
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from openpyxl import Workbook

from config.excel_exports import append_table, workbook_response
from inventory.services.aging import po_aging_snapshot
from imports.services.storage import DuplicateRawFile
from master_data.models import Product, Supplier

from .forms import (
    LegacyWIPSupplierRevisionForm,
    POWIPImportUploadForm,
    PPICProductionPlanForm,
    SupplierForm,
)
from .models import (
    PPICProductionPlan,
    PPICRequirement,
    POWIPImportBatch,
    POWIPImportIssue,
    PurchaseOrder,
)
from .services.wip_import import approve_po_wip_import, create_po_wip_import
from .services.workflows import (
    cancel_po,
    delete_unused_supplier,
    delete_draft_po,
    generate_po_from_production_plan,
    production_plan_po_groups,
    release_po,
    revise_legacy_wip_supplier,
    save_ppic_production_plan,
)


def _requirements_with_ui_state(selected_requirement_ids=None, selected_quantities=None, need_month=None):
    selected_requirement_ids = selected_requirement_ids or set()
    selected_quantities = selected_quantities or {}
    queryset = (
        PPICRequirement.objects.select_related(
            "sku",
            "sku__product_variant__product",
            "sku__product_variant__product__status",
            "sku__product_variant__product__category",
        )
        .filter(approved_qty__gt=0)
        .prefetch_related("po_lines__po", "production_plan_lines__plan")
        .order_by("need_month", "sku__sku")
    )
    if need_month:
        queryset = queryset.filter(need_month=need_month)
    requirements = list(
        queryset
    )
    for row in requirements:
        row.planned_qty = sum(
            (
                line.quantity
                for line in row.production_plan_lines.all()
                if line.plan.purchase_order_id is None
            ),
            Decimal("0"),
        )
        row.available_for_plan_qty = max(row.remaining_qty - row.planned_qty, Decimal("0"))
        row.ui_selected = str(row.id) in selected_requirement_ids
        row.ui_quantity = selected_quantities.get(str(row.id), row.available_for_plan_qty)
        if row.remaining_qty <= 0:
            row.ui_status = "Fully Allocated"
        elif row.available_for_plan_qty <= 0:
            row.ui_status = "In Production Plan"
        elif row.ordered_qty > 0:
            row.ui_status = "Partially Allocated"
        else:
            row.ui_status = "Open"
    return requirements


def _unique_options(rows, value_getter, label_getter):
    options = {}
    for row in rows:
        value = str(value_getter(row))
        options[value] = str(label_getter(row))
    return [{"value": value, "label": label} for value, label in sorted(options.items(), key=lambda item: item[1])]


def _tracking_rows(filters=None, *, limit=100):
    filters = filters or {}
    rows = []
    today = timezone.localdate()
    pos = PurchaseOrder.objects.select_related("supplier", "parent_plan").prefetch_related(
        "items__product", "lines__sku", "lines__qc_inspections", "lines__inbound_receipts"
    )
    query = filters.get("query", "")
    if query:
        pos = pos.filter(
            Q(po_number__icontains=query)
            | Q(parent_plan__ppo_number__icontains=query)
            | Q(lines__sku__sku__icontains=query)
        ).distinct()
    if filters.get("supplier"):
        pos = pos.filter(supplier_id=filters["supplier"])
    if filters.get("products"):
        pos = pos.filter(
            Q(lines__sku__product_variant__product_id__in=filters["products"])
            | Q(items__product_id__in=filters["products"])
        ).distinct()
    if filters.get("po_status") in PurchaseOrder.Status.values:
        pos = pos.filter(status=filters["po_status"])
    if filters.get("need_month"):
        try:
            year, month = (int(part) for part in filters["need_month"].split("-", 1))
        except (TypeError, ValueError):
            pass
        else:
            pos = pos.filter(need_month__year=year, need_month__month=month)
    if limit is not None:
        pos = pos[:limit]
    for po in pos:
        items = list(po.items.all())
        line_count = 0
        total_qty = Decimal("0")
        total_qc_passed = Decimal("0")
        has_production_qc = False
        total_received = Decimal("0")
        latest_receipt_date = None
        for line in po.lines.all():
            line_count += 1
            total_qty += line.ordered_qty
            inspections = list(line.qc_inspections.all())
            if inspections:
                has_production_qc = True
            total_qc_passed += sum((inspection.qty_passed for inspection in inspections), Decimal("0"))
            receipts = list(line.inbound_receipts.all())
            total_received += line.received_before_cutover_qty + sum(
                (receipt.received_qty for receipt in receipts),
                Decimal("0"),
            )
            for receipt in receipts:
                if latest_receipt_date is None or receipt.inbound_date > latest_receipt_date:
                    latest_receipt_date = receipt.inbound_date

        outstanding = max(total_qty - total_received, Decimal("0"))
        if po.status == PurchaseOrder.Status.CANCELLED:
            schedule_status = "Cancelled"
            progress_status = "Cancelled"
        elif total_qty > 0 and total_received >= total_qty:
            progress_status = "Complete"
            schedule_status = (
                "On Time"
                if not po.required_arrival
                or (latest_receipt_date and latest_receipt_date <= po.required_arrival)
                else "Late"
            )
        elif total_received > 0:
            progress_status = "Partially Received"
            schedule_status = "Risk Late" if po.required_arrival and today > po.required_arrival else "On Track"
        else:
            progress_status = po.get_status_display()
            schedule_status = "Risk Late" if po.required_arrival and today > po.required_arrival else "Open"
        rows.append(
            {
                "po": po,
                "item_count": len(items),
                "line_count": line_count,
                "total_qty": total_qty,
                "qc_passed": total_qc_passed if has_production_qc else None,
                "received": total_received,
                "outstanding": outstanding,
                "progress_status": progress_status,
                "schedule_status": schedule_status,
                "production_status": "—",
            }
        )
        is_production_control = not po.parent_plan_id or po.parent_plan.purchase_order_id == po.id
        if po.status == PurchaseOrder.Status.RELEASED and is_production_control:
            from production.services import ensure_production_order, production_snapshot

            production_order = ensure_production_order(po, actor=None)
            rows[-1]["production_status"] = production_snapshot(production_order)["current_label"]
        elif po.parent_plan_id:
            rows[-1]["production_status"] = "PO"
    if filters.get("schedule_status"):
        rows = [row for row in rows if row["schedule_status"] == filters["schedule_status"]]
    return rows


def _export_requirements(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "PPIC Requirement"
    append_table(
        sheet,
        (
            "Need Month", "SKU", "Parent SKU", "Product", "Variant", "Size",
            "Product Status", "Category", "Approved Qty", "Ordered Qty", "Remaining Qty",
            "Allocation Status",
        ),
        (
            (
                row.need_month,
                row.sku.sku,
                row.sku.product_variant.product.parent_sku,
                row.sku.product_variant.product.name,
                row.sku.product_variant.name,
                row.sku.size,
                row.sku.product_variant.product.status.name,
                row.sku.product_variant.product.category.name,
                row.approved_qty,
                row.ordered_qty,
                row.remaining_qty,
                row.ui_status,
            )
            for row in rows
        ),
        number_formats={9: "#,##0", 10: "#,##0", 11: "#,##0"},
    )
    return workbook_response(workbook, f"VOBIA-PPIC-Requirement-{timezone.localdate():%Y-%m-%d}.xlsx")


def _export_purchase_orders(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Purchase Order"
    append_table(
        sheet,
        (
            "No. PPO", "No. PO", "PO Type", "Vendor", "Need Month", "Required Arrival", "Items", "Jumlah SKU", "PO Qty",
            "Production", "Received", "Outstanding", "Schedule", "QC Passed", "Status",
        ),
        (
            (
                row["po"].ppo_number or "—",
                row["po"].po_number or "Draft",
                row["po"].get_order_type_display(),
                row["po"].supplier.name,
                row["po"].need_month,
                row["po"].required_arrival,
                row["item_count"],
                row["line_count"],
                row["total_qty"],
                row["production_status"],
                row["received"],
                row["outstanding"],
                row["schedule_status"],
                row["qc_passed"],
                row["progress_status"],
            )
            for row in rows
        ),
        number_formats={5: "#,##0", 6: "#,##0", 8: "#,##0", 9: "#,##0", 11: "#,##0"},
    )
    return workbook_response(workbook, f"VOBIA-Purchase-Order-{timezone.localdate():%Y-%m-%d}.xlsx")


@login_required
def requirements(request):
    base_rows = _requirements_with_ui_state()
    month_options = sorted({row.need_month for row in base_rows})
    selected_need_month = request.GET.get("need_month", "")
    valid_months = {month.strftime("%Y-%m"): month for month in month_options}
    selected_month = valid_months.get(selected_need_month)
    if selected_month is None:
        selected_need_month = ""
    after_month = [row for row in base_rows if not selected_month or row.need_month == selected_month]

    status_options = _unique_options(
        after_month,
        lambda row: row.sku.product_variant.product.status_id,
        lambda row: row.sku.product_variant.product.status.name,
    )
    selected_status = request.GET.get("status", "")
    if selected_status not in {option["value"] for option in status_options}:
        selected_status = ""
    after_status = [
        row
        for row in after_month
        if not selected_status or str(row.sku.product_variant.product.status_id) == selected_status
    ]

    category_options = _unique_options(
        after_status,
        lambda row: row.sku.product_variant.product.category_id,
        lambda row: row.sku.product_variant.product.category.name,
    )
    selected_category = request.GET.get("category", "")
    if selected_category not in {option["value"] for option in category_options}:
        selected_category = ""
    after_category = [
        row
        for row in after_status
        if not selected_category or str(row.sku.product_variant.product.category_id) == selected_category
    ]
    allocation_status_order = ("Open", "Partially Allocated", "In Production Plan", "Fully Allocated")
    available_allocation_statuses = {row.ui_status for row in after_category}
    allocation_status_options = [
        {"value": value, "label": value}
        for value in allocation_status_order
        if value in available_allocation_statuses
    ]
    valid_allocation_statuses = {option["value"] for option in allocation_status_options}
    selected_allocation_statuses = [
        value
        for value in request.GET.getlist("allocation_status")
        if value in valid_allocation_statuses
    ]
    rows = [
        row
        for row in after_category
        if not selected_allocation_statuses or row.ui_status in selected_allocation_statuses
    ]
    if request.GET.get("export") == "xlsx":
        return _export_requirements(rows)
    return render(
        request,
        "purchasing/requirements.html",
        {
            "requirements": rows,
            "total_approved": sum((row.approved_qty for row in after_category), Decimal("0")),
            "total_ordered": sum((row.ordered_qty for row in after_category), Decimal("0")),
            "total_planned": sum((row.planned_qty for row in after_category), Decimal("0")),
            "total_remaining": sum((row.available_for_plan_qty for row in after_category), Decimal("0")),
            "requirement_line_count": len(after_category),
            "need_month_options": month_options,
            "selected_need_month": selected_need_month,
            "selected_need_month_date": selected_month,
            "status_options": status_options,
            "selected_status": selected_status,
            "category_options": category_options,
            "selected_category": selected_category,
            "allocation_status_options": allocation_status_options,
            "selected_allocation_statuses": selected_allocation_statuses,
        },
    )


def _production_plan_materials(source, requirements):
    rows = []
    products = {
        str(row.sku.product_variant.product_id): row.sku.product_variant.product
        for row in requirements
    }
    product_ids = source.getlist("material_product")
    names = source.getlist("material_name")
    quantities = source.getlist("material_quantity")
    units = source.getlist("material_unit")
    prices = source.getlist("material_unit_price")
    material_types = source.getlist("material_type")
    supplier_ids = source.getlist("material_supplier")
    suppliers = {
        str(row.id): row
        for row in Supplier.objects.filter(id__in=[value for value in supplier_ids if value], is_active=True)
    }
    for index, name in enumerate(names):
        if not name.strip():
            continue
        product = products.get(product_ids[index] if index < len(product_ids) else "")
        if not product:
            raise ValidationError(f"Material {name}: pilih Product/Parent SKU tujuan.")
        supplier_id = supplier_ids[index] if index < len(supplier_ids) else ""
        supplier = suppliers.get(supplier_id) if supplier_id else None
        if supplier_id and not supplier:
            raise ValidationError(f"Material {name}: supplier tidak valid atau sudah nonaktif.")
        material_type = material_types[index] if index < len(material_types) else "MAIN"
        if material_type not in {"MAIN", "SUPPORTING"}:
            raise ValidationError(f"Material {name}: tipe material tidak valid.")
        try:
            quantity = Decimal(quantities[index])
            unit_price = Decimal(prices[index])
        except (ArithmeticError, IndexError, ValueError) as exc:
            raise ValidationError(f"Material {name}: quantity dan harga harus berupa angka.") from exc
        rows.append({
            "product": product,
            "supplier": supplier,
            "material_type": material_type,
            "material_name": name.strip(),
            "quantity": quantity,
            "unit": units[index].strip() if index < len(units) else "",
            "unit_price": unit_price,
        })
    return rows


def _production_plan_costs(source, requirements, *, additional_process_controlled):
    products = {
        str(row.sku.product_variant.product_id): row.sku.product_variant.product
        for row in requirements
    }
    costs_by_product = {}
    supplier_ids = source.getlist("cmt_supplier") + source.getlist("additional_process_supplier")
    suppliers = {
        str(row.id): row
        for row in Supplier.objects.filter(id__in=[value for value in supplier_ids if value], is_active=True)
    }

    def merge_rows(product_field, cost_field, supplier_field, target, supplier_target, label, name_field=None):
        seen_products = set()
        product_ids = source.getlist(product_field)
        costs = source.getlist(cost_field)
        row_supplier_ids = source.getlist(supplier_field)
        names = source.getlist(name_field) if name_field else []
        for index, product_id in enumerate(product_ids):
            if not product_id:
                continue
            product = products.get(product_id)
            if not product:
                raise ValidationError(f"Product biaya {label} harus termasuk dalam Production Plan.")
            if product.id in seen_products:
                raise ValidationError(f"Biaya {label} product {product.name} hanya boleh diisi satu kali.")
            try:
                value = Decimal((costs[index] or "0").replace(",", ""))
            except (ArithmeticError, IndexError, ValueError) as exc:
                raise ValidationError(f"Biaya {label} product {product.name} harus berupa angka.") from exc
            if value < 0:
                raise ValidationError(f"Biaya {label} product {product.name} tidak boleh negatif.")
            supplier_id = row_supplier_ids[index] if index < len(row_supplier_ids) else ""
            supplier = suppliers.get(supplier_id) if supplier_id else None
            if supplier_id and not supplier:
                raise ValidationError(f"Vendor {label} product {product.name} tidak valid atau sudah nonaktif.")
            row = costs_by_product.setdefault(product.id, {
                "product": product,
                "cmt_unit_cost": Decimal("0"),
                "cmt_supplier": None,
                "additional_process_name": "",
                "additional_process_unit_cost": Decimal("0"),
                "additional_process_supplier": None,
            })
            row[target] = value
            row[supplier_target] = supplier
            if name_field:
                row["additional_process_name"] = names[index].strip() if index < len(names) else ""
            seen_products.add(product.id)

    merge_rows(
        "cmt_cost_product",
        "cmt_unit_cost",
        "cmt_supplier",
        "cmt_unit_cost",
        "cmt_supplier",
        "CMT",
    )
    if additional_process_controlled:
        merge_rows(
            "additional_cost_product",
            "additional_process_unit_cost",
            "additional_process_supplier",
            "additional_process_unit_cost",
            "additional_process_supplier",
            "Additional Process",
            name_field="additional_process_cost_name",
        )
    return list(costs_by_product.values())


def _production_plan_context(
    *,
    form,
    requirements,
    plan=None,
    materials=None,
    process_costs=None,
    unit_cogs=None,
):
    requirements = list(requirements)
    for row in requirements:
        line = None
        if plan:
            line = next((item for item in plan.lines.all() if item.requirement_id == row.id), None)
            row.plan_qty = line.quantity if line else Decimal("0")
        else:
            row.plan_qty = row.available_for_plan_qty
        posted_cogs = unit_cogs.get(str(row.id)) if unit_cogs is not None else None
        if posted_cogs is not None:
            row.plan_unit_cogs = posted_cogs
        elif line:
            row.plan_unit_cogs = f"{line.unit_cogs:.0f}"
        else:
            row.plan_unit_cogs = ""
    if materials is None:
        materials = [
            {
                "product": row.product,
                "supplier": row.supplier,
                "material_type": row.material_type,
                "material_name": row.material_name,
                "quantity": row.quantity,
                "unit": row.unit,
                "unit_price": row.unit_price,
                "total_cost": row.total_cost,
            }
            for row in plan.materials.all()
        ] if plan else []
    else:
        materials = [
            {
                **row,
                "total_cost": row["quantity"] * row["unit_price"],
            }
            for row in materials
        ]
    if process_costs is None:
        process_costs = [
            {
                "product": row.product,
                "cmt_unit_cost": row.cmt_unit_cost,
                "cmt_supplier": row.cmt_supplier,
                "additional_process_name": row.additional_process_name,
                "additional_process_unit_cost": row.additional_process_unit_cost,
                "additional_process_supplier": row.additional_process_supplier,
            }
            for row in plan.process_costs.all()
        ] if plan else []
    products = list({row.sku.product_variant.product_id: row.sku.product_variant.product for row in requirements}.values())
    products.sort(key=lambda row: row.name)
    product_names = [row.name for row in products]
    po_preview_groups = []
    if plan:
        if plan.production_method == PPICProductionPlan.ProductionMethod.CMT:
            po_preview_groups = production_plan_po_groups(plan)
        else:
            po_preview_groups = [{
                "child_code": "",
                "order_type_label": PurchaseOrder.OrderType.FPP.label,
                "supplier": plan.supplier,
                "items": list(plan.lines.all()),
                "required_arrival": plan.target_inbound_date,
                "total_cost": plan.planned_total_cost,
            }]
    addable_products = []
    if requirements:
        selected_product_ids = {row.sku.product_variant.product_id for row in requirements}
        category_ids = {row.sku.product_variant.product.category_id for row in requirements}
        product_groups = {}
        for row in _requirements_with_ui_state(need_month=requirements[0].need_month):
            product = row.sku.product_variant.product
            if (
                row.available_for_plan_qty <= 0
                or product.id in selected_product_ids
                or product.category_id not in category_ids
            ):
                continue
            row.plan_qty = row.available_for_plan_qty
            row.plan_unit_cogs = ""
            group = product_groups.setdefault(product.id, {"product": product, "requirements": [], "total_qty": Decimal("0")})
            group["requirements"].append(row)
            group["total_qty"] += row.plan_qty
        addable_products = sorted(product_groups.values(), key=lambda group: group["product"].name)
    return {
        "plan": plan,
        "plan_form": form,
        "requirements": requirements,
        "materials": materials,
        "process_costs": process_costs,
        "material_products": products,
        "supplier_options": Supplier.objects.filter(is_active=True).order_by("name"),
        "product_label": (
            product_names[0] if len(product_names) == 1 else f"{len(product_names)} Products Selected"
        ),
        "addable_products": addable_products,
        "po_preview_groups": po_preview_groups,
        "draft_storage_key": "production-plan:{}:{}".format(
            plan.id if plan else "new",
            ",".join(sorted(str(row.id) for row in requirements)),
        ),
        "total_qty": sum((row.plan_qty for row in requirements), Decimal("0")),
        "plans": PPICProductionPlan.objects.select_related("supplier", "purchase_order").prefetch_related(
            "lines__requirement__sku__product_variant__product",
            "materials__product",
            "materials__supplier",
            "process_costs__product",
            "process_costs__cmt_supplier",
            "process_costs__additional_process_supplier",
        ),
    }


@login_required
def production_plans(request):
    plans = PPICProductionPlan.objects.select_related("supplier", "purchase_order").prefetch_related(
        "lines__requirement__sku__product_variant__product",
        "materials__product",
        "materials__supplier",
        "process_costs__product",
        "process_costs__cmt_supplier",
        "process_costs__additional_process_supplier",
    )
    return render(request, "purchasing/production_plans.html", {"plans": plans})


@login_required
def production_plan_create(request):
    if request.method == "POST" and request.POST.get("action") != "save":
        requirement_ids = request.POST.getlist("requirement")
        if not requirement_ids:
            messages.error(request, "Pilih minimal satu requirement untuk Review Production Plan.")
            return redirect("purchasing:requirements")
        request.session["production_plan_review_requirements"] = requirement_ids
        return redirect("purchasing:production_plan_create")
    requirement_ids = (
        request.POST.getlist("requirement")
        if request.method == "POST"
        else request.session.get("production_plan_review_requirements", [])
    )
    requirements = _requirements_with_ui_state(selected_requirement_ids=set(requirement_ids))
    requirements = [row for row in requirements if str(row.id) in requirement_ids]
    if not requirements:
        messages.error(request, "Pilih minimal satu requirement untuk Review Production Plan.")
        return redirect("purchasing:requirements")
    form = PPICProductionPlanForm(
        request.POST if request.POST.get("action") == "save" else None,
        initial={"production_method": PPICProductionPlan.ProductionMethod.CMT},
    )
    need_months = {row.need_month for row in requirements}
    if len(need_months) == 1:
        form.instance.need_month = next(iter(need_months))
    materials = []
    process_costs = []
    if request.POST.get("action") == "save":
        try:
            materials = _production_plan_materials(request.POST, requirements)
            process_costs = _production_plan_costs(
                request.POST,
                requirements,
                additional_process_controlled=request.POST.get("control_additional_process") == "on",
            )
        except ValidationError as exc:
            form.add_error(None, exc)
    if request.POST.get("action") == "save" and form.is_valid():
        try:
            plan = save_ppic_production_plan(
                actor=request.user,
                requirements=requirements,
                quantities={str(row.id): request.POST.get(f"quantity_{row.id}") for row in requirements},
                values={name: form.cleaned_data[name] for name in form.Meta.fields},
                materials=materials,
                process_costs=process_costs,
                unit_cogs={str(row.id): request.POST.get(f"unit_cogs_{row.id}") for row in requirements},
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            request.session.pop("production_plan_review_requirements", None)
            messages.success(request, "Production Plan Draft tersimpan. Review preview PO sebelum generate.")
            return redirect("purchasing:production_plan_detail", plan_id=plan.id)
    context = _production_plan_context(
        form=form,
        requirements=requirements,
        materials=materials,
        process_costs=process_costs,
        unit_cogs={str(row.id): request.POST.get(f"unit_cogs_{row.id}") for row in requirements}
        if request.POST.get("action") == "save" else None,
    )
    return render(request, "purchasing/production_plan_form.html", context)


@login_required
def production_plan_detail(request, plan_id):
    plan = get_object_or_404(
        PPICProductionPlan.objects.select_related("supplier", "purchase_order").prefetch_related(
            "lines__requirement__sku__product_variant__product",
            "materials__product",
            "materials__supplier",
            "process_costs__product",
            "process_costs__cmt_supplier",
            "process_costs__additional_process_supplier",
        ),
        pk=plan_id,
    )
    all_requirements = _requirements_with_ui_state()
    requirement_ids = {line.requirement_id for line in plan.lines.all()}
    requirements = [row for row in all_requirements if row.id in requirement_ids]
    form = PPICProductionPlanForm(instance=plan)
    materials = None
    process_costs = None
    unit_cogs = None
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if plan.status == PPICProductionPlan.Status.DRAFT:
            form = PPICProductionPlanForm(request.POST, instance=plan)
            submitted_ids = set(request.POST.getlist("requirement"))
            category_ids = {row.sku.product_variant.product.category_id for row in requirements}
            allowed_requirements = [
                row for row in all_requirements
                if row.need_month == plan.need_month
                and row.sku.product_variant.product.category_id in category_ids
                and (row.id in requirement_ids or row.available_for_plan_qty > 0)
            ]
            submitted_requirements = [row for row in allowed_requirements if str(row.id) in submitted_ids]
            if submitted_requirements:
                requirements = submitted_requirements
            else:
                form.add_error(None, "Production Plan wajib memiliki minimal satu Product Requirement.")
            if len(submitted_requirements) != len(submitted_ids):
                form.add_error(None, "Product tambahan harus memiliki Need Month dan Category yang sama serta masih Open.")
            materials = []
            process_costs = []
            unit_cogs = {str(row.id): request.POST.get(f"unit_cogs_{row.id}") for row in requirements}
            try:
                materials = _production_plan_materials(request.POST, requirements)
                process_costs = _production_plan_costs(
                    request.POST,
                    requirements,
                    additional_process_controlled=request.POST.get("control_additional_process") == "on",
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            if submitted_requirements and form.is_valid():
                try:
                    plan = save_ppic_production_plan(
                        actor=request.user,
                        plan=plan,
                        requirements=requirements,
                        quantities={str(row.id): request.POST.get(f"quantity_{row.id}") for row in requirements},
                        values={name: form.cleaned_data[name] for name in form.Meta.fields},
                        materials=materials,
                        process_costs=process_costs,
                        unit_cogs=unit_cogs,
                    )
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    if action == "generate":
                        try:
                            po = generate_po_from_production_plan(plan.id, request.user)
                        except ValidationError as exc:
                            for message in exc.messages:
                                messages.error(request, message)
                        else:
                            messages.success(request, "Draft PO berhasil dibuat dari Production Plan.")
                            return redirect("purchasing:po_detail", po_id=po.id)
                    else:
                        messages.success(request, "Production Plan Draft diperbarui.")
                        return redirect("purchasing:production_plan_detail", plan_id=plan.id)
    return render(
        request,
        "purchasing/production_plan_form.html",
        _production_plan_context(
            form=form,
            requirements=requirements,
            plan=plan,
            materials=materials,
            process_costs=process_costs,
            unit_cogs=unit_cogs,
        ),
    )


@login_required
def overview(request):
    plans = list(
        PPICProductionPlan.objects.filter(
            status=PPICProductionPlan.Status.DRAFT,
            purchase_order__isnull=True,
        )
        .select_related("supplier")
        .prefetch_related("lines__requirement__sku__product_variant__product", "materials__product")
        .order_by("need_month", "created_at")
    )
    plan_months = sorted({plan.need_month for plan in plans})
    selected_month = request.GET.get("month", "")
    valid_months = {month.strftime("%Y-%m") for month in plan_months}
    if selected_month not in valid_months:
        selected_month = ""
    if selected_month:
        plans = [plan for plan in plans if plan.need_month.strftime("%Y-%m") == selected_month]
    pos = list(PurchaseOrder.objects.select_related("supplier", "created_by").prefetch_related("lines")[:100])
    for po in pos:
        po.total_qty = sum((line.ordered_qty for line in po.lines.all()), Decimal("0"))
        po.aging_snapshot = po_aging_snapshot(po) if po.status == PurchaseOrder.Status.RELEASED else None
    return render(
        request,
        "purchasing/overview.html",
        {
            "pos": pos,
            "plans": plans,
            "plan_months": plan_months,
            "selected_month": selected_month,
            "plan_qty": sum((plan.total_qty for plan in plans), Decimal("0")),
            "plan_cost": sum((plan.planned_total_cost for plan in plans), Decimal("0")),
        },
    )


@login_required
def purchase_orders(request):
    selected = {
        "query": request.GET.get("q", "").strip(),
        "supplier": request.GET.get("supplier", ""),
        "products": [value for value in request.GET.getlist("product") if value],
        "need_month": request.GET.get("need_month", ""),
        "po_status": request.GET.get("po_status", ""),
        "schedule_status": request.GET.get("schedule_status", ""),
    }
    rows = _tracking_rows(selected, limit=None if request.GET.get("export") == "xlsx" else 100)
    if request.GET.get("export") == "xlsx":
        return _export_purchase_orders(rows)
    return render(
        request,
        "purchasing/tracking.html",
        {
            "rows": rows,
            "po_count": len(rows),
            "line_count": sum((row["line_count"] for row in rows), 0),
            "total_po_qty": sum((row["total_qty"] for row in rows), Decimal("0")),
            "total_outstanding": sum((row["outstanding"] for row in rows), Decimal("0")),
            "selected": selected,
            "supplier_options": Supplier.objects.filter(purchase_orders__isnull=False).distinct().order_by("name"),
            "product_options": Product.objects.filter(
                variants__skus__purchase_order_lines__isnull=False
            ).distinct().order_by("name"),
            "need_month_options": PurchaseOrder.objects.order_by("-need_month").values_list("need_month", flat=True).distinct(),
            "po_status_options": PurchaseOrder.Status.choices,
            "schedule_status_options": ("Open", "On Track", "Risk Late", "On Time", "Late", "Cancelled"),
        },
    )


@login_required
def tracking(request):
    return purchase_orders(request)


@login_required
def vendors(request):
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f"Vendor {supplier.name} berhasil ditambahkan.")
            return redirect("purchasing:vendors")
    else:
        form = SupplierForm()
    suppliers = Supplier.objects.annotate(po_count=Count("purchase_orders")).order_by("name")
    return render(request, "purchasing/vendors.html", {"form": form, "suppliers": suppliers})


@login_required
def vendor_delete(request, supplier_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    try:
        deleted = delete_unused_supplier(
            supplier.id,
            request.user,
            request.POST.get("reason", "").strip(),
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Vendor {deleted['name']} dihapus; tidak ada Purchase Order yang terdampak.")
    return redirect("purchasing:vendors")


def _po_items_for_display(po):
    material_products = {}
    material_type = {
        PurchaseOrder.OrderType.MAIN_MATERIAL: "MAIN",
        PurchaseOrder.OrderType.SUPPORTING_MATERIAL: "SUPPORTING",
    }.get(po.order_type)
    if po.parent_plan_id and material_type:
        for material in po.parent_plan.materials.select_related("product").filter(
            supplier_id=po.supplier_id,
            material_type=material_type,
        ):
            key = (
                material.material_name.strip().casefold(),
                material.unit.strip().casefold(),
                material.unit_price,
            )
            material_products.setdefault(key, set()).add(material.product.name)
    items = list(po.items.select_related("product"))
    for item in items:
        key = (item.description.strip().casefold(), item.unit.strip().casefold(), item.unit_price)
        item.product_label = item.product.name if item.product else ", ".join(sorted(material_products.get(key, ()))) or "—"
        item.line_total = item.total_cost
    return items


@login_required
def po_detail(request, po_id):
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "created_by", "released_by", "parent_plan").prefetch_related(
            "items__product", "lines__sku", "lines__requirement", "lines__qc_inspections", "lines__inbound_receipts"
        ),
        pk=po_id,
    )
    vendor_revision_form = LegacyWIPSupplierRevisionForm(initial={"supplier": po.supplier_id})
    po_lines = list(po.lines.all())
    for line in po_lines:
        line.total_cogs = Decimal(line.ordered_qty) * Decimal(line.cogs_snapshot or 0)
    po_items = _po_items_for_display(po)
    production_state = None
    activity_rows = []
    is_production_control = not po.parent_plan_id or po.parent_plan.purchase_order_id == po.id
    ppo_children = list(po.parent_plan.child_purchase_orders.select_related("supplier").order_by("child_code")) if po.parent_plan_id else []
    if po.status == PurchaseOrder.Status.RELEASED and is_production_control:
        from production.services import ensure_production_order, production_snapshot

        production_order = ensure_production_order(po, actor=request.user)
        production_state = production_snapshot(production_order)
        finalization = getattr(production_order, "cogs_finalization", None)
        final_rows = {
            row["po_line_id"]: row for row in finalization.line_snapshot
        } if finalization else {}
        for line in po_lines:
            row = final_rows.get(str(line.id))
            if row:
                line.final_sellable_qty = Decimal(row["sellable_qty"])
                line.final_unit_cogs = Decimal(row["final_unit_cogs"])
                line.unit_cogs_increase = Decimal(row["unit_cogs_increase"])
                line.total_cogs = line.final_sellable_qty * line.final_unit_cogs
        ordered_qty = production_state["ordered_qty"]
        activity_rows = [
            {"label": "Cutting", "qty": production_state["cmt_quantities"]["CUT"]["completed_qty"]},
            {"label": "Make · Jahit", "qty": production_state["cmt_quantities"]["MAKE"]["completed_qty"]},
            {"label": "Trim · Finishing", "qty": production_state["cmt_quantities"]["TRIM"]["completed_qty"]},
            {"label": "QC Passed", "qty": production_state["passed_qty"]},
            {"label": "Inbound", "qty": production_state["received_qty"]},
        ]
        for row in activity_rows:
            row["po_qty"] = ordered_qty
            row["gap"] = row["qty"] - ordered_qty
    return render(
        request,
        "purchasing/detail.html",
        {
            "po": po,
            "aging": po_aging_snapshot(po),
            "vendor_revision_form": vendor_revision_form,
            "production_state": production_state,
            "po_lines": po_lines,
            "po_items": po_items,
            "is_production_control": is_production_control,
            "ppo_children": ppo_children,
            "activity_rows": activity_rows,
        },
    )


@login_required
def po_release(request, po_id):
    if request.method == "POST":
        try:
            po = release_po(po_id, request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{po.po_number} berhasil dirilis; COGS sudah dibekukan.")
    return redirect("purchasing:po_detail", po_id=po_id)


@login_required
def po_cancel(request, po_id):
    if request.method == "POST":
        try:
            po = cancel_po(po_id, request.user, request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{po.po_number} dibatalkan dengan audit reason.")
    return redirect("purchasing:po_detail", po_id=po_id)


@login_required
def po_revise_vendor(request, po_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    form = LegacyWIPSupplierRevisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revisi vendor gagal: periksa vendor dan alasan revisi.")
        return redirect("purchasing:po_detail", po_id=po_id)
    try:
        po = revise_legacy_wip_supplier(
            po_id,
            form.cleaned_data["supplier"],
            request.user,
            form.cleaned_data["reason"],
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"Vendor {po.po_number} direvisi menjadi {po.supplier.name}; qty dan COGS snapshot tidak berubah.",
        )
    return redirect("purchasing:po_detail", po_id=po_id)


@login_required
def po_delete_draft(request, po_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        delete_draft_po(po_id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("purchasing:po_detail", po_id=po_id)
    messages.success(request, "Draft PO dihapus; Need Key terkait kembali eligible di PO Generator.")
    return redirect("purchasing:purchase_orders")


@login_required
def po_print(request, po_id):
    po = get_object_or_404(
        PurchaseOrder.objects.select_related(
            "supplier",
            "created_by",
            "released_by",
            "parent_plan",
        ).prefetch_related("items__product", "lines__sku__product_variant__product"),
        pk=po_id,
    )
    print_lines = []
    print_items = []
    subtotal = Decimal("0")
    total_qty = Decimal("0")
    for line in po.lines.all():
        line_total = (
            line.ordered_qty * line.cogs_snapshot
            if line.cogs_snapshot is not None
            else None
        )
        print_lines.append({"line": line, "line_total": line_total})
        total_qty += line.ordered_qty
        if line_total is not None:
            subtotal += line_total
    for item in _po_items_for_display(po):
        print_items.append({"item": item, "line_total": item.total_cost})
        subtotal += item.total_cost
        total_qty += item.quantity
    if print_items:
        print_lines = []
        total_qty = sum((row["item"].quantity for row in print_items), Decimal("0"))
        subtotal = sum((row["line_total"] for row in print_items), Decimal("0"))
    return render(
        request,
        "purchasing/print.html",
        {
            "po": po,
            "print_lines": print_lines,
            "print_items": print_items,
            "subtotal": subtotal,
            "total_qty": total_qty,
            "po_date": po.released_at or po.created_at,
        },
    )


@login_required
def po_wip_import_list(request):
    batches = POWIPImportBatch.objects.select_related("raw_file", "approved_by")[:20]
    return render(request, "purchasing/wip/list.html", {"batches": batches})


@login_required
def po_wip_import_upload(request):
    if request.method == "POST":
        form = POWIPImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_po_wip_import(form.cleaned_data["file"], request.user)
            except DuplicateRawFile as exc:
                existing = exc.raw_file.po_wip_batches.order_by("-created_at").first()
                form.add_error(
                    "file",
                    f"File identik sudah pernah diunggah. Batch sebelumnya: {existing.id if existing else '—'}.",
                )
            else:
                if batch.status == POWIPImportBatch.Status.READY:
                    messages.success(request, "PO WIP berhasil diparsing dan siap direview.")
                else:
                    messages.warning(request, "PO WIP diparsing tetapi memiliki blocking issue.")
                return redirect("purchasing:po_wip_detail", batch_id=batch.id)
    elif request.method == "GET":
        form = POWIPImportUploadForm()
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(request, "purchasing/wip/upload.html", {"form": form})


@login_required
def po_wip_import_detail(request, batch_id):
    batch = get_object_or_404(
        POWIPImportBatch.objects.select_related("raw_file", "approved_by"),
        pk=batch_id,
    )
    page = Paginator(
        batch.staged_rows.select_related("sku__product_variant__product"),
        50,
    ).get_page(request.GET.get("page"))
    issues = batch.issues.select_related("staged_row")
    severity = request.GET.get("severity", "")
    if severity in POWIPImportIssue.Severity.values:
        issues = issues.filter(severity=severity)
    return render(
        request,
        "purchasing/wip/detail.html",
        {"batch": batch, "page": page, "issues": issues[:100]},
    )


@login_required
def po_wip_import_approve(request, batch_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    batch = get_object_or_404(POWIPImportBatch, pk=batch_id)
    try:
        _, counts = approve_po_wip_import(batch.id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"PO WIP committed: {counts['purchase_orders']} PO, {counts['lines']} line, "
            f"{counts['outstanding_qty']} pcs. Belum ada stock movement sampai physical Inbound.",
        )
    return redirect("purchasing:po_wip_detail", batch_id=batch.id)
