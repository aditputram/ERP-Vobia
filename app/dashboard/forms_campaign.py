from django import forms
from django.forms import inlineformset_factory
from decimal import Decimal

from master_data.models import Product

from .models import Campaign, CampaignCreative, CampaignProduct


class CampaignForm(forms.ModelForm):
    budget = forms.CharField(
        label="Campaign Budget",
        widget=forms.TextInput(attrs={"data-rupiah": "", "inputmode": "numeric", "placeholder": "Rp. 0"}),
    )
    campaign_plan_url = forms.URLField(
        label="Campaign Plan URL",
        help_text="Link moodboard, creative direction, atau dokumen campaign plan.",
    )

    class Meta:
        model = Campaign
        exclude = ("created_by", "actual_spent")
        labels = {
            "name": "Campaign Name", "description": "Campaign Description",
            "approval_date": "Approval Campaign Plan", "sample_date": "Product Marketing Sample",
            "creative_date": "Creative Production", "prelaunch_date": "Pre Launch",
            "launch_date": "Launch", "budget": "Campaign Budget",
            "actual_spent": "Actual Campaign Spent",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            **{name: forms.DateInput(attrs={"type": "date"}) for name in ("approval_date", "sample_date", "creative_date", "prelaunch_date", "launch_date")},
        }

    def clean_budget(self):
        digits = "".join(character for character in self.cleaned_data["budget"] if character.isdigit())
        if not digits or len(digits) > 18:
            raise forms.ValidationError("Campaign Budget tidak valid.")
        return Decimal(digits)

    def clean(self):
        data = super().clean()
        dates = [data.get(name) for name in ("approval_date", "sample_date", "creative_date", "prelaunch_date", "launch_date")]
        if all(dates) and dates != sorted(dates):
            raise forms.ValidationError("Timeline harus berurutan dari Approval hingga Launch.")
        return data


class CampaignProductForm(forms.ModelForm):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True).order_by("name"))

    class Meta:
        model = CampaignProduct
        fields = ("product", "target_qty")

    def clean(self):
        data = super().clean()
        product = data.get("product")
        if product:
            prices = list(product.variants.filter(skus__is_active=True).values_list("skus__current_retail_price", flat=True).distinct())
            if None in prices or len(prices) != 1:
                self.add_error("product", "Retail Price SKU aktif harus terisi dan sama untuk Product ini.")
            else:
                self.instance.retail_price_snapshot = prices[0]
                self.instance.target_gross_sales = prices[0] * data.get("target_qty", 0)
        return data


CampaignProductFormSet = inlineformset_factory(Campaign, CampaignProduct, form=CampaignProductForm, extra=5, can_delete=True, min_num=1, validate_min=True)


class CreativeForm(forms.ModelForm):
    class Meta:
        model = CampaignCreative
        fields = ("platform", "post_url")

    def clean_post_url(self):
        url = self.cleaned_data["post_url"]
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        platform = self.data.get("platform")
        allowed = {"INSTAGRAM": {"instagram.com", "www.instagram.com"}, "TIKTOK": {"tiktok.com", "www.tiktok.com", "vm.tiktok.com"}}
        if host not in allowed.get(platform, set()):
            raise forms.ValidationError("Domain link harus sesuai platform yang dipilih.")
        return url


class CampaignSpendForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ("budget", "actual_spent")
        labels = {"budget": "Campaign Budget", "actual_spent": "Actual Campaign Spent"}
