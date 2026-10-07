from django import forms
from django.core.validators import FileExtensionValidator

from master_data.models import Supplier


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ("code", "name", "contact_name", "phone")


class LegacyWIPSupplierRevisionForm(forms.Form):
    supplier = forms.ModelChoiceField(
        label="Vendor yang benar",
        queryset=Supplier.objects.filter(is_active=True).order_by("name"),
    )
    reason = forms.CharField(
        label="Alasan revisi",
        min_length=10,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Wajib menyebutkan sumber koreksi vendor agar audit trail lengkap.",
    )


class POWIPImportUploadForm(forms.Form):
    file = forms.FileField(
        label="File PO WIP",
        validators=[FileExtensionValidator(allowed_extensions=["xlsx", "csv"])],
        help_text="Gunakan file final berisi NO PO, SKU Induk, SKU, Nama Barang, dan WIP.",
    )
