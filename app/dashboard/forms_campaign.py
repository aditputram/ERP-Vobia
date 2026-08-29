from django import forms
from django.forms import inlineformset_factory
from decimal import Decimal

from master_data.models import Product

from .models import Campaign, CampaignCreative, CampaignExpense, CampaignProduct


class CampaignForm(forms.ModelForm):
    COVER_MAX_BYTES = 5 * 1024 * 1024
    COVER_TYPES = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",), "image/webp": (b"RIFF",)}
    budget = forms.CharField(
        label="Campaign Budget",
        widget=forms.TextInput(attrs={"data-rupiah": "", "inputmode": "numeric", "placeholder": "Rp. 0"}),
    )
    campaign_plan_url = forms.URLField(
        label="Campaign Plan URL",
        help_text="Link moodboard, creative direction, atau dokumen campaign plan.",
    )
    creative_asset_url = forms.URLField(
        label="Creative Asset URL",
        help_text="Link folder atau dokumen berisi creative asset campaign.",
        required=False,
    )

    class Meta:
        model = Campaign
        exclude = (
            "created_by", "actual_spent", "actual_approval_date", "actual_sample_date",
            "actual_creative_date", "actual_prelaunch_date", "actual_launch_date",
        )
        labels = {
            "name": "Campaign Name", "description": "Campaign Description",
            "approval_date": "Approval Campaign Plan", "sample_date": "Product Marketing Sample",
            "creative_date": "Creative Production", "prelaunch_date": "Pre Launch",
            "launch_date": "Launch", "budget": "Campaign Budget",
            "actual_spent": "Actual Campaign Spent",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "cover": forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
            **{name: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}) for name in ("approval_date", "sample_date", "creative_date", "prelaunch_date", "launch_date")},
        }

    def clean_budget(self):
        digits = "".join(character for character in self.cleaned_data["budget"] if character.isdigit())
        if not digits or len(digits) > 18:
            raise forms.ValidationError("Campaign Budget tidak valid.")
        return Decimal(digits)

    def clean_cover(self):
        cover = self.cleaned_data.get("cover")
        if not cover or not hasattr(cover, "content_type"):
            return cover
        if cover.size > self.COVER_MAX_BYTES:
            raise forms.ValidationError("Campaign Cover maksimal 5 MB.")
        signatures = self.COVER_TYPES.get(cover.content_type, ())
        header = cover.read(12)
        cover.seek(0)
        if not signatures or not any(header.startswith(signature) for signature in signatures):
            raise forms.ValidationError("Campaign Cover harus berupa JPG, PNG, atau WebP.")
        if cover.content_type == "image/webp" and header[8:12] != b"WEBP":
            raise forms.ValidationError("Campaign Cover WebP tidak valid.")
        return cover

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


class CampaignExpenseForm(forms.ModelForm):
    amount = forms.CharField(
        label="Nominal",
        widget=forms.TextInput(attrs={"data-rupiah": "", "inputmode": "numeric", "placeholder": "Rp. 0"}),
    )

    class Meta:
        model = CampaignExpense
        fields = ("amount", "description")
        labels = {"description": "Description"}
        widgets = {"description": forms.TextInput(attrs={"placeholder": "Contoh: Production kreatif"})}

    def clean_amount(self):
        digits = "".join(character for character in self.cleaned_data["amount"] if character.isdigit())
        if not digits or len(digits) > 18 or Decimal(digits) <= 0:
            raise forms.ValidationError("Nominal harus lebih dari nol.")
        return Decimal(digits)


class CampaignActualTimelineForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = (
            "actual_approval_date", "actual_sample_date", "actual_creative_date",
            "actual_prelaunch_date", "actual_launch_date",
        )
        labels = {
            "actual_approval_date": "Approval Campaign Plan",
            "actual_sample_date": "Product Marketing Sample",
            "actual_creative_date": "Creative Production",
            "actual_prelaunch_date": "Pre Launch",
            "actual_launch_date": "Launch",
        }
        widgets = {name: forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}) for name in fields}
