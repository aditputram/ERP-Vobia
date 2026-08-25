from django import forms
from django.utils import timezone

from inventory.models import QCInspection

from .models import ProductionActivity, ProductionOrder, ProductionPlan, ProductionStage
from .services import (
    cmt_line_availability,
    delivery_line_availability,
    qc_line_availability,
    rejected_delivery_line_availability,
)


class ProductionPlanForm(forms.ModelForm):
    change_reason = forms.CharField(
        label="Alasan perubahan plan",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "Wajib bila plan Active diubah.",
            }
        ),
    )

    class Meta:
        model = ProductionPlan
        fields = (
            "target_material_purchase_date",
            "target_trial_date",
            "target_cut_start_date",
            "target_cut_end_date",
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
            "target_material_purchase_date": "Target Pembelian Material",
            "target_trial_date": "Target Trial Production",
            "target_cut_start_date": "Target Mulai Cut",
            "target_cut_end_date": "Target Selesai Cut",
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

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.status == ProductionPlan.Status.ACTIVE:
            changed_business_fields = [name for name in self.changed_data if name != "change_reason"]
            if changed_business_fields and not (cleaned.get("change_reason") or "").strip():
                self.add_error("change_reason", "Alasan wajib diisi ketika mengubah Production Plan Active.")
        return cleaned


class ProductionActivityForm(forms.Form):
    production_order = forms.ModelChoiceField(
        label="Purchase Order",
        queryset=ProductionOrder.objects.none(),
    )
    activity_type = forms.ChoiceField(label="Activity", choices=())
    activity_date = forms.DateField(
        label="Tanggal Activity",
        initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    quantity = forms.IntegerField(label="Qty Activity", min_value=1, required=False)
    notes = forms.CharField(
        label="Catatan / hasil activity",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, production_order=None, eligible_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.cmt_rows = []
        self.qc_rows = []
        self.fields["production_order"].queryset = ProductionOrder.objects.filter(
            po__status="RELEASED",
            plan__status=ProductionPlan.Status.ACTIVE,
        ).select_related("po", "po__supplier")
        self.fields["production_order"].label_from_instance = lambda row: (
            f"{row.po.po_number} · {row.po.supplier.name}"
        )
        self.fields["activity_type"].choices = [("", "Pilih activity...")] + list(eligible_choices)
        if production_order:
            po_lines = list(
                production_order.po.lines.select_related(
                    "sku",
                    "sku__product_variant__product",
                )
                .prefetch_related("qc_inspections")
                .order_by("sku__sku")
            )
            for line in po_lines:
                availability = cmt_line_availability(production_order, line)
                field_name = f"line_quantity_{line.pk}"
                self.fields[field_name] = forms.IntegerField(
                    label="Qty Activity",
                    min_value=0,
                    required=False,
                    widget=forms.NumberInput(
                        attrs={
                            "min": 0,
                            "step": 1,
                            "inputmode": "numeric",
                            "data-cmt-line-quantity": "",
                        }
                    ),
                )
                self.cmt_rows.append(
                    {
                        "line": line,
                        "field_name": field_name,
                        "available_cut_qty": availability[ProductionStage.Stage.CUT],
                        "available_make_qty": availability[ProductionStage.Stage.MAKE],
                        "available_trim_qty": availability[ProductionStage.Stage.TRIM],
                        "available_delivery_qty": delivery_line_availability(production_order, line),
                        "available_rejected_delivery_qty": rejected_delivery_line_availability(production_order, line),
                    }
                )
                qc_availability = qc_line_availability(production_order, line)
                inspected_name = f"qc_inspected_{line.pk}"
                passed_name = f"qc_passed_{line.pk}"
                disposition_name = f"qc_disposition_{line.pk}"
                reason_name = f"qc_failure_reason_{line.pk}"
                disabled = qc_availability["remaining_qty"] <= 0
                self.fields[inspected_name] = forms.IntegerField(
                    label="Qty Diperiksa",
                    min_value=0,
                    max_value=int(qc_availability["remaining_qty"]),
                    required=False,
                    disabled=disabled,
                    widget=forms.NumberInput(
                        attrs={"min": 0, "step": 1, "inputmode": "numeric", "data-qc-inspected": ""}
                    ),
                )
                self.fields[passed_name] = forms.IntegerField(
                    label="Qty Lolos",
                    min_value=0,
                    required=False,
                    disabled=disabled,
                    widget=forms.NumberInput(
                        attrs={"min": 0, "step": 1, "inputmode": "numeric", "data-qc-passed": ""}
                    ),
                )
                self.fields[disposition_name] = forms.ChoiceField(
                    label="Tindak Lanjut Barang Gagal",
                    choices=[("", "— Pilih tindak lanjut —")] + list(QCInspection.Disposition.choices),
                    required=False,
                    disabled=disabled,
                    widget=forms.Select(attrs={"data-qc-disposition": ""}),
                )
                self.fields[reason_name] = forms.CharField(
                    label="Alasan Gagal",
                    required=False,
                    disabled=disabled,
                    widget=forms.TextInput(
                        attrs={"placeholder": "Contoh: jahitan lepas", "data-qc-failure-reason": ""}
                    ),
                )
                self.qc_rows.append(
                    {
                        "line": line,
                        "inspected_name": inspected_name,
                        "passed_name": passed_name,
                        "disposition_name": disposition_name,
                        "reason_name": reason_name,
                        **qc_availability,
                    }
                )

    @property
    def cmt_line_fields(self):
        return [
            {
                "line": row["line"],
                "field": self[row["field_name"]],
                "available_cut_qty": row["available_cut_qty"],
                "available_make_qty": row["available_make_qty"],
                "available_trim_qty": row["available_trim_qty"],
                "available_delivery_qty": row["available_delivery_qty"],
                "available_rejected_delivery_qty": row["available_rejected_delivery_qty"],
            }
            for row in self.cmt_rows
        ]

    def cmt_line_quantities(self):
        return [
            (row["line"], self.cleaned_data.get(row["field_name"]))
            for row in self.cmt_rows
            if (self.cleaned_data.get(row["field_name"]) or 0) > 0
        ]

    @property
    def qc_line_fields(self):
        return [
            {
                "line": row["line"],
                "inspected_field": self[row["inspected_name"]],
                "passed_field": self[row["passed_name"]],
                "disposition_field": self[row["disposition_name"]],
                "reason_field": self[row["reason_name"]],
                "trim_qty": row["trim_qty"],
                "inspected_qty": row["inspected_qty"],
                "remaining_qty": row["remaining_qty"],
            }
            for row in self.qc_rows
        ]

    def qc_line_results(self):
        results = []
        for row in self.qc_rows:
            inspected = self.cleaned_data.get(row["inspected_name"]) or 0
            if inspected <= 0:
                continue
            passed = self.cleaned_data.get(row["passed_name"]) or 0
            results.append(
                (
                    row["line"],
                    inspected,
                    passed,
                    inspected - passed,
                    self.cleaned_data.get(row["disposition_name"], ""),
                    (self.cleaned_data.get(row["reason_name"]) or "").strip(),
                )
            )
        return results

    def clean(self):
        cleaned = super().clean()
        activity_type = cleaned.get("activity_type")
        if activity_type in {
            ProductionActivity.ActivityType.CUT,
            ProductionActivity.ActivityType.MAKE,
            ProductionActivity.ActivityType.TRIM,
            ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
            ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY,
        } and not any((cleaned.get(row["field_name"]) or 0) > 0 for row in self.cmt_rows):
            raise forms.ValidationError(
                f"Isi minimal satu Qty {dict(ProductionActivity.ActivityType.choices)[activity_type]} per SKU."
            )
        if activity_type == ProductionActivity.ActivityType.QC:
            has_inspected_qty = False
            for row in self.qc_rows:
                inspected = cleaned.get(row["inspected_name"]) or 0
                passed = cleaned.get(row["passed_name"])
                disposition = cleaned.get(row["disposition_name"], "")
                reason = (cleaned.get(row["reason_name"]) or "").strip()
                if inspected <= 0:
                    if (passed or 0) > 0:
                        self.add_error(row["passed_name"], "Isi Qty Diperiksa terlebih dahulu.")
                    continue
                has_inspected_qty = True
                if passed is None:
                    self.add_error(row["passed_name"], "Qty Lolos wajib diisi.")
                    continue
                if passed > inspected:
                    self.add_error(row["passed_name"], "Qty Lolos tidak boleh melebihi Qty Diperiksa.")
                elif inspected - passed > 0 and not disposition:
                    self.add_error(
                        row["disposition_name"],
                        "Tindak lanjut wajib dipilih jika ada Qty Gagal.",
                    )
                if passed is not None and inspected - passed > 0 and not reason:
                    self.add_error(row["reason_name"], "Alasan gagal wajib diisi.")
            if not has_inspected_qty:
                raise forms.ValidationError("Isi minimal satu Qty Diperiksa per SKU.")
        if activity_type == ProductionActivity.ActivityType.TRIAL_REVISION and not (
            cleaned.get("notes") or ""
        ).strip():
            self.add_error("notes", "Alasan revisi Trial wajib diisi.")
        return cleaned


class ProductionCorrectionForm(forms.Form):
    activity_date = forms.DateField(
        label="Tanggal Activity yang benar",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    quantity = forms.IntegerField(label="Qty yang benar", min_value=0, required=False)
    qty_inspected = forms.IntegerField(label="Qty diperiksa yang benar", min_value=1, required=False)
    qty_passed = forms.IntegerField(label="Qty lolos yang benar", min_value=0, required=False)
    qty_failed = forms.IntegerField(label="Qty gagal yang benar", min_value=0, required=False)
    failed_disposition = forms.ChoiceField(
        label="Tindak lanjut barang gagal",
        choices=[("", "—")] + list(QCInspection.Disposition.choices),
        required=False,
    )
    notes = forms.CharField(label="Catatan activity yang benar", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    reason = forms.CharField(label="Alasan koreksi", widget=forms.Textarea(attrs={"rows": 3}))


class ReworkCompletionForm(forms.Form):
    activity_date = forms.DateField(
        label="Tanggal Rework Selesai",
        initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    notes = forms.CharField(
        label="Catatan Perbaikan",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Jelaskan perbaikan yang dilakukan."}),
    )


class ReQCForm(forms.Form):
    activity_date = forms.DateField(
        label="Tanggal Re-QC",
        initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    qty_passed = forms.IntegerField(
        label="Qty Lolos Re-QC",
        min_value=0,
        widget=forms.NumberInput(attrs={"min": 0, "step": 1, "data-re-qc-passed": ""}),
    )
    failed_disposition = forms.ChoiceField(
        label="Tindak Lanjut Jika Masih Gagal",
        required=False,
        choices=(
            ("", "— Tidak ada barang gagal —"),
            (QCInspection.Disposition.REWORK, "Rework Lagi"),
            (QCInspection.Disposition.REJECTED, "Rejected"),
        ),
    )
    notes = forms.CharField(label="Catatan Re-QC", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, max_qty, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_qty = int(max_qty)
        self.fields["qty_passed"].max_value = self.max_qty
        self.fields["qty_passed"].widget.attrs["max"] = self.max_qty
        self.fields["qty_passed"].widget.attrs["data-re-qc-total"] = self.max_qty

    def clean(self):
        cleaned = super().clean()
        passed = cleaned.get("qty_passed")
        if passed is not None and passed < self.max_qty and not cleaned.get("failed_disposition"):
            self.add_error("failed_disposition", "Pilih Rework Lagi atau Rejected untuk Qty yang masih gagal.")
        return cleaned


class ProductionStageUpdateForm(forms.ModelForm):
    is_blocked = forms.BooleanField(
        label="Sedang terkendala",
        required=False,
        help_text="Aktifkan jika tahap sedang berhenti karena kendala. Progress Qty tetap tersimpan.",
    )

    class Meta:
        model = ProductionStage
        fields = (
            "status",
            "completed_qty",
            "target_start_date",
            "target_end_date",
            "actual_start_date",
            "actual_end_date",
            "material_arrival_date",
            "progress_percent",
            "notes",
        )
        widgets = {
            "target_start_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "target_end_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "actual_start_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "actual_end_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "material_arrival_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "completed_qty": forms.NumberInput(attrs={"min": 0, "step": 1}),
        }
        labels = {
            "status": "Status",
            "completed_qty": "Qty selesai",
            "target_start_date": "Target mulai",
            "target_end_date": "Target selesai",
            "actual_start_date": "Aktual mulai",
            "actual_end_date": "Aktual selesai",
            "material_arrival_date": "Material datang di tempat produksi",
            "progress_percent": "Progress (%)",
            "notes": "Catatan / kendala",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cmt_labels = {
            ProductionStage.Stage.CUT: "Qty sudah di-Cut",
            ProductionStage.Stage.MAKE: "Qty sudah di-Make",
            ProductionStage.Stage.TRIM: "Qty sudah di-Trim",
        }
        if self.instance.stage in cmt_labels:
            self.fields.pop("status")
            self.fields.pop("progress_percent")
            self.fields["completed_qty"].label = cmt_labels[self.instance.stage]
            self.fields["completed_qty"].help_text = "Isi total kumulatif yang sudah selesai pada tahap ini."
            if not self.is_bound and self.instance.completed_qty == 0:
                # The database keeps zero as the calculation-safe default, but an
                # untouched CMT input should look empty until the user records qty.
                self.initial["completed_qty"] = ""
            self.fields["is_blocked"].initial = self.instance.status == ProductionStage.Status.BLOCKED
            self.fields.pop("material_arrival_date")
        else:
            self.fields.pop("completed_qty")
            self.fields.pop("is_blocked")
            if self.instance.stage == ProductionStage.Stage.MATERIAL_PURCHASE:
                self.fields.pop("target_end_date")
                self.fields.pop("actual_end_date")
                self.fields.pop("progress_percent")
                self.fields["target_start_date"].label = "Target Pembelian Material"
                self.fields["actual_start_date"].label = "Aktual Pembelian Material"
                self.fields["status"].choices = (
                    (ProductionStage.Status.NOT_STARTED, "Belum di beli"),
                    (ProductionStage.Status.IN_PROGRESS, "Menunggu ketersediaan Material"),
                    (ProductionStage.Status.COMPLETE, "Material siap diproses"),
                )
                self.fields["status"].help_text = (
                    "Status otomatis menjadi Material siap diproses ketika tanggal material datang diisi."
                )
                if self.instance.status == ProductionStage.Status.BLOCKED:
                    self.initial["status"] = ProductionStage.Status.IN_PROGRESS

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        if (
            self.instance.stage == ProductionStage.Stage.MATERIAL_PURCHASE
            and cleaned.get("material_arrival_date")
        ):
            status = ProductionStage.Status.COMPLETE
            cleaned["status"] = status
            self.instance.status = status
            self.instance.progress_percent = 100
        if status == ProductionStage.Status.COMPLETE:
            self.instance.progress_percent = 100
        elif status == ProductionStage.Status.NOT_STARTED:
            self.instance.progress_percent = 0
        completed_qty = cleaned.get("completed_qty")
        if completed_qty is not None and completed_qty != completed_qty.to_integral_value():
            self.add_error("completed_qty", "Qty Production harus berupa bilangan bulat.")
        return cleaned


class TrialStartForm(forms.Form):
    target_trial_date = forms.DateField(
        label="Target Tanggal Trial Production",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )


class TrialSubmitForm(forms.Form):
    trial_date = forms.DateField(
        label="Trial Production Approved",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )


class TrialNoteForm(forms.Form):
    note = forms.CharField(
        label="Hasil dan Catatan Trial",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Tulis satu catatan baru..."}),
    )


class TrialDecisionForm(forms.Form):
    decision_notes = forms.CharField(
        label="Catatan keputusan",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
