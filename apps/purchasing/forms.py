from django import forms
from django.utils.translation import gettext_lazy as _

from .models import PurchaseSettings, Supplier, TaxCode, TaxKind


class SupplierForm(forms.ModelForm):
    opening_balance = forms.DecimalField(
        label=_("رصيد افتتاحي (علينا له)"), required=False, max_digits=14, decimal_places=2,
        help_text=_("اللي كان علينا للمورد قبل ما نبدأ على البرنامج. بيتسجل مرة واحدة عند الإضافة."),
    )  # fmt: skip

    class Meta:
        model = Supplier
        fields = [
            "name", "phone", "tax_registration_number", "tax_file_number", "commercial_register", "address",
            "payment_terms_days", "default_withholding", "notes", "is_active",
        ]  # fmt: skip

    def __init__(self, *args, org, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_withholding"].queryset = TaxCode.objects.for_org(org).filter(
            kind=TaxKind.WITHHOLDING, is_active=True
        )
        self.fields["default_withholding"].empty_label = _("من غير خصم")
        for name in ("phone", "tax_registration_number", "tax_file_number", "commercial_register", "opening_balance"):
            self.fields[name].widget.attrs.update({"dir": "ltr"})
        if self.instance.pk:
            del self.fields["opening_balance"]

    def validate_unique(self):
        pass  # the name is checked per clinic by the service (the form has no organization)


class TaxCodeForm(forms.ModelForm):
    class Meta:
        model = TaxCode
        fields = ["name", "kind", "rate", "recoverable", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rate"].widget.attrs.update({"dir": "ltr", "inputmode": "decimal"})
        if self.instance.pk:
            self.fields["kind"].disabled = True  # a tax that was used keeps its kind

    def validate_unique(self):
        pass


class PurchaseSettingsForm(forms.ModelForm):
    class Meta:
        model = PurchaseSettings
        fields = ["wht_threshold", "cheque_alert_days"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"dir": "ltr", "inputmode": "decimal"})
        self.fields["wht_threshold"].help_text = _("سداد قيمته الخاضعة أقل من أو تساوي المبلغ ده مش بيتخصم منه.")
