from django import forms
from django.core.validators import FileExtensionValidator

from master_data.models import SKU, Supplier


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


class POHeaderForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.filter(is_active=True))
    need_month = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    required_arrival = forms.DateField(
        label="Required Arrival",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Tanggal target barang sudah diterima gudang. Belum dihitung otomatis dari lead time.",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ManualPOForm(POHeaderForm):
    sku = forms.ModelChoiceField(queryset=SKU.objects.filter(is_active=True).select_related("product_variant__product"))
    quantity = forms.IntegerField(min_value=1)


class POWIPImportUploadForm(forms.Form):
    file = forms.FileField(
        label="File PO WIP",
        validators=[FileExtensionValidator(allowed_extensions=["xlsx", "csv"])],
        help_text="Gunakan file final berisi NO PO, SKU Induk, SKU, Nama Barang, dan WIP.",
    )
