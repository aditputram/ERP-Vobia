from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from audit.services import record_audit


MODULES = (
    {
        "slug": "sales",
        "name": "Sales",
        "eyebrow": "REVENUE & CUSTOMER",
        "description": "Dashboard penjualan, performa produk, Pareto, transaksi, dan data import.",
        "status": "Aktif",
        "available": True,
        "accent": "lime",
        "image": "img/modules/module-sales.jpg",
        "image_position": "center 58%",
    },
    {
        "slug": "operation",
        "name": "Operation",
        "eyebrow": "SUPPLY CHAIN",
        "description": "Merchandising, PPIC, production, warehouse, inventory, dan purchasing.",
        "status": "Fondasi aktif",
        "available": True,
        "accent": "teal",
        "image": "img/modules/module-operation.jpg",
        "image_position": "center 60%",
    },
    {
        "slug": "rnd",
        "name": "RnD",
        "eyebrow": "PRODUCT DEVELOPMENT",
        "description": "Riset produk, sampling, material, costing awal, dan lifecycle development.",
        "status": "Segera hadir",
        "available": False,
        "accent": "violet",
        "image": "img/modules/module-rnd.jpg",
        "image_position": "center 52%",
    },
    {
        "slug": "marketing",
        "name": "Marketing",
        "eyebrow": "BRAND & CAMPAIGN",
        "description": "Campaign, content plan, performance marketing, dan kalender peluncuran.",
        "status": "Instagram Report",
        "available": True,
        "accent": "coral",
        "image": "img/modules/module-marketing.jpg",
        "image_position": "center 52%",
    },
    {
        "slug": "finance",
        "name": "Finance",
        "eyebrow": "FINANCIAL CONTROL",
        "description": "Cash flow, payable, receivable, budgeting, dan financial reporting.",
        "status": "Segera hadir",
        "available": False,
        "accent": "blue",
        "image": "img/modules/module-finance.jpg",
        "image_position": "center 54%",
    },
    {
        "slug": "human-resource",
        "name": "Human Resource",
        "eyebrow": "PEOPLE & ORGANIZATION",
        "description": "Employee data, attendance, payroll, performance, dan organization.",
        "status": "Segera hadir",
        "available": False,
        "accent": "amber",
        "image": "img/modules/module-human-resource.jpg",
        "image_position": "center 54%",
    },
)


@login_required
def index(request):
    return render(request, "dashboard/index.html", {"modules": MODULES})


@login_required
def enter_module(request, module_slug):
    module = next((item for item in MODULES if item["slug"] == module_slug), None)
    if module is None:
        messages.error(request, "Modul tidak ditemukan.")
        return redirect("dashboard:index")
    if not module["available"]:
        messages.info(
            request,
            f"Modul {module['name']} sudah masuk roadmap dan akan diaktifkan setelah proses bisnisnya siap.",
        )
        return redirect("dashboard:index")

    request.session["active_module"] = module_slug
    record_audit(
        actor=request.user,
        action="module_entered",
        entity_type="navigation.module",
        entity_id=module_slug,
        metadata={"module_name": module["name"]},
    )
    if module_slug == "sales":
        return redirect("sales:dashboard")
    if module_slug == "marketing":
        return redirect("dashboard:instagram_dashboard")
    return redirect("merchandising:overview")


@login_required
def guide(request):
    return render(request, "dashboard/guide.html")
