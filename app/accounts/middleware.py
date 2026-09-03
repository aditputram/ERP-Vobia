from django.http import HttpResponseForbidden


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
                # Modul lama mempertahankan akses sebelumnya; modul baru harus diberikan eksplisit.
                default_level = "none" if module == "rnd" else "approve"
                level = (user.module_access or {}).get(module, default_level)
                if level == "none":
                    return HttpResponseForbidden("Akun ini tidak memiliki akses ke modul tersebut.")
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if level == "view":
                        return HttpResponseForbidden("Akun ini hanya memiliki akses lihat.")
                    url_name = getattr(request.resolver_match, "url_name", "") or ""
                    if level == "edit" and any(word in url_name for word in APPROVAL_WORDS):
                        return HttpResponseForbidden("Tindakan ini memerlukan akses Approve.")
        return None
