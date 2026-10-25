import re

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import ClinicRole, Role

from .models import USERNAME_RE, User


class EmailAuthenticationForm(AuthenticationForm):
    """Username or email + password (accounts.backends.UsernameOrEmailBackend)."""

    username = forms.CharField(
        label=_("اسم المستخدم أو الإيميل"),
        max_length=254,
        widget=forms.TextInput(
            attrs={"autofocus": True, "autocomplete": "username", "autocapitalize": "none", "dir": "ltr"}
        ),
    )
    password = forms.CharField(
        label=_("كلمة المرور"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "dir": "ltr"}),
    )

    error_messages = {
        "invalid_login": _("اسم المستخدم/الإيميل أو كلمة المرور غلط."),
        "inactive": _("الحساب ده متوقف."),
    }

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


def _password_field(label, required=True):
    return forms.CharField(
        label=label,
        strip=False,
        required=required,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "dir": "ltr"}),
    )


class UserDetailsForm(forms.Form):
    """Name, username, optional email and password — shared by "add user" and "edit user". On edit (`user` given) the
    password is optional: empty = unchanged."""

    full_name = forms.CharField(label=_("الاسم"), max_length=150)
    username = forms.CharField(
        label=_("اسم المستخدم"),
        max_length=150,
        help_text=_("بيدخل بيه بدل الإيميل — حروف وأرقام و . _ - من غير مسافات (مثلاً: mona أو mona.ali)."),
        widget=forms.TextInput(attrs={"dir": "ltr", "autocomplete": "off", "autocapitalize": "none"}),
    )
    email = forms.EmailField(
        label=_("الإيميل (اختياري)"),
        required=False,
        help_text=_("لو اتكتب: يقدر يدخل بيه كمان، ويرجّع كلمة السر لو نسيها."),
        widget=forms.EmailInput(attrs={"dir": "ltr", "autocomplete": "off"}),
    )
    password1 = _password_field(_("كلمة السر"))
    password2 = _password_field(_("تأكيد كلمة السر"))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["password1"].help_text = _("%(n)s حروف أو أرقام على الأقل.") % {"n": settings.PASSWORD_MIN_LENGTH}
        if user is not None:
            self.fields["password1"] = _password_field(_("كلمة سر جديدة (سيبها فاضية لو مش هتغيّرها)"), required=False)
            self.fields["password2"] = _password_field(_("تأكيد كلمة السر الجديدة"), required=False)
            self.initial.setdefault("full_name", user.full_name)
            self.initial.setdefault("username", user.username)
            self.initial.setdefault("email", user.email or "")

    def _others(self):
        return User.objects.exclude(pk=self.user.pk) if self.user is not None else User.objects.all()

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        unchanged = self.user is not None and username == self.user.username  # older accounts: the email as username
        if not unchanged and not re.match(USERNAME_RE, username):
            raise forms.ValidationError(_("حروف وأرقام و . _ - بس، من غير مسافات ولا @."))
        if self._others().filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
            raise forms.ValidationError(_("اسم المستخدم ده مستخدم قبل كده — اختار اسم تاني."))
        return username

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and self._others().filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
            raise forms.ValidationError(_("الإيميل ده مستخدم لحساب تاني."))
        return email or None

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if (p1 or p2) and p1 != p2:
            self.add_error("password2", _("كلمتين السر مش زي بعض."))
        if p1:
            candidate = self.user or User()
            candidate.full_name = cleaned.get("full_name", "")
            candidate.username = cleaned.get("username") or ""
            candidate.email = cleaned.get("email")
            try:
                password_validation.validate_password(p1, candidate)
            except forms.ValidationError as e:
                self.add_error("password1", e)
        return cleaned


class UserCreateForm(UserDetailsForm):
    role = forms.ChoiceField(label=_("الدور"), choices=())

    def __init__(self, *args, organization, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = choices  # roles.choices_for(): only what the editor may give
        # default: the clinic's reception role if offered, else the first role (never "owner" by default)
        reception = ClinicRole.objects.filter(organization=organization, key=Role.RECEPTION).first()
        offered = [v for v, _l in choices]
        if reception and str(reception.pk) in offered:
            self.fields["role"].initial = str(reception.pk)
        elif offered:
            self.fields["role"].initial = offered[-1] if offered[0] == "owner" else offered[0]


class RoleForm(forms.Form):
    role = forms.ChoiceField(label=_("الدور"), choices=())

    def __init__(self, *args, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = choices


class ClinicRoleForm(forms.ModelForm):
    class Meta:
        model = ClinicRole
        fields = ["name", "description"]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        taken = ClinicRole.objects.filter(organization=self.instance.organization, name__iexact=name)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError(_("فيه دور بالاسم ده بالفعل."))
        if name == str(Role.OWNER.label):
            raise forms.ValidationError(_("الاسم ده محجوز للمالك."))
        return name
