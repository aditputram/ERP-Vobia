from pathlib import Path

from django import forms
from django.conf import settings
from django.utils import timezone

from master_data.models import SKU, Warehouse
from purchasing.models import PurchaseOrderLine
from sales.models import SalesOrderLine

from .models import InventoryException, InventoryMovement, PhysicalReturnReceipt, QCInspection


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ("code", "name", "address")


class OpeningForm(forms.Form):
    sku = forms.ModelChoiceField(queryset=SKU.objects.filter(is_active=True))
    quantity = forms.DecimalField(decimal_places=0)
    frozen_unit_cogs = forms.DecimalField(min_value=0, decimal_places=4)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_active=True), required=False)
    reason = forms.CharField(initial="FIFO opening snapshot EOD 31 July 2026", widget=forms.Textarea(attrs={"rows": 2}))


class FIFOOpeningImportUploadForm(forms.Form):
    file = forms.FileField(
        label="Export FIFO Opening",
        help_text="Gunakan export .xlsx/.csv dari tab FIFO Opening di Vobia MD 2026.",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx,.csv"}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if Path(uploaded.name).suffix.lower() not in {".xlsx", ".csv"}:
            raise forms.ValidationError("Format harus .xlsx atau .csv.")
        if uploaded.size <= 0:
            raise forms.ValidationError("File kosong.")
        if uploaded.size > settings.MASTER_IMPORT_MAX_BYTES:
            raise forms.ValidationError("File melebihi batas upload 25 MB.")
        return uploaded


class QCForm(forms.Form):
    po_line = forms.ModelChoiceField(
        label="Purchase Order + SKU",
        queryset=PurchaseOrderLine.objects.filter(po__status="RELEASED").select_related("po", "sku")
    )
    inspected_at = forms.DateTimeField(label="Waktu QC", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    qty_inspected = forms.IntegerField(label="Qty diperiksa", min_value=1)
    qty_passed = forms.IntegerField(label="Qty lolos", min_value=0)
    qty_failed = forms.IntegerField(label="Qty gagal", min_value=0)
    failed_disposition = forms.ChoiceField(label="Tindak lanjut barang gagal", choices=[("", "—")]+list(QCInspection.Disposition.choices), required=False)
    notes = forms.CharField(label="Catatan QC", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, production_gate=False, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = PurchaseOrderLine.objects.filter(po__status="RELEASED").select_related(
            "po", "sku", "sku__product_variant__product"
        )
        if production_gate:
            queryset = queryset.filter(
                po__production_order__stages__stage="TRIM",
                po__production_order__stages__completed_qty__gt=0,
            ).distinct()
        self.fields["po_line"].queryset = queryset
        self.fields["po_line"].label_from_instance = lambda line: (
            f"{line.po.po_number} · {line.sku.sku} · {line.sku.product_variant.product.name}"
        )


class InboundForm(forms.Form):
    po_line = forms.ModelChoiceField(
        queryset=PurchaseOrderLine.objects.filter(
            po__status="RELEASED",
            po__source="LEGACY_WIP",
        ).select_related("po", "sku")
    )
    inbound_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    received_qty = forms.IntegerField(min_value=1)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_active=True))
    reference = forms.CharField(max_length=120, help_text="Nomor GRN/surat jalan unik.")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class DeliveryReceiveForm(forms.Form):
    delivery_activity = forms.UUIDField(widget=forms.HiddenInput)
    inbound_date = forms.DateField(
        label="Tanggal diterima gudang",
        initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    received_qty = forms.IntegerField(label="Quantity Received", min_value=1)
    warehouse = forms.ModelChoiceField(
        label="Gudang penerima",
        queryset=Warehouse.objects.filter(is_active=True),
    )
    notes = forms.CharField(label="Catatan", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, max_qty=None, min_date=None, default_warehouse_code="MAIN", **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_qty = int(max_qty) if max_qty is not None else None
        self.min_date = min_date
        if not self.is_bound:
            self.fields["warehouse"].initial = self.fields["warehouse"].queryset.filter(
                code=default_warehouse_code
            ).first()
            if min_date is not None:
                self.fields["inbound_date"].initial = max(timezone.localdate(), min_date)
        if min_date is not None:
            self.fields["inbound_date"].widget.attrs["min"] = min_date.isoformat()
        if max_qty is not None:
            self.fields["received_qty"].max_value = int(max_qty)
            self.fields["received_qty"].widget.attrs["max"] = int(max_qty)

    def clean_inbound_date(self):
        inbound_date = self.cleaned_data["inbound_date"]
        if self.min_date is not None and inbound_date < self.min_date:
            raise forms.ValidationError(
                f"Tanggal Diterima minimal {self.min_date:%d/%m/%Y}, sama dengan Tanggal Kirim."
            )
        return inbound_date

    def clean_received_qty(self):
        quantity = self.cleaned_data["received_qty"]
        if self.expected_qty is not None and quantity != self.expected_qty:
            raise forms.ValidationError(
                f"Quantity Received harus sama dengan Qty Delivering ({self.expected_qty} pcs)."
            )
        return quantity


class ReturnForm(forms.Form):
    sales_line = forms.ModelChoiceField(
        queryset=SalesOrderLine.objects.filter(order__current_status="Retur").select_related("order", "sku")
    )
    received_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    quantity = forms.IntegerField(min_value=1)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_active=True))
    condition = forms.ChoiceField(choices=PhysicalReturnReceipt.Condition.choices)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class AdjustmentForm(forms.Form):
    sku = forms.ModelChoiceField(queryset=SKU.objects.filter(is_active=True))
    movement_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    direction = forms.ChoiceField(choices=InventoryMovement.Direction.choices)
    quantity = forms.IntegerField(min_value=1)
    unit_cost = forms.DecimalField(min_value=0, decimal_places=4, required=False, help_text="Wajib untuk Adjustment In.")
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_active=True), required=False)
    exception = forms.ModelChoiceField(
        queryset=InventoryException.objects.filter(status=InventoryException.Status.OPEN).select_related("sku"),
        required=False,
        help_text="Opsional; pilih hanya bila adjustment ini adalah koreksi exception tersebut.",
    )
    evidence_reference = forms.CharField(max_length=240)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))
