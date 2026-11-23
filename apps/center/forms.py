from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.phones import to_e164
from apps.patients.models import Gender, Patient

from .models import CenterSettings, Disposition, EmergencyReason, Service, Triage


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ["name_ar", "name_en", "category", "price", "doctor_share_percent", "is_active"]
        widgets = {
            "name_en": forms.TextInput(attrs={"dir": "ltr"}),
            "price": forms.NumberInput(attrs={"dir": "ltr", "step": "0.01", "min": "0", "inputmode": "decimal"}),
            "doctor_share_percent": forms.NumberInput(
                attrs={"dir": "ltr", "step": "0.01", "min": "0", "max": "100", "inputmode": "decimal"}
            ),
        }
        help_texts = {
            "doctor_share_percent": _("اللي بياخده الدكتور اللي عمل الخدمة من تمنها. 0 = مفيش."),
        }

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.org = org

    def clean_name_ar(self):
        name = " ".join(self.cleaned_data["name_ar"].split())
        same = Service.objects.filter(organization=self.org, name_ar=name).exclude(pk=self.instance.pk)
        if same.exists():
            raise ValidationError(_("فيه خدمة بالاسم ده."))
        return name


class CenterSettingsForm(forms.ModelForm):
    class Meta:
        model = CenterSettings
        fields = ["emergency_fee", "lobby_show_names", "lobby_sound"]
        widgets = {
            "emergency_fee": forms.NumberInput(attrs={"dir": "ltr", "step": "0.01", "min": "0", "inputmode": "decimal"})
        }
        help_texts = {"emergency_fee": _("بيتحسب على كل حالة طوارئ بتتسجل. 0 = من غير رسوم.")}


class EmergencyForm(forms.Form):
    """Fast registration: an existing patient (picked from the search), or just a name and a phone number."""

    patient = forms.IntegerField(required=False, widget=forms.HiddenInput)
    full_name = forms.CharField(label=_("الاسم"), max_length=150, required=False)
    phone = forms.CharField(label=_("رقم الموبايل"), max_length=20, required=False)
    gender = forms.ChoiceField(label=_("النوع"), choices=Gender.choices, required=False)
    triage = forms.ChoiceField(label=_("درجة الخطورة"), choices=Triage.choices, initial=Triage.URGENT)
    reason = forms.ChoiceField(label=_("السبب"), choices=EmergencyReason.choices, initial=EmergencyReason.LABOUR)
    reason_text = forms.CharField(label=_("تفاصيل (اختياري)"), max_length=200, required=False)
    doctor = forms.IntegerField(required=False)

    def __init__(self, *args, org=None, doctors=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.org, self.doctors = org, list(doctors)
        self.patient_obj = None

    def clean(self):
        data = super().clean()
        pk = data.get("patient")
        if pk:
            self.patient_obj = Patient.objects.for_org(self.org).filter(pk=pk, is_active=True).first()
            if self.patient_obj is None:
                raise ValidationError(_("المريض ده مش موجود."))
        else:
            if not (data.get("full_name") or "").strip():
                self.add_error("full_name", _("اكتب الاسم، أو اختار مريضة من البحث."))
            if not data.get("phone"):
                self.add_error("phone", _("اكتب رقم موبايل."))
            else:
                try:
                    data["phone"] = to_e164(data["phone"])
                except ValidationError as e:
                    self.add_error("phone", e)
            if not data.get("gender"):
                self.add_error("gender", _("اختار النوع."))
        by_pk = {d.pk: d for d in self.doctors}
        data["doctor_obj"] = by_pk.get(data.get("doctor")) or (self.doctors[0] if len(self.doctors) == 1 else None)
        if data["doctor_obj"] is None:
            self.add_error("doctor", _("اختار الدكتور."))
        return data


class DispositionForm(forms.Form):
    disposition = forms.ChoiceField(label=_("التصرف"), choices=Disposition.choices)
    notes = forms.CharField(label=_("ملاحظات"), max_length=300, required=False)
