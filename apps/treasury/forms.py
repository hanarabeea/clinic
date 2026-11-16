from django import forms
from django.utils.translation import gettext_lazy as _

from apps.billing.models import PaymentMethod
from apps.ledger.chart import CONTROL_KEYS
from apps.ledger.models import Account, CostCenter

from .models import ExpenseTemplate, Treasury, TreasuryKind, VoucherKind
from .services import DENOMINATIONS


class VoucherForm(forms.Form):
    """One form for the three kinds; which fields count depends on `kind` (the service checks the rest)."""

    kind = forms.ChoiceField(choices=VoucherKind.choices)
    treasury = forms.ModelChoiceField(label=_("الخزينة"), queryset=Treasury.objects.none())
    amount = forms.DecimalField(label=_("المبلغ"), min_value=0.01, max_digits=14, decimal_places=2)
    date = forms.DateField(label=_("التاريخ"), widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(label=_("البيان"), max_length=300)
    counter_account = forms.ModelChoiceField(label=_("الحساب"), queryset=Account.objects.none(), required=False)
    to_treasury = forms.ModelChoiceField(label=_("إلى خزينة"), queryset=Treasury.objects.none(), required=False)
    cost_center = forms.ModelChoiceField(label=_("مركز التكلفة"), queryset=CostCenter.objects.none(), required=False)
    party_name = forms.CharField(label=_("الطرف (اختياري)"), max_length=150, required=False)

    def __init__(self, *args, org, **kwargs):
        super().__init__(*args, **kwargs)
        treasuries = Treasury.objects.for_org(org).filter(is_active=True)
        self.fields["treasury"].queryset = treasuries
        self.fields["to_treasury"].queryset = treasuries
        treasury_accounts = treasuries.values("account_id")
        self.fields["counter_account"].queryset = (
            Account.objects.for_org(org)
            .filter(is_group=False, is_active=True)
            .exclude(pk__in=treasury_accounts)
            .exclude(system_key__in=CONTROL_KEYS)  # those move through their own documents only
        )
        self.fields["cost_center"].queryset = CostCenter.objects.for_org(org).filter(is_active=True)
        self.fields["treasury"].empty_label = _("اختار الخزينة")
        self.fields["to_treasury"].empty_label = _("اختار الخزينة")
        self.fields["counter_account"].empty_label = _("اختار الحساب")
        self.fields["cost_center"].empty_label = _("من غير")
        self.fields["cost_center"].help_text = _("مطلوب لحسابات المصروفات والإيرادات.")
        self.fields["amount"].widget.attrs.update({"dir": "ltr", "inputmode": "decimal", "step": "0.01"})


class CountForm(forms.Form):
    """How many of each note and coin are in the drawer."""

    witness_name = forms.CharField(label=_("الشاهد (اختياري)"), max_length=120, required=False)
    notes = forms.CharField(label=_("ملاحظات"), max_length=300, required=False)
    keep = forms.DecimalField(label=_("يفضل في الدرج للوردية الجاية"), min_value=0, max_digits=14, decimal_places=2,
                              required=False)  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for face in DENOMINATIONS:
            self.fields[f"d_{face}"] = forms.IntegerField(
                label=face, min_value=0, max_value=100000, required=False,
                widget=forms.NumberInput(
                    attrs={"dir": "ltr", "inputmode": "numeric", "min": "0", "data-face": face,
                           "class": "input py-1.5 text-center"}
                ),
            )  # fmt: skip
        self.fields["keep"].widget.attrs.update({"dir": "ltr", "inputmode": "decimal", "step": "0.01"})

    def face_fields(self):
        return [self[f"d_{face}"] for face in DENOMINATIONS]

    def denominations(self) -> dict:
        return {face: self.cleaned_data.get(f"d_{face}") or 0 for face in DENOMINATIONS}


class TreasuryForm(forms.ModelForm):
    class Meta:
        model = Treasury
        fields = ["name", "kind", "receives_method", "is_active"]

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.org = org
        self.fields["receives_method"].required = False
        self.fields["receives_method"].choices = [("", _("ولا حاجة")), *PaymentMethod.choices]
        self.fields["receives_method"].help_text = _("الفلوس اللي الاستقبال بياخدها بالطريقة دي بتتسجل هنا.")
        if self.instance.pk:
            self.fields["kind"].disabled = True  # its account sits under that kind's group

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        same = Treasury.objects.filter(organization=self.org, name=name).exclude(pk=self.instance.pk)
        if same.exists():
            raise forms.ValidationError(_("فيه خزينة بالاسم ده."))
        return name

    def clean_receives_method(self):
        method = self.cleaned_data.get("receives_method") or ""
        if method:
            taken = Treasury.objects.filter(organization=self.org, receives_method=method).exclude(pk=self.instance.pk)
            if taken.exists():
                raise forms.ValidationError(
                    _("%(t)s هي اللي بتستقبل الطريقة دي. شيلها من هناك الأول.") % {"t": taken.first().name}
                )
        return method


# the group of the chart a new treasury's account goes under
GROUP_CODE = {
    TreasuryKind.DRAWER: "1101",
    TreasuryKind.SAFE: "1101",
    TreasuryKind.BANK: "1102",
    TreasuryKind.ELECTRONIC: "1103",
}


class ExpenseTemplateForm(forms.ModelForm):
    class Meta:
        model = ExpenseTemplate
        fields = ["name", "amount", "treasury", "counter_account", "cost_center", "day_of_month", "party_name",
                  "is_active"]  # fmt: skip

    def __init__(self, *args, org, **kwargs):
        super().__init__(*args, **kwargs)
        treasuries = Treasury.objects.for_org(org).filter(is_active=True)
        self.fields["treasury"].queryset = treasuries
        self.fields["counter_account"].queryset = (
            Account.objects.for_org(org)
            .filter(is_group=False, is_active=True)
            .exclude(pk__in=treasuries.values("account_id"))
            .exclude(system_key__in=CONTROL_KEYS)
        )
        self.fields["cost_center"].queryset = CostCenter.objects.for_org(org).filter(is_active=True)
        self.fields["cost_center"].help_text = _("مطلوب لحسابات المصروفات.")
        for name in ("amount", "day_of_month"):
            self.fields[name].widget.attrs.update({"dir": "ltr", "inputmode": "decimal"})

    def validate_unique(self):
        pass  # the name is checked per clinic by the view (the form has no organization)
