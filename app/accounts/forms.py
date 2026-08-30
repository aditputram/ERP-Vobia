from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


MODULES = (
    ("sales", "Sales"),
    ("operation", "Operation"),
    ("marketing", "Marketing"),
    ("master_data", "Master Data"),
    ("reconciliation", "Reconciliation"),
    ("guide", "Panduan & UAT"),
)
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
        for key, label in MODULES:
            self.fields[f"access_{key}"] = forms.ChoiceField(
                label=label,
                choices=ACCESS_LEVELS,
                initial="approve" if self.instance.is_superuser else saved_access.get(key, "none"),
                disabled=self.instance.is_superuser,
            )

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
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        if commit:
            user.save()
        return user
