from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


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

