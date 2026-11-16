"""The books (docs/plan/14-obgyn-center.md §14.5): chart of accounts, fiscal periods and the journal.

This is the part that must never be wrong. Three layers keep it right:
1. `ledger.services.post()` is the only code that creates entries, and it checks everything before saving;
2. PostgreSQL itself (migration 0002): a line has exactly one of debit / credit, a posted entry balances and has at
   least two lines (checked at commit by a deferred constraint trigger), posted entries and their lines can never be
   changed or deleted (corrections are reversal entries), posting needs an open period, a leaf account and one clinic
   on every row;
3. the verification job (`verify_books`, phase 19) proves it again every night.
"""

from django.conf import settings as dj_settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel


class LedgerSettings(TenantScopedModel):
    """One row per clinic (`for_org` creates it)."""

    fiscal_year_start_month = models.PositiveSmallIntegerField(_("أول شهر في السنة المالية"), default=1)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("إعدادات الحسابات")
        verbose_name_plural = _("إعدادات الحسابات")
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="uniq_ledger_settings_per_org"),
            models.CheckConstraint(
                condition=Q(fiscal_year_start_month__gte=1, fiscal_year_start_month__lte=12),
                name="ledger_fiscal_start_month_1_12",
            ),
        ]

    def __str__(self):
        return f"{self.organization} ledger settings"

    @classmethod
    def for_org(cls, org):
        obj, _created = cls.objects.get_or_create(organization=org)
        return obj


# --- chart of accounts -----------------------------------------------------------------------------------------


class AccountType(models.TextChoices):
    ASSET = "asset", _("أصول")
    LIABILITY = "liability", _("التزامات")
    EQUITY = "equity", _("حقوق ملكية")
    REVENUE = "revenue", _("إيرادات")
    EXPENSE = "expense", _("مصروفات")


DEBIT_NORMAL = {AccountType.ASSET, AccountType.EXPENSE}


class Account(TenantScopedModel):
    """One account of the tree. The code is hierarchical (a child's code starts with its parent's), so sorting by
    code lists the tree in order. Only leaf accounts (`is_group=False`) take postings. `system_key` names the accounts
    the program itself posts to (cash_main, patients_receivable...): they can be renamed, never removed."""

    code = models.CharField(_("الكود"), max_length=20)
    name_ar = models.CharField(_("اسم الحساب"), max_length=120)
    name_en = models.CharField(_("الاسم بالإنجليزي"), max_length=120, blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    type = models.CharField(_("النوع"), max_length=10, choices=AccountType.choices)
    is_group = models.BooleanField(_("حساب رئيسي (مجموعة)"), default=False)
    is_active = models.BooleanField(_("مفعّل"), default=True)
    system_key = models.CharField(max_length=40, blank=True, editable=False)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("حساب")
        verbose_name_plural = _("شجرة الحسابات")
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_account_code_per_org"),
            models.UniqueConstraint(
                fields=["organization", "system_key"], condition=~Q(system_key=""), name="uniq_account_system_key"
            ),
        ]

    def __str__(self):
        return f"{self.code} {self.name_ar}"

    @property
    def debit_normal(self) -> bool:
        return self.type in DEBIT_NORMAL

    @property
    def is_result(self) -> bool:
        """Revenue and expense accounts: every line on them names a cost center."""
        return self.type in (AccountType.REVENUE, AccountType.EXPENSE)


class CostCenter(TenantScopedModel):
    """Where revenue is earned and money is spent (outpatient, theatre, emergency, inpatient, administration)."""

    code = models.CharField(_("الكود"), max_length=10)
    name_ar = models.CharField(_("الاسم"), max_length=80)
    system_key = models.CharField(max_length=40, blank=True, editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("مركز تكلفة")
        verbose_name_plural = _("مراكز التكلفة")
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="uniq_cost_center_code")]

    def __str__(self):
        return self.name_ar


# --- fiscal years and periods ----------------------------------------------------------------------------------


class PeriodStatus(models.TextChoices):
    OPEN = "open", _("مفتوحة")
    CLOSED = "closed", _("مقفولة")


class FiscalYear(TenantScopedModel):
    name = models.CharField(max_length=20)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=PeriodStatus.choices, default=PeriodStatus.OPEN)

    class Meta:
        verbose_name = _("سنة مالية")
        verbose_name_plural = _("السنوات المالية")
        ordering = ["start_date"]
        constraints = [models.UniqueConstraint(fields=["organization", "start_date"], name="uniq_fiscal_year_start")]

    def __str__(self):
        return self.name


class FiscalPeriod(TenantScopedModel):
    """A month of a fiscal year. Nothing posts into a closed one (the service refuses, and so does the database)."""

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name="periods")
    number = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=PeriodStatus.choices, default=PeriodStatus.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("فترة مالية")
        verbose_name_plural = _("الفترات المالية")
        ordering = ["start_date"]
        constraints = [models.UniqueConstraint(fields=["fiscal_year", "number"], name="uniq_fiscal_period_number")]
        indexes = [models.Index(fields=["organization", "start_date", "end_date"])]

    def __str__(self):
        return f"{self.start_date:%Y-%m}"

    @property
    def is_open(self) -> bool:
        return self.status == PeriodStatus.OPEN


class EntrySequence(TenantScopedModel):
    """The last entry number of one fiscal year. Read and bumped under `select_for_update` in the same transaction
    that posts the entry, so numbers have no gaps and no duplicates."""

    fiscal_year = models.OneToOneField(FiscalYear, on_delete=models.CASCADE, related_name="sequence")
    last_number = models.PositiveIntegerField(default=0)


# --- journal ---------------------------------------------------------------------------------------------------


class EntryStatus(models.TextChoices):
    DRAFT = "draft", _("مسودة")
    POSTED = "posted", _("مرحّل")


class JournalEntry(TenantScopedModel):
    """One journal entry. Written by `ledger.services.post()` only. Once posted it never changes: a mistake is
    corrected by a reversal entry (`reverses`), so the books always show what happened and when it was put right.
    `source_*` names the document that produced it; one document posts one entry per event."""

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name="entries")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="entries")
    number = models.PositiveIntegerField(null=True, blank=True)
    date = models.DateField()
    memo = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=EntryStatus.choices, default=EntryStatus.DRAFT)
    total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    source_type = models.CharField(max_length=60, blank=True)
    source_id = models.BigIntegerField(null=True, blank=True)
    source_event = models.CharField(max_length=30, blank=True)
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal")
    created_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("قيد يومية")
        verbose_name_plural = _("قيود اليومية")
        ordering = ["date", "number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fiscal_year", "number"],
                condition=Q(number__isnull=False),
                name="uniq_entry_number_per_year",
            ),
            models.UniqueConstraint(
                fields=["organization", "source_type", "source_id", "source_event"],
                condition=~Q(source_type=""),
                name="uniq_entry_per_source_event",
            ),
            models.CheckConstraint(
                condition=Q(status="draft") | Q(number__isnull=False, posted_at__isnull=False),
                name="ledger_posted_entry_has_number",
            ),
        ]
        indexes = [models.Index(fields=["organization", "date"])]

    def __str__(self):
        return f"{self.number or '-'} ({self.date})"

    @property
    def is_posted(self) -> bool:
        return self.status == EntryStatus.POSTED

    @property
    def is_reversed(self) -> bool:
        return hasattr(self, "reversal")


class PartyType(models.TextChoices):
    PATIENT = "patient", _("مريض")
    SUPPLIER = "supplier", _("مورد")
    DOCTOR = "doctor", _("دكتور")
    EMPLOYEE = "employee", _("موظف")
    PAYER = "payer", _("جهة تعاقد")


class JournalLine(TenantScopedModel):
    """One side of an entry: an amount on the debit side or on the credit side of one leaf account (never both, never
    zero: a CHECK constraint). `party_*` says whose account it is inside a control account (which patient, which
    supplier), so every control account can be proven against its subledger."""

    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField(default=1)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    cost_center = models.ForeignKey(CostCenter, null=True, blank=True, on_delete=models.PROTECT, related_name="lines")
    party_type = models.CharField(max_length=10, choices=PartyType.choices, blank=True)
    party_id = models.BigIntegerField(null=True, blank=True)
    memo = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = _("سطر قيد")
        verbose_name_plural = _("سطور القيود")
        ordering = ["entry_id", "line_no", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(debit__gt=0) & Q(credit=0)) | (Q(credit__gt=0) & Q(debit=0)),
                name="ledger_line_one_side_only",
            ),
            models.CheckConstraint(
                condition=(Q(party_type="") & Q(party_id__isnull=True))
                | (~Q(party_type="") & Q(party_id__isnull=False)),
                name="ledger_line_party_whole_or_none",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "account"]),
            models.Index(fields=["organization", "party_type", "party_id"]),
        ]

    def __str__(self):
        return f"{self.account_id}: {self.debit or -self.credit}"

    @property
    def amount(self):
        """Signed: debit positive, credit negative."""
        return self.debit - self.credit


# --- books verification ----------------------------------------------------------------------------------------


class BooksCheck(TenantScopedModel):
    """One run of the books verification (ledger/verify.py): when, by whom (blank = the nightly job), and what it
    found. `problems` is a list of {"code", "message"}; empty when the books are sound."""

    ran_at = models.DateTimeField()
    ok = models.BooleanField()
    problems = models.JSONField(default=list, blank=True)
    seconds = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    ran_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = _("فحص دفاتر")
        verbose_name_plural = _("فحوصات الدفاتر")
        ordering = ["-ran_at", "-id"]
        indexes = [models.Index(fields=["organization", "-ran_at"])]

    def __str__(self):
        return f"{self.ran_at:%Y-%m-%d %H:%M} {'ok' if self.ok else 'problems'}"
