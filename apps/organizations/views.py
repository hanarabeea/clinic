from django.contrib import messages
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from apps.accounts.permissions import require_membership, require_perm
from apps.audit import services as audit

from .branding import logo_content_type, public_brand
from .forms import CompanySettingsForm, OrganizationForm
from .models import OrganizationSettings


@require_perm("settings.manage")
def settings_company(request):
    org = request.organization
    org_settings, _created = OrganizationSettings.objects.get_or_create(organization=org)
    org_form = OrganizationForm(request.POST or None, instance=org, prefix="org")
    form = CompanySettingsForm(request.POST or None, request.FILES or None, instance=org_settings)
    if request.method == "POST" and org_form.is_valid() and form.is_valid():
        with transaction.atomic():
            org_form.save()
            form.save()
        audit.log(
            "settings.company", request=request, fields=sorted(set(org_form.changed_data) | set(form.changed_data))
        )
        messages.success(request, _("بيانات الشركة اتحفظت."))
        return redirect("organizations:settings")
    return render(request, "organizations/settings_company.html", {"org_form": org_form, "form": form})


def public_logo(request):
    """The logo on the sign-in pages — only for a single-clinic install (branding.public_brand), so a shared server
    never shows one clinic's logo to another's staff. The clinic's logo is public anyway (sign, prescriptions)."""
    brand = public_brand()
    if brand is None or not brand["settings"].logo:
        raise Http404
    logo_file = brand["settings"].logo
    resp = FileResponse(logo_file.open("rb"), content_type=logo_content_type(logo_file.name))
    resp["Cache-Control"] = "public, max-age=86400"  # the URL carries ?v=<updated_at>
    return resp


@require_membership
def logo(request):
    """Branding logo is private media: served only to members of the organization."""
    org_settings = getattr(request.organization, "settings", None)
    if org_settings is None or not org_settings.logo:
        raise Http404
    return FileResponse(org_settings.logo.open("rb"), content_type=logo_content_type(org_settings.logo.name))
