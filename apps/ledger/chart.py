"""The starting chart of accounts: a standard Egyptian tree for a medical center, in five classes
(1 الأصول، 2 الالتزامات، 3 حقوق الملكية، 4 الإيرادات، 5 المصروفات).

It is data: every clinic gets its own copy (`ensure(org)`), and may rename, add, move or switch off accounts from the
accounts screen. What the program relies on is only the `system_key` of the accounts it posts to; `account(org, key)`
finds them. The tree and the tax treatment behind it must be confirmed by the center's accountant before go-live
(plan 14 §14.0).
"""

from django.db import transaction

from .models import Account, AccountType, CostCenter

A, L, Q, R, X = AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY, AccountType.REVENUE, AccountType.EXPENSE

# (code, Arabic name, English name, type, system key)
CHART = [
    ("1", "الأصول", "Assets", A, ""),
    ("11", "الأصول المتداولة", "Current assets", A, ""),
    ("1101", "النقدية بالخزائن", "Cash on hand", A, ""),
    ("110101", "الخزينة الرئيسية", "Main safe", A, "cash_main"),
    ("110102", "درج الاستقبال", "Reception drawer", A, "cash_reception"),
    ("1102", "النقدية بالبنوك", "Cash at banks", A, ""),
    ("110201", "البنك: الحساب الجاري", "Bank current account", A, "bank_main"),
    ("1103", "مقبوضات إلكترونية تحت التسوية", "Electronic receipts in transit", A, ""),
    ("110301", "مقبوضات الفيزا تحت التسوية", "Card receipts in transit", A, "card_clearing"),
    ("110302", "مقبوضات إنستاباي", "InstaPay receipts", A, "instapay_clearing"),
    ("110303", "مقبوضات المحافظ الإلكترونية", "Wallet receipts", A, "wallet_clearing"),
    ("1104", "العملاء", "Receivables", A, ""),
    ("110401", "المرضى: ذمم مدينة", "Patients receivable", A, "patients_receivable"),
    ("110402", "جهات التعاقد: ذمم مدينة", "Payers receivable", A, "payers_receivable"),
    ("1105", "أوراق القبض", "Notes receivable", A, ""),
    ("110501", "شيكات تحت التحصيل", "Cheques receivable", A, "cheques_receivable"),
    ("1106", "المخزون", "Inventory", A, ""),
    ("110601", "مخزون المستلزمات الطبية", "Medical supplies inventory", A, "inventory"),
    ("1107", "أرصدة مدينة أخرى", "Other debit balances", A, ""),
    ("110701", "ضريبة القيمة المضافة على المشتريات", "Input VAT (recoverable)", A, "vat_input"),
    ("110702", "ضرائب مخصومة من المنبع (لنا)", "Withholding tax receivable", A, "wht_receivable"),
    ("110703", "مصروفات مدفوعة مقدمًا", "Prepaid expenses", A, "prepaid_expenses"),
    ("110704", "سلف وعهد العاملين", "Staff advances", A, "staff_advances"),
    ("12", "الأصول الثابتة", "Fixed assets", A, ""),
    ("1201", "الأصول الثابتة بالتكلفة", "Fixed assets at cost", A, ""),
    ("120101", "أجهزة ومعدات طبية", "Medical equipment", A, "fixed_medical_equipment"),
    ("120102", "أثاث وتجهيزات", "Furniture and fittings", A, "fixed_furniture"),
    ("120103", "أجهزة كمبيوتر وبرامج", "Computers and software", A, "fixed_computers"),
    ("1202", "مجمع الإهلاك", "Accumulated depreciation", A, ""),
    ("120201", "مجمع إهلاك الأصول الثابتة", "Accumulated depreciation", A, "accumulated_depreciation"),
    ("2", "الالتزامات", "Liabilities", L, ""),
    ("21", "الالتزامات المتداولة", "Current liabilities", L, ""),
    ("2101", "الموردون", "Suppliers", L, ""),
    ("210101", "الموردون", "Suppliers payable", L, "suppliers_payable"),
    ("2102", "أوراق الدفع", "Notes payable", L, ""),
    ("210201", "شيكات صادرة لم تُصرف", "Cheques payable", L, "cheques_payable"),
    ("2103", "مصلحة الضرائب", "Tax authority", L, ""),
    ("210301", "ضريبة القيمة المضافة على المبيعات", "Output VAT", L, "vat_output"),
    ("210302", "ضرائب خصم تحت حساب الضريبة (للمصلحة)", "Withholding tax payable", L, "wht_payable"),
    ("210303", "ضريبة كسب العمل", "Payroll tax payable", L, "payroll_tax_payable"),
    ("2104", "مستحقات", "Accruals", L, ""),
    ("210401", "أتعاب أطباء مستحقة", "Doctor fees payable", L, "doctor_payables"),
    ("210402", "مصروفات مستحقة", "Accrued expenses", L, "accrued_expenses"),
    ("210403", "تأمينات اجتماعية مستحقة", "Social insurance payable", L, "social_insurance_payable"),
    ("210404", "أتعاب فريق خارجي مستحقة", "Outside team fees payable", L, "external_fees_payable"),
    ("2105", "دفعات مقدمة", "Advances received", L, ""),
    ("210501", "دفعات مقدمة من المرضى", "Patient deposits", L, "patient_deposits"),
    ("3", "حقوق الملكية", "Equity", Q, ""),
    ("31", "حقوق الملكية", "Equity", Q, ""),
    ("3101", "رأس المال", "Capital", Q, ""),
    ("310101", "رأس المال", "Capital", Q, "capital"),
    ("3102", "جاري الشركاء", "Partners current account", Q, ""),
    ("310201", "جاري الشركاء", "Partners current account", Q, "partners_current"),
    ("3103", "الأرباح المرحلة", "Retained earnings", Q, ""),
    ("310301", "الأرباح المرحلة", "Retained earnings", Q, "retained_earnings"),
    ("3104", "أرصدة افتتاحية", "Opening balances", Q, ""),
    ("310401", "أرصدة افتتاحية", "Opening balances", Q, "opening_balances"),
    ("4", "الإيرادات", "Revenue", R, ""),
    ("41", "إيرادات النشاط", "Operating revenue", R, ""),
    ("4101", "إيرادات العيادات", "Outpatient revenue", R, ""),
    ("410101", "إيرادات الكشوفات", "Visit fees", R, "revenue_visits"),
    ("410102", "إيرادات الخدمات", "Services revenue", R, "revenue_services"),
    ("410103", "إيرادات الطوارئ", "Emergency revenue", R, "revenue_emergency"),
    ("4102", "إيرادات العمليات والإقامة", "Theatre and inpatient revenue", R, ""),
    ("410201", "إيرادات العمليات", "Operations revenue", R, "revenue_operations"),
    ("410202", "إيرادات الإقامة", "Room revenue", R, "revenue_rooms"),
    ("4103", "مردودات وخصومات", "Discounts allowed", R, ""),
    ("410301", "خصم مسموح به", "Discounts allowed", R, "discounts_allowed"),
    ("42", "إيرادات أخرى", "Other income", R, ""),
    ("4201", "إيرادات أخرى", "Other income", R, ""),
    ("420101", "إيرادات متنوعة", "Sundry income", R, "revenue_other"),
    ("5", "المصروفات", "Expenses", X, ""),
    ("51", "تكلفة النشاط", "Cost of services", X, ""),
    ("5101", "تكلفة النشاط", "Cost of services", X, ""),
    ("510101", "مستلزمات طبية مستهلكة", "Medical supplies used", X, "medical_supplies_expense"),
    ("510102", "أتعاب الأطباء", "Doctor fees", X, "doctor_fees_expense"),
    ("510103", "أجور التمريض والفنيين", "Nursing and technicians wages", X, "nursing_wages"),
    ("52", "مصروفات عمومية وإدارية", "General and administrative", X, ""),
    ("5201", "مصروفات عمومية وإدارية", "General and administrative", X, ""),
    ("520101", "الأجور والمرتبات", "Salaries", X, "salaries"),
    ("520102", "الإيجار", "Rent", X, "rent"),
    ("520103", "كهرباء ومياه وغاز", "Utilities", X, "utilities"),
    ("520104", "تليفون وإنترنت", "Telephone and internet", X, "telecom"),
    ("520105", "صيانة وإصلاح", "Maintenance", X, "maintenance"),
    ("520106", "أدوات مكتبية ومطبوعات", "Stationery and printing", X, "stationery"),
    ("520107", "نظافة", "Cleaning", X, "cleaning"),
    ("520108", "دعاية وتسويق", "Marketing", X, "marketing"),
    ("520109", "مصروفات بنكية وعمولات تحصيل", "Bank charges and collection fees", X, "bank_charges"),
    ("520110", "مصروفات متنوعة", "Sundry expenses", X, "sundry_expenses"),
    ("520111", "إهلاك الأصول الثابتة", "Depreciation", X, "depreciation"),
    ("520112", "عجز وزيادة الخزينة", "Cash over and short", X, "cash_over_short"),
    ("520113", "فروق تقريب", "Rounding differences", X, "rounding"),
    ("520114", "عجز وزيادة المخزون", "Stock over and short", X, "stock_over_short"),
]

# Control accounts: each is the total of a subledger (one balance per patient, payer, supplier, doctor...), so they
# move only through their own documents, never by a manual entry or a free voucher.
CONTROL_KEYS = {
    "patients_receivable", "payers_receivable", "suppliers_payable", "cheques_receivable", "cheques_payable",
    "doctor_payables", "patient_deposits", "inventory",
}  # fmt: skip

COST_CENTERS = [
    ("10", "العيادات الخارجية", "outpatient"),
    ("20", "العمليات", "theatre"),
    ("30", "الطوارئ", "emergency"),
    ("40", "الإقامة", "inpatient"),
    ("90", "الإدارة", "admin"),
]

CODES = [row[0] for row in CHART]


def parent_code(code: str) -> str:
    """The longest shorter code of the chart that `code` starts with ("110101" → "1101")."""
    best = ""
    for other in CODES:
        if other != code and code.startswith(other) and len(other) > len(best):
            best = other
    return best


GROUPS = {parent_code(code) for code in CODES} - {""}


@transaction.atomic
def ensure(org) -> None:
    """Give a clinic the starting tree and cost centers. Safe to call any time: an account is matched by its system
    key (or its code for the group rows) and never overwritten, so whatever the clinic renamed or moved stays."""
    existing = {a.code: a for a in Account.objects.filter(organization=org)}
    keys = {a.system_key for a in existing.values() if a.system_key}
    for code, name_ar, name_en, kind, key in CHART:
        if code in existing or (key and key in keys):
            continue
        parent = existing.get(parent_code(code))
        existing[code] = Account.objects.create(
            organization=org,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            type=kind,
            parent=parent,
            is_group=code in GROUPS,
            system_key=key,
        )
    have = set(CostCenter.objects.filter(organization=org).values_list("system_key", flat=True))
    for code, name_ar, key in COST_CENTERS:
        if key not in have:
            CostCenter.objects.get_or_create(
                organization=org, code=code, defaults={"name_ar": name_ar, "system_key": key}
            )


def account(org, key: str) -> Account:
    """The account the program posts to for `key` (created with the tree on first use)."""
    found = Account.objects.filter(organization=org, system_key=key).first()
    if found is None:
        ensure(org)
        found = Account.objects.get(organization=org, system_key=key)
    return found


def cost_center(org, key: str) -> CostCenter:
    found = CostCenter.objects.filter(organization=org, system_key=key).first()
    if found is None:
        ensure(org)
        found = CostCenter.objects.get(organization=org, system_key=key)
    return found
