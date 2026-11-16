from django import forms
from django.utils.translation import gettext_lazy as _

from .models import InvoiceSettings, Payer


class PayerForm(forms.ModelForm):
    class Meta:
        model = Payer
        fields = ["name", "kind", "default_percent", "phone", "contact_name", "notes", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_percent"].widget.attrs.update({"dir": "ltr", "inputmode": "decimal"})
        self.fields["phone"].widget.attrs.update({"dir": "ltr", "inputmode": "tel"})
        self.fields["default_percent"].help_text = _(
            "النسبة اللي بتتحط لأي مريضة تتسجل على الجهة دي، وتتغير لكل مريضة."
        )

    def validate_unique(self):
        pass  # the name is checked per clinic by the service (the form has no organization)


class InvoiceSettingsForm(forms.ModelForm):
    class Meta:
        model = InvoiceSettings
        fields = ["discount_limit_percent", "tax_registration_number", "commercial_register", "invoice_footer"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("discount_limit_percent", "tax_registration_number", "commercial_register"):
            self.fields[name].widget.attrs.update({"dir": "ltr"})
        self.fields["discount_limit_percent"].help_text = _(
            "خصم أكبر من النسبة دي محتاج حد معاه صلاحية «خصم من غير حد» (صاحب العيادة)."
        )
        self.fields["tax_registration_number"].help_text = _("بيتطبع على الفاتورة لو مكتوب.")
