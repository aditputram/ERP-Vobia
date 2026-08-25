from pathlib import Path

from django import forms
from django.conf import settings

from .models import SalesImportBatch


class MasterImportUploadForm(forms.Form):
    file = forms.FileField(
        label="File Bank Data",
        help_text="Gunakan export canonical Bank Data All Source 26 dalam format .xlsx atau .csv.",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx,.csv"}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        extension = Path(uploaded.name).suffix.lower()
        if extension not in {".xlsx", ".csv"}:
            raise forms.ValidationError("Format harus .xlsx atau .csv.")
        if uploaded.size <= 0:
            raise forms.ValidationError("File kosong.")
        if uploaded.size > settings.MASTER_IMPORT_MAX_BYTES:
            limit_mb = settings.MASTER_IMPORT_MAX_BYTES // (1024 * 1024)
            raise forms.ValidationError(f"Ukuran file melebihi batas {limit_mb} MB.")
        return uploaded


class SalesImportUploadForm(forms.Form):
    source = forms.ChoiceField(
        label="Marketplace",
        choices=(
            (SalesImportBatch.Source.SHOPEE, "Shopee"),
            (SalesImportBatch.Source.TIKTOK, "TikTok"),
        ),
    )
    file = forms.FileField(
        label="File transaksi mentah",
        help_text="Gunakan file export asli Shopee atau TikTok dalam format .xlsx/.csv.",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx,.csv"}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        extension = Path(uploaded.name).suffix.lower()
        if extension not in {".xlsx", ".csv"}:
            raise forms.ValidationError("Format harus .xlsx atau .csv.")
        if uploaded.size <= 0:
            raise forms.ValidationError("File kosong.")
        if uploaded.size > settings.MASTER_IMPORT_MAX_BYTES:
            limit_mb = settings.MASTER_IMPORT_MAX_BYTES // (1024 * 1024)
            raise forms.ValidationError(f"Ukuran file melebihi batas {limit_mb} MB.")
        return uploaded
