from pathlib import Path

from django import forms
from django.conf import settings

from .models import TrafficPeriodState


class TrafficUploadForm(forms.Form):
    source = forms.ChoiceField(choices=TrafficPeriodState.Source.choices)
    period_start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    period_end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={"accept": ".xlsx,.csv"}))

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if Path(uploaded.name).suffix.lower() not in {".xlsx", ".csv"}:
            raise forms.ValidationError("Format harus .xlsx atau .csv.")
        if uploaded.size <= 0 or uploaded.size > settings.MASTER_IMPORT_MAX_BYTES:
            raise forms.ValidationError("File kosong atau melebihi batas upload.")
        return uploaded
