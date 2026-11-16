from django import forms
from django.utils.translation import gettext_lazy as _

from .models import InventorySettings, StockItem, Warehouse


class StockItemForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = ["name", "unit", "category", "reorder_level", "sale_price", "track_expiry", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("reorder_level", "sale_price"):
            self.fields[name].widget.attrs.update({"dir": "ltr", "inputmode": "decimal"})
        self.fields["reorder_level"].help_text = _("لما الرصيد يوصل للكمية دي بيظهر تنبيه. صفر = من غير تنبيه.")
        self.fields["sale_price"].help_text = _(
            "لو بيتحسب على المريضة لما يتصرف في عمليتها. صفر = داخل في سعر العملية."
        )

    def validate_unique(self):
        pass  # the name is checked per clinic by the view (the form has no organization)


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ["name", "is_active"]

    def validate_unique(self):
        pass


class InventorySettingsForm(forms.ModelForm):
    class Meta:
        model = InventorySettings
        fields = ["expiry_alert_days"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["expiry_alert_days"].widget.attrs.update({"dir": "ltr", "inputmode": "numeric"})
