from datetime import datetime

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.timeutils import CAIRO
from apps.doctors.models import Doctor
from apps.obstetrics.models import BabySex

from .models import THEATRE_KINDS, NewbornStatus, ProcedureType, Room, RoomKind, TeamRole


def _aware(day, clock):
    return datetime.combine(day, clock, tzinfo=CAIRO)


class OperationForm(forms.Form):
    """Booking (or moving) an operation: what, where, when, who."""

    procedure = forms.ModelChoiceField(
        label=_("العملية"), queryset=ProcedureType.objects.none(), empty_label=_("اختار العملية")
    )
    room = forms.ModelChoiceField(label=_("الغرفة"), queryset=Room.objects.none(), empty_label=_("اختار الغرفة"))
    surgeon = forms.ModelChoiceField(label=_("الجراح"), queryset=Doctor.objects.none(), empty_label=_("اختار الدكتور"))
    date = forms.DateField(label=_("اليوم"), widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(label=_("الساعة"), widget=forms.TimeInput(attrs={"type": "time"}))
    duration_minutes = forms.IntegerField(
        label=_("المدة (دقيقة)"), min_value=10, max_value=720, required=False,
        widget=forms.NumberInput(attrs={"dir": "ltr", "inputmode": "numeric"}),
    )  # fmt: skip
    is_emergency = forms.BooleanField(label=_("عملية طارئة"), required=False)
    notes = forms.CharField(label=_("ملاحظات"), max_length=300, required=False)

    def __init__(self, *args, org, moving=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["procedure"].queryset = ProcedureType.objects.for_org(org).filter(is_active=True)
        self.fields["room"].queryset = Room.objects.for_org(org).filter(is_active=True, kind__in=THEATRE_KINDS)
        self.fields["surgeon"].queryset = Doctor.objects.for_org(org).filter(is_active=True)
        if moving:  # only the time, the room and the length change
            for name in ("procedure", "surgeon", "is_emergency", "notes"):
                del self.fields[name]

    def start(self):
        return _aware(self.cleaned_data["date"], self.cleaned_data["time"])


class NewbornForm(forms.Form):
    sex = forms.ChoiceField(label=_("النوع"), choices=BabySex.choices)
    weight_g = forms.IntegerField(label=_("الوزن (جرام)"), min_value=200, max_value=7000, required=False)
    apgar_1 = forms.IntegerField(label=_("أبجار 1"), min_value=0, max_value=10, required=False)
    apgar_5 = forms.IntegerField(label=_("أبجار 5"), min_value=0, max_value=10, required=False)
    born_time = forms.TimeField(label=_("ساعة الولادة"), required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    status = forms.ChoiceField(label=_("الحالة"), choices=NewbornStatus.choices, initial=NewbornStatus.WELL)
    notes = forms.CharField(label=_("ملاحظات"), max_length=300, required=False)


class TeamMemberForm(forms.Form):
    role = forms.ChoiceField(label=_("الدور"), choices=TeamRole.choices)
    doctor = forms.ModelChoiceField(label=_("دكتور من المركز"), queryset=Doctor.objects.none(), required=False,
                                    empty_label=_("من بره المركز"))  # fmt: skip
    name = forms.CharField(label=_("الاسم"), max_length=120, required=False)
    fee = forms.DecimalField(label=_("الأتعاب"), min_value=0, max_digits=10, decimal_places=2, required=False)

    def __init__(self, *args, org, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doctor"].queryset = Doctor.objects.for_org(org).filter(is_active=True)

    def clean(self):
        data = super().clean()
        if not data.get("doctor") and not (data.get("name") or "").strip():
            raise forms.ValidationError(_("اختار دكتور من المركز أو اكتب الاسم."))
        return data


class AdmitForm(forms.Form):
    room = forms.ModelChoiceField(label=_("الغرفة"), queryset=Room.objects.none(), empty_label=_("اختار الغرفة"))
    reason = forms.CharField(label=_("سبب الدخول"), max_length=200, required=False)

    def __init__(self, *args, org, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].queryset = Room.objects.for_org(org).filter(
            is_active=True, kind__in=[RoomKind.INPATIENT, RoomKind.RECOVERY]
        )


class DischargeForm(forms.Form):
    summary = forms.CharField(label=_("ملخص الخروج"), required=False, widget=forms.Textarea(attrs={"rows": 4}))
    instructions = forms.CharField(label=_("تعليمات للمريضة"), required=False, widget=forms.Textarea(attrs={"rows": 3}))


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "kind", "beds", "nightly_rate", "is_active"]

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.org = org

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Room.objects.filter(organization=self.org, name=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(_("فيه غرفة بالاسم ده."))
        return name


class ProcedureForm(forms.ModelForm):
    checklist_text = forms.CharField(
        label=_("قايمة ما قبل العملية"), required=False, widget=forms.Textarea(attrs={"rows": 6}),
        help_text=_("سطر لكل بند. حط * في أول السطر لو البند مش إجباري."),
    )  # fmt: skip

    kit_text = forms.CharField(
        label=_("المستلزمات المعتادة من المخزن"), required=False, widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("سطر لكل صنف: اسم الصنف زي ما هو في المخزن، وبعده = والكمية. مثال: شاش معقم = 5"),
    )  # fmt: skip

    class Meta:
        model = ProcedureType
        fields = ["name_ar", "name_en", "delivery", "duration_minutes", "package_price", "fasting_hours", "is_active"]
        widgets = {"name_en": forms.TextInput(attrs={"dir": "ltr"})}

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.org = org
        self.fields["delivery"].required = False
        self.fields["delivery"].choices = [("", _("مش ولادة")), *self.fields["delivery"].choices[1:]]
        items = self.instance.checklist or []
        self.fields["checklist_text"].initial = "\n".join(
            ("" if item.get("required", True) else "* ") + item.get("label", "") for item in items
        )
        if self.instance.pk:
            from .services import kit_rows

            self.fields["kit_text"].initial = "\n".join(
                f"{item.name} = {quantity.normalize():f}" for item, quantity in kit_rows(self.instance)
            )

    def clean_kit_text(self):
        from decimal import Decimal, InvalidOperation

        from apps.inventory.models import StockItem

        rows = []
        for line in (self.cleaned_data.get("kit_text") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            name, _sep, raw = line.rpartition("=")
            name, raw = (name.strip(), raw.strip()) if _sep else (line, "1")
            item = StockItem.objects.filter(organization=self.org, name__iexact=name, is_active=True).first()
            if item is None:
                raise forms.ValidationError(_("مفيش صنف في المخزن اسمه «%(n)s».") % {"n": name})
            try:
                quantity = Decimal(raw)
            except InvalidOperation as e:
                raise forms.ValidationError(_("كمية «%(n)s» لازم تكون رقم.") % {"n": name}) from e
            if quantity <= 0:
                raise forms.ValidationError(_("كمية «%(n)s» لازم تكون أكبر من صفر.") % {"n": name})
            rows.append({"item": item.pk, "quantity": str(quantity)})
        return rows

    def clean_name_ar(self):
        name = " ".join(self.cleaned_data["name_ar"].split())
        same = ProcedureType.objects.filter(organization=self.org, name_ar=name).exclude(pk=self.instance.pk)
        if same.exists():
            raise forms.ValidationError(_("فيه عملية بالاسم ده."))
        return name

    def save(self, commit=True):
        items = []
        for line in self.cleaned_data.get("checklist_text", "").splitlines():
            line = line.strip()
            if line:
                optional = line.startswith("*")
                items.append({"label": line.lstrip("* ").strip()[:150], "required": not optional})
        self.instance.checklist = items
        self.instance.kit = self.cleaned_data.get("kit_text") or []
        return super().save(commit)
