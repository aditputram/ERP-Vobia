from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from .models import (
    Collection,
    DevelopmentProduct,
    DevelopmentProductMaterial,
    MarketingRecommendation,
)


class CollectionForm(forms.ModelForm):
    class Meta:
        model = Collection
        fields = ("code", "name", "objective")
        labels = {
            "code": "Collection Code",
            "name": "Collection Name",
            "objective": "Tujuan Collection",
        }
        help_texts = {
            "objective": "Contoh: Membuat koleksi tas kerja perempuan yang ringan, rapi, dan terjangkau.",
        }
        widgets = {
            "objective": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Jelaskan tujuan utama dan kebutuhan pelanggan yang ingin dijawab.",
                }
            ),
        }

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()


class DevelopmentProductForm(forms.ModelForm):
    FILE_MAX_BYTES = 10 * 1024 * 1024
    FILE_TYPES = {
        "application/pdf": (b"%PDF",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/webp": (b"RIFF",),
    }

    class Meta:
        model = DevelopmentProduct
        fields = (
            "name",
            "category",
            "product_cover",
            "mockup",
            "technical_drawing",
        )
        labels = {
            "name": "Nama Product",
            "product_cover": "Upload Product Cover",
            "mockup": "Upload MDR",
            "technical_drawing": "Upload Technical Drawing",
        }
        help_texts = {
            "product_cover": "JPG, PNG, atau WebP. Maksimal 10 MB.",
            "mockup": "PDF, JPG, PNG, atau WebP. Maksimal 10 MB.",
            "technical_drawing": "PDF, JPG, PNG, atau WebP. Maksimal 10 MB.",
        }
        widgets = {
            "product_cover": forms.FileInput(attrs={"accept": ".jpg,.jpeg,.png,.webp"}),
            "mockup": forms.FileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png,.webp"}),
            "technical_drawing": forms.FileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png,.webp"}),
        }

    def _clean_file(self, field_name, *, image_only=False):
        uploaded = self.cleaned_data.get(field_name)
        if not uploaded or not hasattr(uploaded, "content_type"):
            return uploaded
        if uploaded.size > self.FILE_MAX_BYTES:
            raise forms.ValidationError("File maksimal 10 MB.")
        if image_only and uploaded.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise forms.ValidationError("Product Cover harus berupa JPG, PNG, atau WebP.")
        signatures = self.FILE_TYPES.get(uploaded.content_type, ())
        header = uploaded.read(12)
        uploaded.seek(0)
        if not signatures or not any(header.startswith(signature) for signature in signatures):
            raise forms.ValidationError("File harus berupa PDF, JPG, PNG, atau WebP yang valid.")
        if uploaded.content_type == "image/webp" and header[8:12] != b"WEBP":
            raise forms.ValidationError("File WebP tidak valid.")
        return uploaded

    def clean_mockup(self):
        return self._clean_file("mockup")

    def clean_technical_drawing(self):
        return self._clean_file("technical_drawing")

    def clean_product_cover(self):
        return self._clean_file("product_cover", image_only=True)


class DevelopmentProductMaterialForm(forms.ModelForm):
    requirement = forms.DecimalField(
        label="Kebutuhan",
        max_digits=12,
        decimal_places=1,
        min_value=Decimal("0.1"),
        widget=forms.NumberInput(
            attrs={"min": "0.1", "step": "0.1", "inputmode": "decimal", "placeholder": "0.0"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance.pk and self.instance.requirement is not None:
            self.initial["requirement"] = self.instance.requirement.quantize(Decimal("0.1"))

    class Meta:
        model = DevelopmentProductMaterial
        fields = ("material", "requirement", "eom")
        labels = {
            "material": "Material",
            "requirement": "Kebutuhan",
            "eom": "EOM / Satuan",
        }
        widgets = {
            "material": forms.TextInput(attrs={"placeholder": "Contoh: Canvas 12 oz"}),
            "eom": forms.TextInput(attrs={"placeholder": "Contoh: meter, pcs, gram"}),
        }


DevelopmentProductMaterialFormSet = inlineformset_factory(
    DevelopmentProduct,
    DevelopmentProductMaterial,
    form=DevelopmentProductMaterialForm,
    extra=1,
    can_delete=True,
)


class MarketingRecommendationForm(forms.ModelForm):
    class Meta:
        model = MarketingRecommendation
        fields = ("recommendation", "recommended_quantity", "rationale")
        widgets = {"rationale": forms.Textarea(attrs={"rows": 5})}

    def clean(self):
        data = super().clean()
        if data.get("recommendation") == MarketingRecommendation.Decision.GO:
            if not data.get("recommended_quantity"):
                self.add_error("recommended_quantity", "Quantity wajib diisi untuk rekomendasi GO.")
        else:
            data["recommended_quantity"] = None
        return data


class OfficialDecisionForm(forms.ModelForm):
    class Meta:
        model = MarketingRecommendation
        fields = ("official_decision", "official_quantity", "official_note")
        labels = {
            "official_decision": "Official Decision",
            "official_quantity": "Approved Quantity",
            "official_note": "Approval Note",
        }
        widgets = {"official_note": forms.Textarea(attrs={"rows": 5})}

    def clean(self):
        data = super().clean()
        if not data.get("official_decision"):
            self.add_error("official_decision", "Official Decision wajib dipilih.")
        elif data.get("official_decision") == MarketingRecommendation.Decision.GO:
            if not data.get("official_quantity"):
                self.add_error("official_quantity", "Approved Quantity wajib diisi untuk GO.")
        else:
            data["official_quantity"] = None
        return data
