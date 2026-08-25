from django import forms

from master_data.models import SKU


class ManualSaleForm(forms.Form):
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
    sku = forms.ModelChoiceField(queryset=SKU.objects.filter(is_active=True))
    quantity = forms.IntegerField(label="Quantity", min_value=1)
    net_unit_price = forms.DecimalField(
        label="Harga net per unit",
        min_value=0,
        decimal_places=4,
    )
    status = forms.ChoiceField(
        label="Status",
        choices=[("Selesai", "Selesai"), ("Pending", "Pending"), ("Retur", "Retur")],
    )
    shipped = forms.BooleanField(label="Barang sudah keluar/dikirim", required=False)
