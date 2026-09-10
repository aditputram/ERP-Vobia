from django.conf import settings
from django.urls import reverse

from finance.catalog import FINANCE_NAV_SECTIONS, ROUTES as FINANCE_ROUTES

from .access import MODULE_TABS, can_access_tab


def tab_permissions(request):
    if not getattr(request.user, "is_authenticated", False):
        return {}
    permissions = {
        module: {
            tab_key: can_access_tab(request.user, module, tab_key)
            for tab_key, _label, _section, _views in tabs
        }
        for module, tabs in MODULE_TABS.items()
    }
    namespace = getattr(getattr(request, "resolver_match", None), "namespace", "")
    if namespace == "rnd":
        current_module = "rnd"
    elif namespace == "finance":
        current_module = "finance"
    elif request.path.startswith("/marketing/"):
        current_module = "marketing"
    elif namespace in {"merchandising", "purchasing", "production", "inventory"}:
        current_module = "operation"
    elif namespace in {"sales", "traffic"} or request.path.startswith("/imports/sales/"):
        current_module = "sales"
    else:
        current_module = request.session.get("active_module", "sales")
    current_url_name = getattr(getattr(request, "resolver_match", None), "url_name", "")
    current_slug = (getattr(getattr(request, "resolver_match", None), "kwargs", {}) or {}).get("slug")
    finance_nav_sections = []
    for section in FINANCE_NAV_SECTIONS:
        items = []
        for tab_key, label, slug in section["items"]:
            if not permissions["finance"].get(tab_key):
                continue
            route = FINANCE_ROUTES.get(tab_key, "finance:feature")
            if tab_key == "journals":
                active = "journal" in current_url_name
            else:
                active = current_slug == slug if slug else current_url_name == route.split(":")[-1]
            items.append(
                {
                    "label": label,
                    "href": reverse(route, args=[slug] if slug else None),
                    "active": active,
                }
            )
        if items:
            finance_nav_sections.append(
                {**section, "items": items, "active": any(item["active"] for item in items)}
            )
    return {
        "tab_permissions": permissions,
        "current_business_module": current_module,
        "show_system_nav": any(
            permissions[module][tab_key]
            for module, tab_key in (
                ("master_data", "master_data"),
                ("reconciliation", "reconciliation"),
                ("guide", "guide"),
            )
        ),
        "finance_nav_sections": finance_nav_sections,
        "finance_uat_mode": settings.FINANCE_UAT_MODE,
    }
