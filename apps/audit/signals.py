from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models import Q
from django.dispatch import receiver

from apps.organizations.models import Membership

from .services import log


def _org_of(user):
    m = Membership.objects.filter(user=user, is_active=True).select_related("organization").order_by("id").first()
    return m.organization if m else None


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    log("auth.login", request=request, org=_org_of(user), actor=user, summary=user.username)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        log("auth.logout", request=request, org=_org_of(user), actor=user, summary=user.username)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    from apps.accounts.models import User

    login = ((credentials or {}).get("username", "") or "").strip()
    user = User.objects.filter(Q(username__iexact=login) | Q(email__iexact=login)).first() if login else None
    log(
        "auth.login_failed",
        request=request,
        org=_org_of(user) if user else None,
        summary=login[:150],  # the attempted username / email only, never the password
    )
