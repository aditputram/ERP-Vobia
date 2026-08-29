from decimal import Decimal
from urllib.parse import urlparse

from django import forms
from django.forms import inlineformset_factory

from master_data.models import Product

from .models import KolPartnership, KolProduct


def validate_post_url(url, platform):
    if not url:
        return url
    host = (urlparse(url).hostname or "").lower()
    allowed = {"INSTAGRAM": {"instagram.com", "www.instagram.com"}, "TIKTOK": {"tiktok.com", "www.tiktok.com", "vm.tiktok.com"}}
    if host not in allowed.get(platform, set()):
        raise forms.ValidationError("Domain link harus sesuai platform yang dipilih.")
    return url


class KolPartnershipForm(forms.ModelForm):
    budget = forms.CharField(widget=forms.TextInput(attrs={"data-rupiah": "", "inputmode": "numeric", "placeholder": "Rp. 0"}))

    class Meta:
        model = KolPartnership
        fields = ("campaign", "kol_name", "platform", "budget", "post_url")
        labels = {"kol_name": "Nama KOL", "budget": "Budget per Posting", "post_url": "Link Konten"}
        help_texts = {"post_url": "Boleh dikosongkan sampai konten tayang."}

    def clean_budget(self):
        digits = "".join(character for character in self.cleaned_data["budget"] if character.isdigit())
        if not digits or len(digits) > 18:
            raise forms.ValidationError("Budget tidak valid.")
        return Decimal(digits)

    def clean_post_url(self):
        return validate_post_url(self.cleaned_data.get("post_url", ""), self.data.get("platform"))


class KolPostUrlForm(forms.ModelForm):
    class Meta:
        model = KolPartnership
        fields = ("post_url",)
        labels = {"post_url": "Link Konten"}

    def clean_post_url(self):
        return validate_post_url(self.cleaned_data["post_url"], self.instance.platform)


class KolMetricForm(forms.ModelForm):
    class Meta:
        model = KolPartnership
        fields = ("views", "likes", "comments", "saves", "shares")


class KolProductForm(forms.ModelForm):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True).order_by("name"))

    class Meta:
        model = KolProduct
        fields = ("product", "quantity")
        labels = {"quantity": "Qty"}


KolProductFormSet = inlineformset_factory(KolPartnership, KolProduct, form=KolProductForm, extra=3, can_delete=True, min_num=1, validate_min=True)
