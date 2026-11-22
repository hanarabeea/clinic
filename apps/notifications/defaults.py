"""Default message templates (06 §6.3). The clinic edits them from the templates screen."""

from .models import AppliesTo, Event

DEFAULT_TEMPLATES = [
    {
        "event": Event.BOOKING_CONFIRMED,
        "name_ar": "تأكيد الحجز",
        "body": (
            "أهلًا {first_name} 👋\n"
            "تم حجز {visit_type} مع {doctor_name} يوم {date}، {time_label}.\n"
            "العنوان: {clinic_address}\n"
            "{map_link}\n"
            "للتعديل أو الإلغاء: {clinic_phone}"
        ),
        "email_subject": "تأكيد حجزك",
    },
    {
        "event": Event.REMINDER,
        "name_ar": "تذكير قبلها بيوم",
        "offset_minutes": 1440,
        "min_lead_minutes": 120,
        "body": (
            "تذكير: ميعادك مع {doctor_name} بكرة {date}، {time_label}.\n"
            "لو مش هتقدر تيجي يا ريت تبلغنا على {clinic_phone} عشان نِدّي الميعاد لحد تاني."
        ),
        "email_subject": "تذكير بميعادك بكرة",
    },
    {
        "event": Event.REMINDER,
        "name_ar": "تذكير قبلها بساعة",
        "offset_minutes": 60,
        "min_lead_minutes": 10,
        "applies_to_mode": AppliesTo.SLOTS,
        "body": "ميعادك مع {doctor_name} النهارده {time_label} — فاضل حوالي ساعة. في انتظارك 🌿",
        "email_subject": "ميعادك بعد ساعة",
    },
    {
        "event": Event.RESCHEDULED,
        "name_ar": "تعديل الميعاد",
        "body": "تم تعديل ميعادك مع {doctor_name} من {old_date} {old_time} إلى {date}، {time_label}.",
        "email_subject": "تعديل ميعادك",
    },
    {
        "event": Event.CANCELLED,
        "name_ar": "إلغاء الحجز",
        "body": "تم إلغاء ميعادك مع {doctor_name} يوم {date}. للحجز من جديد: {clinic_phone}",
        "email_subject": "إلغاء ميعادك",
    },
    {
        "event": Event.NEAR_TURN,
        "name_ar": "دورك قرّب",
        "applies_to_mode": AppliesTo.QUEUE,
        "body": "{first_name}، دورك قرّب 🔔 قدامك {patients_ahead} بس. يا ريت تكون في العيادة.",
        "email_subject": "دورك قرّب",
    },
    {
        "event": Event.NO_SHOW_REBOOKED,
        "name_ar": "غياب + إعادة حجز",
        "body": (
            "افتقدناك النهارده يا {first_name}. حجزنالك ميعاد جديد يوم {date}، {time_label}.\n"
            "لو مش مناسب كلمنا على {clinic_phone}."
        ),
        "email_subject": "ميعادك الجديد",
    },
    {
        "event": Event.NO_SHOW_MISSED,
        "name_ar": "غياب (افتقدناك)",
        "body": "افتقدناك النهارده يا {first_name}. تقدر تحجز ميعاد جديد على {clinic_phone}.",
        "email_subject": "افتقدناك",
    },
    {
        "event": Event.PRESCRIPTION,
        "name_ar": "الروشتة",
        "attach_pdf": True,
        "body": "{first_name}، دي روشتة زيارتك النهارده مع {doctor_name} (رقم {rx_number}). ألف سلامة عليك.",
        "email_subject": "روشتة زيارتك",
    },
    {
        "event": Event.OPERATION_BOOKED,
        "name_ar": "حجز عملية",
        "body": (
            "أهلًا {first_name}، تم حجز {operation_name} مع {doctor_name} "
            "يوم {operation_date} الساعة {operation_time}.\n"
            "العنوان: {clinic_address}\n"
            "لأي استفسار: {clinic_phone}"
        ),
        "email_subject": "حجز عمليتك",
    },
    {
        "event": Event.OPERATION_REMINDER,
        "name_ar": "تذكير بعملية (قبلها بيوم)",
        "body": (
            "تذكير: {operation_name} بكرة {operation_date} الساعة {operation_time} مع {doctor_name}.\n"
            "الصيام عن الأكل والشرب قبلها بـ{fasting_hours} ساعات، وهاتي معاكي التحاليل وبطاقتك.\n"
            "لأي استفسار: {clinic_phone}"
        ),
        "email_subject": "تذكير بعمليتك بكرة",
    },
    {
        "event": Event.OPERATION_CHANGED,
        "name_ar": "تغيير ميعاد عملية",
        "body": (
            "{first_name}، ميعاد {operation_name} اتغير: يوم {operation_date} الساعة {operation_time}.\n"
            "لأي استفسار: {clinic_phone}"
        ),
        "email_subject": "تغيير ميعاد عمليتك",
    },
    {
        "event": Event.WAITLIST_OFFER,
        "name_ar": "ميعاد فضي (قايمة الانتظار)",
        "body": (
            "أهلًا {first_name}، فضي ميعاد مع {doctor_name} يوم {date}، {time_label}.\n"
            "لو يناسبك ابعت 1 في خلال {hold_minutes} دقيقة ونحجزهولك، ولو مش مناسب ابعت 2.\n{clinic_name}"
        ),
        "email_subject": "فضي ميعاد",
    },
    {
        "event": Event.REPLY_CONFIRMED,
        "name_ar": "رد: الميعاد اتأكد",
        "body": "تمام يا {first_name}، ميعادك مع {doctor_name} يوم {date}، {time_label} اتأكد. في انتظارك.",
        "email_subject": "ميعادك اتأكد",
    },
    # The two below are for OB/GYN clinics only ("obgyn": seeding skips them elsewhere).
    {
        "event": Event.MILESTONE_DUE,
        "name_ar": "وقت محطة متابعة الحمل",
        "body": (
            "{first_name}، إنتِ دلوقتي في الأسبوع {pregnancy_week} من الحمل، وده وقت: {milestone_name}.\n"
            "للحجز أو الاستفسار: {clinic_phone}\n{clinic_name}"
        ),
        "email_subject": "وقت متابعة مهمة في حملك",
        "obgyn": True,
    },
    {
        "event": Event.POSTPARTUM,
        "name_ar": "متابعة بعد الولادة",
        "body": (
            "حمد لله على سلامتك يا {first_name}، وألف مبروك.\n"
            "فاضل أسبوع على ميعاد متابعة بعد الولادة. للحجز: {clinic_phone}\n{clinic_name}"
        ),
        "email_subject": "متابعة بعد الولادة",
        "obgyn": True,
    },
]


def templates_for(org) -> list[dict]:
    """The default templates that make sense for this clinic, ready to create (the "obgyn" marker taken out)."""
    from apps.obstetrics.services import enabled

    obgyn = enabled(org)
    return [
        {k: v for k, v in tpl.items() if k != "obgyn"} for tpl in DEFAULT_TEMPLATES if obgyn or not tpl.get("obgyn")
    ]


# Events added after a clinic was set up: `add_missing` gives it their default template (and only theirs).
LATER_EVENTS = (
    Event.OPERATION_BOOKED, Event.OPERATION_REMINDER, Event.OPERATION_CHANGED, Event.MILESTONE_DUE, Event.POSTPARTUM,
    Event.WAITLIST_OFFER, Event.REPLY_CONFIRMED,
)  # fmt: skip
