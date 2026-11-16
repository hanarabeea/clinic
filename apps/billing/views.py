from datetime import date

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import require_perm
from apps.audit import services as audit
from apps.billing.models import PaymentMethod
from apps.core.timeutils import CAIRO
from apps.treasury import services as treasury
from apps.treasury.forms import CountForm

from . import services


@require_perm("payment.record")
def cashbox(request):
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = timezone.localtime(timezone.now(), CAIRO).date()
    return render(request, "billing/cashbox.html", {"box": services.cashbox(request.organization, day)})


@require_perm("payment.record")
def shift_close(request):
    """تقفيل الوردية: the cashier counts the drawer, the cash goes to the main safe, and any difference between the
    count and the books waits for someone else's approval."""
    drawer = treasury.for_method(request.organization, PaymentMethod.CASH)
    shift = treasury.current_shift(drawer)
    if shift is None and treasury.cash_on_hand(drawer) > 0:
        # cash sitting in the drawer outside any shift (taken before the books existed): count it like a shift
        shift = treasury.ensure_shift(drawer, by=request.user)
    if shift is None:
        messages.info(request, _("الدرج فاضي ومفيش وردية مفتوحة. الوردية بتتفتح مع أول فلوس تتاخد."))
        return redirect("billing:cashbox")
    form = CountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            count = treasury.close_shift(
                shift, denominations=form.denominations(), by=request.user, keep=d.get("keep") or 0, notes=d["notes"]
            )
        except treasury.TreasuryError as e:
            form.add_error(None, str(e))
        else:
            audit.log(
                "treasury.shift_closed", request=request, target=count, summary=f"{drawer.name}: {count.difference}"
            )
            if count.difference == 0:
                messages.success(request, _("الوردية اتقفلت والجرد مطابق."))
            else:
                kind = _("عجز") if count.difference < 0 else _("زيادة")
                messages.warning(
                    request,
                    _("الوردية اتقفلت وفيه %(k)s %(d)s. الفرق مستني اعتماد حد تاني.")
                    % {"k": kind, "d": abs(count.difference)},
                )
            return redirect("billing:cashbox")
    ctx = {"treasury": drawer, "form": form, "surprise": False, "back": reverse("billing:cashbox")}
    return render(request, "treasury/count_form.html", ctx)
