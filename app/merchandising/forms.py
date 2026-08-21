from django import forms
from django.utils import timezone

from master_data.models import Category, Product, ProductStatus

from .models import ProjectionRule, ProjectionScenario


class ProjectionScenarioForm(forms.ModelForm):
    class Meta:
        model = ProjectionScenario
        fields = ("name", "start_month", "end_month")
        widgets = {"start_month": forms.DateInput(attrs={"type": "date"}), "end_month": forms.DateInput(attrs={"type": "date"})}


class ProjectionBuilderForm(forms.Form):
    scenario = forms.ModelChoiceField(queryset=ProjectionScenario.objects.all())
    target_month = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    scope_type = forms.ChoiceField(choices=ProjectionRule.ScopeType.choices)
    product_status = forms.ModelChoiceField(queryset=ProductStatus.objects.all(), required=False)
    category = forms.ModelChoiceField(queryset=Category.objects.all(), required=False)
    product = forms.ModelChoiceField(queryset=Product.objects.all(), required=False)
    method = forms.ChoiceField(choices=ProjectionRule.Method.choices)
    parameter = forms.DecimalField(min_value=0, decimal_places=4, help_text="Persentase atau target Stock Ratio.")
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        cleaned = super().clean()
        selected = {
            ProjectionRule.ScopeType.PRODUCT_STATUS: cleaned.get("product_status"),
            ProjectionRule.ScopeType.CATEGORY: cleaned.get("category"),
            ProjectionRule.ScopeType.PRODUCT: cleaned.get("product"),
        }
        scope = cleaned.get("scope_type")
        if scope and (not selected.get(scope) or sum(bool(value) for value in selected.values()) != 1):
            raise forms.ValidationError("Pilih tepat satu filter yang sesuai Scope Type.")
        return cleaned


class IncomingMonthCloseForm(forms.Form):
    month = forms.DateField(input_formats=["%Y-%m"], widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    evidence_reference = forms.CharField(max_length=240, help_text="Contoh: warehouse stock opname + inbound reconciliation August 2026")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_month(self):
        value = self.cleaned_data["month"]
        return value.replace(day=1)
