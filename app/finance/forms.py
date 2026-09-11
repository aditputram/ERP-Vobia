from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from .models import Account, FINANCE_OPENING_DATE, JournalEntry, JournalLine


ACCOUNT_TYPE_CHOICES = (
    ("BANK", "BANK · Kas & Bank"),
    ("AREC", "AREC · Piutang Usaha"),
    ("INTR", "INTR · Persediaan"),
    ("OASS", "OASS · Aset Lancar Lainnya"),
    ("OCAS", "OCAS · Aset Lainnya"),
    ("FASS", "FASS · Aset Tetap"),
    ("DEPR", "DEPR · Akumulasi Penyusutan"),
    ("APAY", "APAY · Utang Usaha"),
    ("OCLY", "OCLY · Liabilitas Lancar Lainnya"),
    ("LTLY", "LTLY · Liabilitas Jangka Panjang"),
    ("EQTY", "EQTY · Ekuitas"),
    ("REVE", "REVE · Pendapatan"),
    ("COGS", "COGS · Beban Pokok Penjualan"),
    ("EXPS", "EXPS · Beban"),
    ("OINC", "OINC · Pendapatan Lainnya"),
    ("OEXP", "OEXP · Beban Lainnya"),
)


class AccountForm(forms.ModelForm):
    account_type = forms.ChoiceField(label="Tipe akun", choices=ACCOUNT_TYPE_CHOICES)

    class Meta:
        model = Account
        fields = ("code", "name", "account_type", "parent", "currency", "is_postable", "is_active")
        labels = {
            "code": "Kode akun",
            "name": "Nama akun",
            "parent": "Akun induk",
            "currency": "Mata uang",
            "is_postable": "Akun transaksi",
            "is_active": "Aktif",
        }
        help_texts = {
            "parent": "Pilih akun induk bila akun ini merupakan turunan.",
            "is_postable": "Matikan untuk membuat akun induk yang tidak dapat dipakai pada jurnal.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = Account.objects.filter(is_active=True, is_postable=False)


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
