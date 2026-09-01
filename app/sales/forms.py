from django import forms
from django.forms import BaseFormSet, formset_factory

from master_data.models import Product, SKU


class ManualSaleHeaderForm(forms.Form):
    SOURCE_CHOICES = [
        ("Offline", "Offline"),
        ("Website", "Website"),
        ("Whatsapp", "Whatsapp"),
        ("KOL", "KOL"),
        ("Live", "Live"),
        ("Disclosure", "Disclosure"),
        ("Internal", "Internal"),
        ("Marketing", "Marketing"),
        ("Event", "Event"),
    ]

    source_label = forms.ChoiceField(label="Source", choices=SOURCE_CHOICES)
    order_number = forms.CharField(label="No. Pesanan / Invoice", max_length=160)
    order_datetime = forms.DateTimeField(
        label="Tanggal & waktu",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    status = forms.ChoiceField(
        label="Status",
        choices=[("Selesai", "Selesai"), ("Pending", "Pending"), ("Retur", "Retur")],
    )
    shipped = forms.BooleanField(label="Barang sudah keluar/dikirim", required=False)


class ProductChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, product):
        return f"{product.name} · {product.parent_sku}" if product.parent_sku else product.name


class SKUChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, sku):
        variant = sku.product_variant
        detail = " / ".join(
            value for value in (("" if variant.name == "Default" else variant.name), sku.size) if value
        )
        return f"{sku.sku} · {detail}" if detail else sku.sku


class ManualSaleLineForm(forms.Form):
    product = ProductChoiceField(
        queryset=Product.objects.filter(
            is_active=True,
            variants__skus__is_active=True,
        ).distinct().order_by("name"),
        label="Product",
    )
    sku = SKUChoiceField(
        queryset=SKU.objects.filter(
            is_active=True,
            product_variant__product__is_active=True,
        ).select_related("product_variant__product"),
        label="SKU",
        widget=forms.Select(attrs={"data-sku-select": ""}),
    )
    quantity = forms.IntegerField(label="Quantity", min_value=1)
    net_unit_price = forms.DecimalField(
        label="Harga net per unit",
        min_value=0,
        decimal_places=4,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1", "inputmode": "decimal"}),
    )

    def clean(self):
        data = super().clean()
        product = data.get("product")
        sku = data.get("sku")
        if product and sku and sku.product_variant.product_id != product.id:
            self.add_error("sku", "SKU harus berasal dari Product yang dipilih.")
        return data


class BaseManualSaleLineFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            sku = form.cleaned_data.get("sku")
            if sku and sku.id in seen:
                form.add_error("sku", "SKU yang sama hanya boleh dipilih sekali per transaksi.")
            if sku:
                seen.add(sku.id)


ManualSaleLineFormSet = formset_factory(
    ManualSaleLineForm,
    formset=BaseManualSaleLineFormSet,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
