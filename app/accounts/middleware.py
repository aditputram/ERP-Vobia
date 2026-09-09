from django.http import HttpResponseForbidden

from .access import VIEW_TABS, can_access_tab, module_level


MODULE_PATHS = (
    ("/sales/", "sales"),
    ("/traffic/", "sales"),
    ("/imports/sales/", "sales"),
    ("/merchandising/", "operation"),
    ("/purchasing/", "operation"),
    ("/production/", "operation"),
    ("/inventory/", "operation"),
    ("/rnd/", "rnd"),
    ("/marketing/", "marketing"),
    ("/master-data/", "master_data"),
    ("/imports/master/", "master_data"),
    ("/reconciliation/", "reconciliation"),
    ("/guide/", "guide"),
)
APPROVAL_WORDS = ("approve", "approval", "release", "commit", "finalize")


class ModuleAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        if user.is_authenticated and not user.is_superuser:
            module = next((key for prefix, key in MODULE_PATHS if request.path.startswith(prefix)), None)
            if module:
                level = module_level(user, module)
                if level == "none":
                    return HttpResponseForbidden("Akun ini tidak memiliki akses ke modul tersebut.")
                view_name = getattr(request.resolver_match, "view_name", "") or ""
                route_permission = VIEW_TABS.get(view_name)
                if route_permission:
                    route_module, tab_keys = route_permission
                    if route_module != module or not any(
                        can_access_tab(user, module, tab_key) for tab_key in tab_keys
                    ):
                        return HttpResponseForbidden("Akun ini tidak memiliki akses ke tab tersebut.")
                elif module in (getattr(user, "tab_access", {}) or {}):
                    return HttpResponseForbidden("Akun ini tidak memiliki akses ke tab tersebut.")
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if level == "view":
                        return HttpResponseForbidden("Akun ini hanya memiliki akses lihat.")
                    url_name = getattr(request.resolver_match, "url_name", "") or ""
                    if level == "edit" and any(word in url_name for word in APPROVAL_WORDS):
                        return HttpResponseForbidden("Tindakan ini memerlukan akses Approve.")
        return None
