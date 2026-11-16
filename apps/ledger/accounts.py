"""Editing the chart of accounts: add, rename, move, switch off, remove. The rules that keep the tree usable:

- an account sits under a group of its own type, and its code starts with the group's code;
- an account that has postings is never deleted, never becomes a group and never changes type (the database refuses
  too); it can be renamed or, with a zero balance, switched off;
- the accounts the program posts to (`system_key`) and the accounts of treasuries stay.
"""

from django.db import transaction
from django.utils.translation import gettext as _

from .models import Account, JournalLine
from .services import LedgerError, balance_of


def has_postings(account) -> bool:
    return JournalLine.objects.filter(account=account).exists()


def is_treasury_account(account) -> bool:
    return hasattr(account, "treasury")


def suggest_code(parent) -> str:
    """The next free code under `parent`: its code plus two digits ("1101" → "110103")."""
    taken = set(Account.objects.filter(organization=parent.organization_id).values_list("code", flat=True))
    for n in range(1, 100):
        code = f"{parent.code}{n:02d}"
        if code not in taken:
            return code
    raise LedgerError(_("المجموعة دي مليانة. اختار كود بنفسك."))


def _check_code(org, parent, code, *, ignore=None) -> str:
    code = (code or "").strip()
    if not code.isdigit():
        raise LedgerError(_("الكود أرقام بس."))
    if not code.startswith(parent.code) or len(code) <= len(parent.code):
        raise LedgerError(_("الكود لازم يبدأ بكود المجموعة (%(p)s) ويزيد عليه.") % {"p": parent.code})
    if len(code) > 20:
        raise LedgerError(_("الكود طويل قوي."))
    same = Account.objects.filter(organization=org, code=code)
    if ignore is not None:
        same = same.exclude(pk=ignore.pk)
    if same.exists():
        raise LedgerError(_("الكود ده مستخدم."))
    return code


def _check_parent(org, parent) -> None:
    if parent is None or parent.organization_id != org.pk:
        raise LedgerError(_("اختار المجموعة اللي الحساب تحتها."))
    if not parent.is_group:
        raise LedgerError(_("%(p)s حساب فرعي، مينفعش يتضاف تحته. اختار مجموعة.") % {"p": parent})


@transaction.atomic
def add_account(org, *, parent, name_ar, code="", name_en="", is_group=False, by=None) -> Account:
    _check_parent(org, parent)
    name_ar = " ".join((name_ar or "").split())
    if not name_ar:
        raise LedgerError(_("اكتب اسم الحساب."))
    code = _check_code(org, parent, code) if code else suggest_code(parent)
    account = Account(
        organization=org,
        code=code,
        name_ar=name_ar[:120],
        name_en=(name_en or "").strip()[:120],
        parent=parent,
        type=parent.type,
        is_group=is_group,
    )
    account._history_user = by
    account.save()
    return account


@transaction.atomic
def rename(account, *, name_ar, name_en="", by=None) -> Account:
    name_ar = " ".join((name_ar or "").split())
    if not name_ar:
        raise LedgerError(_("اكتب اسم الحساب."))
    account.name_ar, account.name_en = name_ar[:120], (name_en or "").strip()[:120]
    account._history_user = by
    account.save(update_fields=["name_ar", "name_en", "updated_at"])
    return account


@transaction.atomic
def move(account, *, parent, by=None) -> Account:
    """Put a leaf account under another group of the same type. It gets the next free code there."""
    org = account.organization
    _check_parent(org, parent)
    if account.is_group:
        raise LedgerError(_("المجموعات بتفضل في مكانها. انقل الحسابات الفرعية."))
    if parent.type != account.type:
        raise LedgerError(_("الحساب بيتنقل جوه نفس النوع بس (%(t)s).") % {"t": account.get_type_display()})
    if parent.pk == account.parent_id:
        return account
    account.parent, account.code = parent, suggest_code(parent)
    account._history_user = by
    account.save(update_fields=["parent", "code", "updated_at"])
    return account


@transaction.atomic
def set_active(account, active: bool, *, by=None) -> Account:
    if not active:
        if account.system_key or is_treasury_account(account):
            raise LedgerError(_("الحساب ده البرنامج بيستخدمه، مينفعش يتوقف."))
        if account.is_group and account.children.filter(is_active=True).exists():
            raise LedgerError(_("وقّف الحسابات اللي تحته الأول."))
        if not account.is_group and balance_of(account) != 0:
            raise LedgerError(_("الحساب عليه رصيد. صفّره الأول."))
    account.is_active = active
    account._history_user = by
    account.save(update_fields=["is_active", "updated_at"])
    return account


@transaction.atomic
def delete_account(account) -> None:
    """Only an account nothing was ever posted to (and nothing hangs under) can be removed."""
    if account.system_key or is_treasury_account(account):
        raise LedgerError(_("الحساب ده البرنامج بيستخدمه، مينفعش يتمسح."))
    if account.children.exists():
        raise LedgerError(_("فيه حسابات تحته."))
    if has_postings(account):
        raise LedgerError(_("الحساب عليه قيود، مينفعش يتمسح. ممكن توقفه."))
    account.delete()
