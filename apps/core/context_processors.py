from django.conf import settings

from apps.accounts.permissions import permissions_for
from apps.organizations.branding import public_brand

from . import navigation


def organization(request):
    membership = getattr(request, "membership", None)
    org = getattr(request, "organization", None)
    org_settings = getattr(org, "settings", None) if org else None
    brand = (None if org else public_brand()) or {"org": None, "settings": None, "doctor": None}
    return {
        "current_org": org,
        "current_org_settings": org_settings,
        # signed out (login / password reset): the clinic's own look when this install has a single clinic
        "public_brand": brand,
        "brand_settings": org_settings or brand["settings"],  # whose colours / tab icon the page wears
        "current_membership": membership,
        "can": permissions_for(membership),
        "desk_idle_lock_minutes": settings.DESK_IDLE_LOCK_MINUTES,
        # the side menu (nav_groups), or the role's single work screen (work): apps/core/navigation.py
        **navigation.for_request(request),
    }
