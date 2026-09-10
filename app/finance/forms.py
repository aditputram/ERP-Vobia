from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from .models import Account, FINANCE_OPENING_DATE, JournalEntry, JournalLine


class JournalEntryForm(forms.ModelForm):
    class Meta:
        model = JournalEntry
        fields = ("entry_date", "description", "reference")
        widgets = {"entry_date": forms.DateInput(attrs={"type": "date", "min": FINANCE_OPENING_DATE.isoformat()})}


class JournalLineForm(forms.ModelForm):
    class Meta:
        model = JournalLine
        fields = ("account", "description", "debit", "credit")
        widgets = {
            "debit": forms.NumberInput(attrs={"min": 0, "step": "0.000001"}),
            "credit": forms.NumberInput(attrs={"min": 0, "step": "0.000001"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(is_active=True, is_postable=True)


class BaseJournalLineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        debit = Decimal("0")
        credit = Decimal("0")
        line_count = 0
        for form in self.forms:
            data = form.cleaned_data
            if not data or data.get("DELETE"):
                continue
            debit += data.get("debit") or Decimal("0")
            credit += data.get("credit") or Decimal("0")
            line_count += 1
        if line_count < 2:
            raise forms.ValidationError("Jurnal minimal memiliki dua baris.")
        if debit <= 0 or debit != credit:
            raise forms.ValidationError("Total Debit dan Kredit harus sama dan lebih dari nol.")


JournalLineFormSet = inlineformset_factory(
    JournalEntry,
    JournalLine,
    form=JournalLineForm,
    formset=BaseJournalLineFormSet,
    extra=2,
    can_delete=True,
)
