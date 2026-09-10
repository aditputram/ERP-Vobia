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
    }
