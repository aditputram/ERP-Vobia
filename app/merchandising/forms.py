from decimal import Decimal

from django import forms
from django.db import models
from django.utils import timezone

from master_data.models import Category, Product, ProductStatus, Subcategory

from .models import ProjectionRule, ProjectionScenario
from .services.builder import drafted_product_ids
from .services.planning_activity import (
    filter_products_by_planning_activity,
    planning_activity_snapshot,
)


class ProjectionScenarioForm(forms.ModelForm):
    start_month = forms.DateField(
        label="Start month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    end_month = forms.DateField(
        label="End month",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )

    class Meta:
        model = ProjectionScenario
        fields = ("name", "start_month", "end_month")

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name")
        start_month = cleaned.get("start_month")
        end_month = cleaned.get("end_month")
        if name and start_month and end_month:
            duplicate = ProjectionScenario.objects.filter(
                name__iexact=name.strip(),
                start_month=start_month,
                end_month=end_month,
            )
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise forms.ValidationError("Scenario dengan nama dan periode yang sama sudah ada.")
        return cleaned


class ProjectionBuilderForm(forms.Form):
    class PlanningActivity(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Nonaktif"
        ALL = "ALL", "Semua Produk"

    scenario = forms.ModelChoiceField(queryset=ProjectionScenario.objects.all())
    target_month = forms.DateField(
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    scope_type = forms.ChoiceField(
        choices=ProjectionRule.ScopeType.choices,
        initial=ProjectionRule.ScopeType.ALL_PRODUCTS,
        widget=forms.HiddenInput(),
    )
    product_status = forms.ModelChoiceField(
        queryset=ProductStatus.objects.all(), required=False, empty_label="All statuses"
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(), required=False, empty_label="All categories"
    )
    subcategory = forms.ModelChoiceField(
        label="Sub Category",
        queryset=Subcategory.objects.all(),
        required=False,
        empty_label="All sub categories",
    )
    planning_activity = forms.ChoiceField(
        label="Planning activity",
        choices=PlanningActivity.choices,
        initial=PlanningActivity.ACTIVE,
        required=False,
    )
    product = forms.ModelMultipleChoiceField(
        queryset=Product.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "builder-product-source"}),
    )
    method = forms.ChoiceField(choices=ProjectionRule.Method.choices)
    parameter = forms.DecimalField(
        min_value=0,
        decimal_places=4,
        required=False,
        help_text="Persentase, target Stock Ratio, atau jumlah bulan.",
    )
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        cleaned = super().clean()
        product_status = cleaned.get("product_status")
        category = cleaned.get("category")
        subcategory = cleaned.get("subcategory")
        selected_products = cleaned.get("product")
        planning_activity = cleaned.get("planning_activity") or self.PlanningActivity.ACTIVE
        method = cleaned.get("method")
        parameter = cleaned.get("parameter")
        if method == ProjectionRule.Method.SAME_AS_LAST_MONTH:
            cleaned["parameter"] = Decimal("0")
        elif parameter is None:
            self.add_error("parameter", "Parameter wajib untuk metode ini.")
        elif method == ProjectionRule.Method.TARGET_STOCK_RATIO and parameter <= 0:
            self.add_error("parameter", "Target Stock Ratio harus lebih besar dari nol.")
        elif method == ProjectionRule.Method.SELL_OUT_ENDING_MONTHS and (
            parameter <= 0 or parameter != parameter.to_integral_value()
        ):
            self.add_error("parameter", "Jumlah bulan harus bilangan bulat lebih dari nol.")
        activity_products = filter_products_by_planning_activity(
            Product.objects.filter(is_active=True),
            planning_activity,
            planning_activity_snapshot(target_month=cleaned.get("target_month")),
        )
        claimed_product_ids = drafted_product_ids(cleaned.get("target_month"))
        available_products = activity_products.exclude(id__in=claimed_product_ids)

        if subcategory and category and subcategory.category_id != category.id:
            self.add_error("subcategory", "Sub Category harus berada di dalam Category yang dipilih.")

        # Status, Category, Sub Category, and Product are cascading filters. "All products"
        # means every active product inside the selected filter intersection,
        # not every product in the selected status alone.
        if selected_products:
            if selected_products.filter(id__in=claimed_product_ids).exists():
                raise forms.ValidationError(
                    "Product yang dipilih sudah memiliki Draft Projection untuk Target Month ini. "
                    "Buka melalui View Draft atau pilih Product lain."
                )
            valid_products = selected_products.filter(id__in=available_products.values("id"))
            if product_status:
                valid_products = valid_products.filter(status=product_status)
            if category:
                valid_products = valid_products.filter(category=category)
            if subcategory:
                valid_products = valid_products.filter(subcategory=subcategory)
            if valid_products.count() != selected_products.count():
                raise forms.ValidationError(
                    "Product harus sesuai dengan Product Status, Category, dan Sub Category yang dipilih."
                )
            cleaned["scope_type"] = ProjectionRule.ScopeType.PRODUCT
            cleaned["product"] = valid_products
        elif subcategory or (product_status and category):
            matching_products = available_products
            if product_status:
                matching_products = matching_products.filter(status=product_status)
            if category:
                matching_products = matching_products.filter(category=category)
            if subcategory:
                matching_products = matching_products.filter(subcategory=subcategory)
            if not matching_products.exists():
                raise forms.ValidationError(
                    "Tidak ada Product aktif yang sesuai dengan Product Status, Category, dan Sub Category tersebut."
                )
            cleaned["scope_type"] = ProjectionRule.ScopeType.PRODUCT
            cleaned["product"] = matching_products
        elif planning_activity != self.PlanningActivity.ALL:
            matching_products = available_products
            if product_status:
                matching_products = matching_products.filter(status=product_status)
            if category:
                matching_products = matching_products.filter(category=category)
            if subcategory:
                matching_products = matching_products.filter(subcategory=subcategory)
            if not matching_products.exists():
                raise forms.ValidationError("Tidak ada Product dengan Planning Activity dan filter tersebut.")
            cleaned["scope_type"] = ProjectionRule.ScopeType.PRODUCT
            cleaned["product"] = matching_products
        elif category:
            cleaned["scope_type"] = ProjectionRule.ScopeType.CATEGORY
            cleaned["product_status"] = None
            cleaned["product"] = Product.objects.none()
        elif product_status:
            cleaned["scope_type"] = ProjectionRule.ScopeType.PRODUCT_STATUS
            cleaned["category"] = None
            cleaned["product"] = Product.objects.none()
        else:
            cleaned["scope_type"] = ProjectionRule.ScopeType.ALL_PRODUCTS
            cleaned["product"] = Product.objects.none()
        return cleaned


class IncomingMonthCloseForm(forms.Form):
    month = forms.DateField(input_formats=["%Y-%m"], widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    evidence_reference = forms.CharField(max_length=240, help_text="Contoh: warehouse stock opname + inbound reconciliation August 2026")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_month(self):
        value = self.cleaned_data["month"]
        return value.replace(day=1)
