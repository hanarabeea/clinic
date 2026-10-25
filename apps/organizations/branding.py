"""The clinic shown before anyone signs in (login, password reset).

One install = one clinic (CLAUDE.md "one clinic = one Organization"): when exactly one active organization exists, its
colours, name, doctor and logo brand the sign-in pages. With none or several, the neutral DigiClinic look is used, so
a shared server never reveals which clinics it hosts.
"""

from django.core.exceptions import ObjectDoesNotExist

from .models import Organization


def public_brand():
    orgs = list(Organization.objects.filter(is_active=True).order_by("id")[:2])
    if len(orgs) != 1:
        return None
    org = orgs[0]
    try:
        settings = org.settings
    except ObjectDoesNotExist:
        return None
    from apps.doctors.models import Doctor

    return {
        "org": org,
        "settings": settings,
        "doctor": Doctor.objects.filter(organization=org).order_by("id").first(),
    }
