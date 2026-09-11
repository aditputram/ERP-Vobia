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
    is_subaccount = forms.BooleanField(
        label="Sub-account",
        required=False,
        help_text="Centang untuk membuat akun anak.",
    )
    opening_balance = forms.DecimalField(
        label="Saldo awal",
        required=False,
        min_value=0,
        max_digits=22,
        decimal_places=6,
        initial=0,
        help_text="Masuk ke opening Draft per 1 September 2026.",
    )
    opening_side = forms.ChoiceField(
        label="Posisi saldo awal",
        choices=(("DEBIT", "Debit"), ("CREDIT", "Kredit")),
        required=False,
        initial="DEBIT",
    )

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
        self.is_edit = not self.instance._state.adding
        parents = Account.objects.filter(is_active=True, is_postable=False)
        if self.is_edit:
            parents = parents.exclude(pk=self.instance.pk)
            self.fields.pop("is_subaccount")
        else:
            self.fields.pop("is_postable")
            self.order_fields(
                (
                    "code",
                    "name",
                    "account_type",
                    "is_subaccount",
                    "parent",
                    "currency",
                    "is_active",
                    "opening_balance",
                    "opening_side",
                )
            )
        self.fields["parent"].queryset = parents
        self.fields["parent"].help_text = "Wajib dipilih jika Sub-account dicentang."

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        current = parent
        while current:
            if self.is_edit and current.pk == self.instance.pk:
                raise forms.ValidationError("Akun induk tidak boleh membentuk siklus.")
            current = current.parent
        return parent

    def clean_is_postable(self):
        is_postable = self.cleaned_data.get("is_postable")
        if self.is_edit and not is_postable and self.instance.journal_lines.exists():
            raise forms.ValidationError("Akun yang sudah dipakai jurnal harus tetap menjadi akun transaksi.")
        if self.is_edit and is_postable and self.instance.children.exists():
            raise forms.ValidationError("Akun yang memiliki akun turunan harus tetap menjadi akun induk.")
        return is_postable

    def clean(self):
        cleaned = super().clean()
        if not self.is_edit:
            is_subaccount = cleaned.get("is_subaccount", False)
            if is_subaccount and not cleaned.get("parent"):
                self.add_error("parent", "Pilih akun induk untuk membuat Sub-account.")
            if not is_subaccount:
                cleaned["parent"] = None
                self.instance.parent = None
            self.instance.is_postable = is_subaccount
        is_postable = cleaned.get("is_postable", self.instance.is_postable)
        if cleaned.get("opening_balance") and not is_postable:
            self.add_error("opening_balance", "Saldo awal hanya dapat diisi pada akun transaksi.")
        if cleaned.get("opening_balance") and not cleaned.get("is_active"):
            self.add_error("opening_balance", "Akun dengan saldo awal harus berstatus aktif.")
        cleaned["opening_side"] = cleaned.get("opening_side") or "DEBIT"
        return cleaned


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
