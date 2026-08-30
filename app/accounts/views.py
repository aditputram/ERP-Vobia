from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import PasswordChangeView
from django.db import transaction
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View

from audit.services import record_audit

from .forms import InitialSuperadminSetupForm, ManagedUserForm
from .models import LoginThrottle


def client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", maxsplit=1)[0].strip()[:64]
    return request.META.get("REMOTE_ADDR", "")[:64]


class InitialSuperadminSetupView(View):
    template_name = "accounts/initial_setup.html"
    local_addresses = {"127.0.0.1", "::1"}

    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, "ALLOW_INITIAL_SETUP_PAGE", False):
            return HttpResponseForbidden(
                "Halaman setup dimatikan di server. Gunakan perintah setup_superadmin."
            )
        if request.META.get("REMOTE_ADDR", "") not in self.local_addresses:
            return HttpResponseForbidden("Initial setup hanya tersedia dari localhost.")
        if get_user_model().objects.exists():
            return redirect("accounts:login")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render(request, self.template_name, {"form": InitialSuperadminSetupForm()})

    def post(self, request):
        form = InitialSuperadminSetupForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form}, status=400)

        with transaction.atomic():
            user_model = get_user_model()
            if user_model.objects.select_for_update().exists():
                return redirect("accounts:login")
            user = user_model(
                username="vobiasuperadmin",
                is_active=True,
                is_staff=True,
                is_superuser=True,
            )
            user.set_password(form.cleaned_data["password1"])
            user.save()
            record_audit(
                actor=user,
                action="superadmin_created",
                entity_type="accounts.user",
                entity_id=user.pk,
                metadata={"setup_method": "localhost_first_run"},
            )

        login(request, user)
        messages.success(request, "Akun vobiasuperadmin berhasil dibuat.")
        return redirect("dashboard:index")


class LocalLoginView(View):
    template_name = "accounts/login.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)
        return render(request, self.template_name, {"form": AuthenticationForm(request)})

    def post(self, request):
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)

        username = request.POST.get("username", "").strip().lower()
        ip_address = client_ip(request)
        now = timezone.now()

        with transaction.atomic():
            throttle, _ = LoginThrottle.objects.select_for_update().get_or_create(
                username=username,
                ip_address=ip_address,
            )
            if throttle.locked_until and throttle.locked_until > now:
                form = AuthenticationForm(request, data=request.POST)
                form.add_error(None, "Login sementara dikunci. Coba lagi setelah 15 menit.")
                record_audit(
                    actor=None,
                    action="login_blocked",
                    entity_type="authentication",
                    metadata={"username": username, "ip_address": ip_address},
                )
                return render(request, self.template_name, {"form": form}, status=429)

        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            LoginThrottle.objects.filter(
                username=username,
                ip_address=ip_address,
            ).update(failure_count=0, locked_until=None, last_failed_at=None)
            record_audit(
                actor=user,
                action="login_success",
                entity_type="authentication",
                entity_id=str(user.pk),
                metadata={"ip_address": ip_address},
            )
            return redirect(settings.LOGIN_REDIRECT_URL)

        with transaction.atomic():
            throttle = LoginThrottle.objects.select_for_update().get(
                username=username,
                ip_address=ip_address,
            )
            throttle.failure_count += 1
            throttle.last_failed_at = now
            if throttle.failure_count >= settings.LOGIN_FAILURE_LIMIT:
                throttle.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            throttle.save(
                update_fields=["failure_count", "last_failed_at", "locked_until", "updated_at"]
            )

        record_audit(
            actor=None,
            action="login_failed",
            entity_type="authentication",
            metadata={"username": username, "ip_address": ip_address},
        )
        return render(request, self.template_name, {"form": form}, status=401)


class LocalLogoutView(View):
    def post(self, request):
        actor = request.user if request.user.is_authenticated else None
        if actor:
            record_audit(
                actor=actor,
                action="logout",
                entity_type="authentication",
                entity_id=str(actor.pk),
            )
        logout(request)
        return redirect("accounts:login")

    def get(self, request):
        return HttpResponseNotAllowed(["POST"])


class VobiaPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    form_class = PasswordChangeForm
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("dashboard:index")

    def form_valid(self, form):
        response = super().form_valid(form)
        record_audit(
            actor=self.request.user,
            action="password_changed",
            entity_type="accounts.user",
            entity_id=str(self.request.user.pk),
        )
        messages.success(self.request, "Password berhasil diperbarui.")
        return response


superuser_required = user_passes_test(lambda user: user.is_authenticated and user.is_superuser)


@superuser_required
def user_list(request):
    users = get_user_model().objects.order_by("-is_superuser", "username")
    return render(request, "accounts/user_list.html", {"users": users})


@superuser_required
def user_create(request):
    form = ManagedUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        record_audit(
            actor=request.user,
            action="user_created",
            entity_type="accounts.user",
            entity_id=str(user.pk),
            metadata={"username": user.username, "module_access": user.module_access},
        )
        messages.success(request, f"Akun {user.username} berhasil dibuat.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "managed_user": None})


@superuser_required
def user_edit(request, user_id):
    managed_user = get_object_or_404(get_user_model(), pk=user_id)
    form = ManagedUserForm(request.POST or None, instance=managed_user)
    if request.method == "POST" and form.is_valid():
        if managed_user == request.user and not form.cleaned_data["is_active"]:
            form.add_error("is_active", "Akun yang sedang dipakai tidak boleh dinonaktifkan.")
        else:
            user = form.save()
            record_audit(
                actor=request.user,
                action="user_updated",
                entity_type="accounts.user",
                entity_id=str(user.pk),
                metadata={"username": user.username, "module_access": user.module_access},
            )
            messages.success(request, f"Akun {user.username} berhasil diperbarui.")
            return redirect("accounts:user_list")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "managed_user": managed_user},
    )
