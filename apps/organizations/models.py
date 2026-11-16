from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class Organization(BaseModel):
    name_ar = models.CharField(_("الاسم بالعربي"), max_length=200)
    name_en = models.CharField(_("الاسم بالإنجليزي"), max_length=200, blank=True)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("منظمة")
        verbose_name_plural = _("المنظمات")

    def __str__(self):
        return self.name_ar


def logo_upload_to(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return f"org_{instance.organization_id}/branding/logo.{ext}"


class Specialty(models.TextChoices):
    GENERAL = "general", _("عام (كل التخصصات)")
    OBGYN = "obgyn", _("نسا وتوليد")


class OrganizationSettings(BaseModel):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="settings")
    # Switches on what only that specialty needs: "obgyn" = the pregnancy file (apps/obstetrics).
    specialty = models.CharField(_("تخصص العيادة"), max_length=10, choices=Specialty.choices, default=Specialty.GENERAL)
    clinic_name_ar = models.CharField(_("اسم العيادة بالعربي"), max_length=200, blank=True)
    clinic_name_en = models.CharField(_("اسم العيادة بالإنجليزي"), max_length=200, blank=True)
    logo = models.ImageField(_("اللوجو"), upload_to=logo_upload_to, blank=True)
    primary_color = models.CharField(_("اللون الأساسي"), max_length=7, default="#A21E48")
    dark_color = models.CharField(_("اللون الغامق"), max_length=7, default="#24131E")
    neutral_color = models.CharField(_("اللون المحايد"), max_length=7, default="#F4F2F5")
    phones = models.JSONField(_("أرقام التليفون"), default=list, blank=True)
    address_ar = models.CharField(_("العنوان بالعربي"), max_length=300, blank=True)
    map_link = models.URLField(_("لينك الموقع على الخريطة"), blank=True)
    working_hours_text = models.CharField(_("مواعيد العمل (سطر الفوتر)"), max_length=300, blank=True)
    timezone = models.CharField(_("التوقيت"), max_length=50, default="Africa/Cairo")
    whatsapp_sender = models.CharField(_("رقم WhatsApp للإرسال"), max_length=20, blank=True)

    class Meta:
        verbose_name = _("إعدادات المنظمة")
        verbose_name_plural = _("إعدادات المنظمات")

    def __str__(self):
        return f"{self.organization} settings"


class Role(models.TextChoices):
    OWNER = "owner", _("مالك")
    ADMIN = "admin", _("مدير النظام")
    DOCTOR = "doctor", _("دكتور")
    RECEPTION = "reception", _("استقبال")
    VIEWER = "viewer", _("مشاهد")
    MEMBER = "member", _("موظف")  # permissions come from the clinic's own role (`Membership.clinic_role`)


class ClinicRole(BaseModel):
    """A role the clinic defines itself: a name + a set of permissions (apps/accounts/roles.py). Every employee except
    the owner has one. `key` remembers which built-in role it started from (blank for roles the clinic created)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="roles")
    name = models.CharField(_("اسم الدور"), max_length=60)
    description = models.CharField(_("وصف"), max_length=200, blank=True)
    permissions = models.JSONField(_("الصلاحيات"), default=list, blank=True)
    key = models.CharField(max_length=20, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    # The one screen this role works in (a key of apps/core/navigation.ITEMS): its people open straight onto it, laid
    # out like a program of its own with no side menu. Blank = the whole system. Layout only: access stays with
    # `permissions`.
    start_screen = models.CharField(_("شاشة العمل"), max_length=20, blank=True)

    class Meta:
        verbose_name = _("دور")
        verbose_name_plural = _("الأدوار")
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_role_name")]

    def __str__(self):
        return self.name


class Membership(BaseModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(_("الدور"), max_length=20, choices=Role.choices, default=Role.RECEPTION)
    clinic_role = models.ForeignKey(
        ClinicRole, null=True, blank=True, on_delete=models.PROTECT, related_name="members", verbose_name=_("الدور")
    )
    is_active = models.BooleanField(_("نشط"), default=True)
    # Per-employee adjustments on top of the role's permissions (apps/accounts/permissions.py):
    # effective = (role's ∪ granted − revoked), closed over IMPLIES.
    granted_permissions = models.JSONField(_("صلاحيات زيادة"), default=list, blank=True)
    revoked_permissions = models.JSONField(_("صلاحيات متشالة"), default=list, blank=True)

    class Meta:
        verbose_name = _("عضوية")
        verbose_name_plural = _("العضويات")
        constraints = [models.UniqueConstraint(fields=["user", "organization"], name="uniq_membership")]

    def __str__(self):
        return f"{self.user} @ {self.organization} ({self.role_name})"

    @property
    def role_name(self):
        return self.clinic_role.name if self.clinic_role_id else self.get_role_display()
