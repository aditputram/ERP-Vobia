from pathlib import Path

from django import forms
from django.conf import settings

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
        queryset=PurchaseOrderLine.objects.filter(po__status="RELEASED").select_related("po", "sku")
    )
    inspected_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    qty_inspected = forms.IntegerField(min_value=1)
    qty_passed = forms.IntegerField(min_value=0)
    qty_failed = forms.IntegerField(min_value=0)
    failed_disposition = forms.ChoiceField(choices=[("", "—")]+list(QCInspection.Disposition.choices), required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class InboundForm(forms.Form):
    po_line = forms.ModelChoiceField(
        queryset=PurchaseOrderLine.objects.filter(po__status="RELEASED").select_related("po", "sku")
    )
    inbound_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    received_qty = forms.IntegerField(min_value=1)
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.filter(is_active=True))
    reference = forms.CharField(max_length=120, help_text="Nomor GRN/surat jalan unik.")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


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
