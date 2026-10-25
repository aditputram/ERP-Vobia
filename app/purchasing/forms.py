from django import forms
from django.core.validators import FileExtensionValidator

from master_data.models import Supplier

from .models import PPICProductionPlan


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ("code", "name", "contact_name", "phone")


class PPICProductionPlanForm(forms.ModelForm):
    class Meta:
        model = PPICProductionPlan
        fields = (
            "production_method",
            "supplier",
            "control_material_purchase",
            "control_trial",
            "control_cut",
            "control_additional_process",
            "control_make",
            "control_trim",
            "control_qc",
            "control_inbound",
            "target_material_purchase_date",
            "target_trial_date",
            "target_cut_start_date",
            "target_cut_end_date",
            "additional_process_name",
            "target_additional_process_start_date",
            "target_additional_process_end_date",
            "target_make_start_date",
            "target_make_end_date",
            "target_trim_start_date",
            "target_trim_end_date",
            "target_qc_start_date",
            "target_qc_end_date",
            "target_inbound_date",
            "notes",
        )
        labels = {
            "production_method": "Production Method",
            "supplier": "FPP Vendor",
            "control_material_purchase": "Material Purchase",
            "control_trial": "Trial Production",
            "control_cut": "Cut",
            "control_additional_process": "Additional Process",
            "control_make": "Make",
            "control_trim": "Trim",
            "control_qc": "Quality Control",
            "control_inbound": "Inbound",
            "target_material_purchase_date": "Target Pembelian Material",
            "target_trial_date": "Target Trial Production",
            "target_cut_start_date": "Target Mulai Cut",
            "target_cut_end_date": "Target Selesai Cut",
            "additional_process_name": "Nama Additional Process",
            "target_additional_process_start_date": "Target Mulai Additional Process",
            "target_additional_process_end_date": "Target Selesai Additional Process",
            "target_make_start_date": "Target Mulai Make",
            "target_make_end_date": "Target Selesai Make",
            "target_trim_start_date": "Target Mulai Trim",
            "target_trim_end_date": "Target Selesai Trim",
            "target_qc_start_date": "Target Mulai QC",
            "target_qc_end_date": "Target Selesai QC",
            "target_inbound_date": "Target Inbound",
            "notes": "Catatan Production Plan",
        }
        widgets = {
            **{
                field: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
                for field in (
                    "target_material_purchase_date",
                    "target_trial_date",
                    "target_cut_start_date",
                    "target_cut_end_date",
                    "target_additional_process_start_date",
                    "target_additional_process_end_date",
                    "target_make_start_date",
                    "target_make_end_date",
                    "target_trim_start_date",
                    "target_trim_end_date",
                    "target_qc_start_date",
                    "target_qc_end_date",
                    "target_inbound_date",
                )
            },
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by("name")
        self.fields["additional_process_name"].widget.attrs["placeholder"] = "Contoh: Bordir / Sablon"

    def clean(self):
        cleaned = super().clean()
        controls = (
            "control_material_purchase",
            "control_trial",
            "control_cut",
            "control_additional_process",
            "control_make",
            "control_trim",
            "control_qc",
            "control_inbound",
        )
        if not any(cleaned.get(name) for name in controls):
            raise forms.ValidationError("Pilih minimal satu proses yang akan dikontrol.")
        return cleaned


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
