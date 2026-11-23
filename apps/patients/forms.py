from datetime import date

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core import national_id
from apps.core.phones import to_e164

from . import custom_fields
from .models import Allergy, ChronicCondition, FieldScope, Patient


class PatientForm(forms.ModelForm):
    age_years = forms.IntegerField(
        label=_("السن (تقريبي)"),
        required=False,
        min_value=0,
        max_value=130,
        help_text=_("لو مش عارفة تاريخ الميلاد بالظبط، اكتبي السن بس."),
    )
    whatsapp_same_as_phone = forms.BooleanField(label=_("نفس رقم الموبايل"), required=False, initial=True)

    class Meta:
        model = Patient
        fields = [
            "full_name",
            "gender",
            "date_of_birth",
            "phone",
            "whatsapp",
            "email",
            "national_id",
            "husband_name",
            "guardian_name",
            "address",
            "occupation",
            "preferred_channel",
            "messaging_consent",
            "notes",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "phone": forms.TextInput(attrs={"dir": "ltr"}),
            "national_id": forms.TextInput(attrs={"dir": "ltr", "inputmode": "numeric", "autocomplete": "off"}),
            "whatsapp": forms.TextInput(attrs={"dir": "ltr"}),
            "email": forms.EmailInput(attrs={"dir": "ltr"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, org=None, check_org=None, **kwargs):
        super().__init__(*args, **kwargs)
        # the clinic a national ID must be unique in (quick-add forms pass it without the custom fields)
        self.check_org = check_org or org or (self.instance.organization if self.instance.pk else None)
        self.fields["gender"].required = False  # the national ID says it; clean() asks when nothing does
        # Clinic-defined extra fields (scope=patient); quick-add forms built without `org` skip them.
        self.custom_defs = custom_fields.definitions(org, FieldScope.PATIENT)
        custom_fields.add_to_form(self, self.custom_defs, self.instance.custom_fields)
        self.fields["whatsapp"].required = False
        if self.instance.pk and self.instance.whatsapp == self.instance.phone:
            self.fields["whatsapp_same_as_phone"].initial = True

    def clean_phone(self):
        return to_e164(self.cleaned_data["phone"])

    def clean_national_id(self):
        raw = self.cleaned_data.get("national_id", "")
        if not national_id.clean(raw):
            self._nid = None
            return ""
        try:
            self._nid = national_id.parse(raw)
        except national_id.InvalidNationalId as e:
            raise ValidationError(str(e)) from e
        if self.check_org is not None:
            other = (
                Patient.objects.filter(organization=self.check_org, national_id=self._nid.number, is_active=True)
                .exclude(pk=self.instance.pk)
                .first()
            )
            if other is not None:
                taken = {"name": other.full_name, "n": other.file_number}
                raise ValidationError(_("الرقم القومي ده مسجل لملف تاني: %(name)s #%(n)s") % taken)
        return self._nid.number

    def clean_whatsapp(self):
        value = self.cleaned_data.get("whatsapp")
        return to_e164(value) if value else ""

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("whatsapp_same_as_phone"):
            cleaned["whatsapp"] = cleaned.get("phone", "")
        nid = getattr(self, "_nid", None)
        if nid is not None:  # the national ID fills what reception did not type, and must agree with what it did
            if not cleaned.get("gender"):
                cleaned["gender"] = nid.gender
            elif cleaned["gender"] != nid.gender:
                self.add_error("national_id", _("النوع مش مطابق للرقم القومي."))
            if not cleaned.get("date_of_birth"):
                cleaned["date_of_birth"] = nid.birth_date
                cleaned["age_years"] = None
            elif cleaned["date_of_birth"] != nid.birth_date:
                self.add_error("national_id", _("تاريخ الميلاد مش مطابق للرقم القومي."))
        if not cleaned.get("gender") and "gender" not in self.errors:
            self.add_error("gender", _("اختار النوع (أو اكتب الرقم القومي)."))
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        age = self.cleaned_data.get("age_years")
        if age is not None and not self.cleaned_data.get("date_of_birth"):
            today = date.today()
            try:
                instance.date_of_birth = today.replace(year=today.year - age)
            except ValueError:  # today is Feb 29 and the target year has no such day
                instance.date_of_birth = today.replace(month=2, day=28, year=today.year - age)
            instance.dob_is_estimated = True
        elif self.cleaned_data.get("date_of_birth"):
            instance.dob_is_estimated = False
        if self.custom_defs:
            instance.custom_fields = custom_fields.collect(self, self.custom_defs, instance.custom_fields)
        if commit:
            instance.save()
        return instance

    @property
    def custom_bound_fields(self):
        return custom_fields.bound_fields(self, self.custom_defs)


class AllergyForm(forms.ModelForm):
    class Meta:
        model = Allergy
        fields = ["name", "severity", "notes"]


class ChronicConditionForm(forms.ModelForm):
    class Meta:
        model = ChronicCondition
        fields = ["name", "notes"]


class MergeForm(forms.Form):
    duplicate_id = forms.IntegerField(widget=forms.HiddenInput)
