from django import forms

from master_data.models import SKU, Supplier


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ("code", "name", "contact_name", "phone")


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
