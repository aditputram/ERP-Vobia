from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .access import MODULES, MODULE_TABS
ACCESS_LEVELS = (
    ("none", "Tidak ada akses"),
    ("view", "Lihat"),
    ("edit", "Input / Edit"),
    ("approve", "Approve"),
)


class InitialSuperadminSetupForm(forms.Form):
    password1 = forms.CharField(
        label="Password baru",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Ulangi password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if not password1 or not password2:
            return cleaned
        if password1 != password2:
            raise forms.ValidationError("Password dan konfirmasi tidak sama.")
        candidate = get_user_model()(username="vobiasuperadmin")
        try:
            validate_password(password1, candidate)
        except ValidationError as exc:
            self.add_error("password1", exc)
        return cleaned


class ManagedUserForm(forms.ModelForm):
    password = forms.CharField(
        label="Password baru",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Wajib untuk akun baru. Kosongkan saat edit bila tidak ingin mengganti password.",
    )

    class Meta:
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email", "job_title", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        saved_access = self.instance.module_access if self.instance.pk else {}
        saved_tabs = self.instance.tab_access if self.instance.pk else {}
        self.module_permission_rows = []
        for key, label in MODULES:
            self.fields[f"access_{key}"] = forms.ChoiceField(
                label=label,
                choices=ACCESS_LEVELS,
                initial="approve" if self.instance.is_superuser else saved_access.get(key, "none"),
                disabled=self.instance.is_superuser,
            )
            tab_choices = [
                (tab_key, f"{section} · {tab_label}")
                for tab_key, tab_label, section, _view_names in MODULE_TABS[key]
            ]
            initial_tabs = (
                saved_tabs[key]
                if key in saved_tabs
                else [tab_key for tab_key, _tab_label, _section, _view_names in MODULE_TABS[key]]
            )
            self.fields[f"tabs_{key}"] = forms.MultipleChoiceField(
                label=f"Tab {label}",
                choices=tab_choices,
                initial=initial_tabs,
                required=False,
                disabled=self.instance.is_superuser,
                widget=forms.CheckboxSelectMultiple,
            )
            self.module_permission_rows.append(
                {
                    "key": key,
                    "label": label,
                    "access": self[f"access_{key}"],
                    "tabs": self[f"tabs_{key}"],
                }
            )

    def clean(self):
        cleaned = super().clean()
        for key, label in MODULES:
            level = cleaned.get(f"access_{key}")
            tabs = cleaned.get(f"tabs_{key}") or []
            if level != "none" and not tabs:
                self.add_error(f"tabs_{key}", f"Pilih minimal satu tab untuk modul {label}.")
            if level == "none":
                cleaned[f"tabs_{key}"] = []
        return cleaned

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if not self.instance.pk and not password:
            raise ValidationError("Password wajib diisi untuk akun baru.")
        if password:
            validate_password(password, self.instance)
        return password

    def save(self, commit=True):
        user = super().save(commit=False)
        if not user.is_superuser:
            user.module_access = {
                key: self.cleaned_data[f"access_{key}"] for key, _ in MODULES
            }
            user.tab_access = {
                key: self.cleaned_data[f"tabs_{key}"] for key, _ in MODULES
            }
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        if commit:
            user.save()
        return user
