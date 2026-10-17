from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from bot.db.models import Loan, LoanDue
from bot.services.loans import KIND_TITLES, LoanKind
from bot.services.money import format_rub

BTN_HOME = "🏠 Главная"
BTN_LOANS = "💳 Кредиты"
BTN_INCOME = "➕ Доход"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SUBS = "🔁 Подписки"

# Russian time zones, west to east
RU_TIMEZONES = [
    ("Калининград", "Europe/Kaliningrad", 2),
    ("Москва", "Europe/Moscow", 3),
    ("Самара", "Europe/Samara", 4),
    ("Екатеринбург", "Asia/Yekaterinburg", 5),
    ("Омск", "Asia/Omsk", 6),
    ("Новосибирск", "Asia/Novosibirsk", 7),
    ("Иркутск", "Asia/Irkutsk", 8),
    ("Якутск", "Asia/Yakutsk", 9),
    ("Владивосток", "Asia/Vladivostok", 10),
    ("Магадан", "Asia/Magadan", 11),
    ("Камчатка", "Asia/Kamchatka", 12),
]

SHIFT_PRESETS = ["2/2", "5/2", "3/3", "1/3", "6/1"]


class DueCb(CallbackData, prefix="due"):
    action: str  # paid / part / undo
    due_id: int


class LoanCb(CallbackData, prefix="loan"):
    action: str  # open / pay / early / edit / close / delete / close_yes / delete_yes
    loan_id: int


class EditCb(CallbackData, prefix="ledit"):
    field: str
    loan_id: int


class EarlyCb(CallbackData, prefix="early"):
    mode: str  # term / payment


class PaydayCb(CallbackData, prefix="pday"):
    action: str  # yes / other / delay / d1 / d3 / date
    payday_id: int


def main_menu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for text in (BTN_HOME, BTN_LOANS, BTN_INCOME, BTN_SUBS, BTN_SETTINGS):
        builder.button(text=text)
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Остаток на карте")


def timezone_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for title, key, offset in RU_TIMEZONES:
        builder.button(text=f"{title} (UTC+{offset})", callback_data=f"tz:{key}")
    builder.adjust(2)
    return builder.as_markup()


def shifts_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for preset in SHIFT_PRESETS:
        builder.button(text=preset, callback_data=f"shift:{preset}")
    builder.button(text="Без графика", callback_data="shift:none")
    builder.adjust(3)
    return builder.as_markup()


def shift_position_keyboard(work: int, off: int) -> InlineKeyboardMarkup:
    """'Today is my 2nd shift' anchors the cycle without asking for a date."""
    builder = InlineKeyboardBuilder()
    for i in range(work):
        builder.button(text=f"{i + 1}-я смена", callback_data=f"spos:{i}")
    for i in range(off):
        builder.button(text=f"{i + 1}-й выходной", callback_data=f"spos:{work + i}")
    builder.adjust(max(work, off) if work + off <= 8 else 4)
    return builder.as_markup()


def skip_keyboard(data: str, text: str = "Пропустить") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=data)
    return builder.as_markup()


def settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Часовой пояс", callback_data="settings:tz")
    builder.button(text="График", callback_data="settings:shifts")
    builder.button(text="Выплаты", callback_data="settings:income")
    builder.button(text="Время напоминаний", callback_data="settings:remind")
    builder.button(text="Время про остаток", callback_data="settings:evening")
    builder.button(text="Резерв на жизнь", callback_data="settings:reserve")
    builder.button(text="Порог досрочки", callback_data="settings:threshold")
    builder.button(text="🔄 Обновления", callback_data="settings:updates")
    builder.adjust(2)
    return builder.as_markup()


def loan_kinds_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for kind in LoanKind:
        builder.button(text=KIND_TITLES[kind], callback_data=f"lkind:{kind.value}")
    builder.adjust(2)
    return builder.as_markup()


def pay_method_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Автосписание", callback_data="lpay:auto")
    builder.button(text="Вручную", callback_data="lpay:manual")
    return builder.as_markup()


def confirm_loan_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Сохранить", callback_data="ladd:save")
    builder.button(text="✖️ Отмена", callback_data="ladd:cancel")
    return builder.as_markup()


def loans_keyboard(loans: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for loan_id, text in loans:
        builder.button(text=text, callback_data=LoanCb(action="open", loan_id=loan_id))
    builder.button(text="➕ Добавить кредит", callback_data="ladd:start")
    builder.adjust(1)
    return builder.as_markup()


def loan_card_keyboard(loan: Loan) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if loan.status == "active":
        builder.button(text="✅ Внести платёж", callback_data=LoanCb(action="pay", loan_id=loan.id))
        builder.button(text="⏩ Досрочно", callback_data=LoanCb(action="early", loan_id=loan.id))
        builder.button(text="✏️ Изменить", callback_data=LoanCb(action="edit", loan_id=loan.id))
        builder.button(text="🏁 Закрыт", callback_data=LoanCb(action="close", loan_id=loan.id))
    builder.button(text="🗑 Удалить", callback_data=LoanCb(action="delete", loan_id=loan.id))
    builder.button(text="« К списку", callback_data="loans:list")
    builder.adjust(2)
    return builder.as_markup()


EDIT_FIELDS = {
    "principal": "Долг",
    "payment": "Платёж",
    "due": "Дата платежа",
    "rate": "Ставка",
    "method": "Способ оплаты",
    "insurance": "Страховка/комиссии",
    "penalty": "Пени",
}


def loan_edit_keyboard(loan_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for field, title in EDIT_FIELDS.items():
        builder.button(text=title, callback_data=EditCb(field=field, loan_id=loan_id))
    builder.button(text="« Назад", callback_data=LoanCb(action="open", loan_id=loan_id))
    builder.adjust(2)
    return builder.as_markup()


def yes_no_keyboard(yes: CallbackData | str, no: CallbackData | str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да", callback_data=yes)
    builder.button(text="Нет", callback_data=no)
    return builder.as_markup()


def early_mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Уменьшить срок", callback_data=EarlyCb(mode="term"))
    builder.button(text="Уменьшить платёж", callback_data=EarlyCb(mode="payment"))
    return builder.as_markup()


def due_pay_keyboard(due: LoanDue) -> InlineKeyboardMarkup:
    left = due.amount_minor - due.paid_minor
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Оплатил {format_rub(left)}",
                   callback_data=DueCb(action="paid", due_id=due.id))
    builder.button(text="Другая сумма", callback_data=DueCb(action="part", due_id=due.id))
    builder.adjust(1)
    return builder.as_markup()


def balance_keyboard(
    settled: list[tuple[Loan, LoanDue]], unconfirmed: list[tuple[Loan, LoanDue]]
) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    for loan, due in settled:
        builder.button(text=f"❌ {loan.lender}: не списалось",
                       callback_data=DueCb(action="undo", due_id=due.id))
    for loan, due in unconfirmed:
        builder.button(text=f"✅ {loan.lender}: оплатил",
                       callback_data=DueCb(action="paid", due_id=due.id))
    builder.adjust(1)
    return builder.as_markup() if settled or unconfirmed else None


def payday_keyboard(payday_id: int, amount: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Да, {format_rub(amount)}",
                   callback_data=PaydayCb(action="yes", payday_id=payday_id))
    builder.button(text="Да, другая сумма",
                   callback_data=PaydayCb(action="other", payday_id=payday_id))
    builder.button(text="Задерживается",
                   callback_data=PaydayCb(action="delay", payday_id=payday_id))
    builder.adjust(1)
    return builder.as_markup()


def shortfall_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Что делать", callback_data="short:plan")
    return builder.as_markup()


def prepay_keyboard(loan_id: int, title: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"⏩ Досрочно в {title}", callback_data=LoanCb(action="early",
                                                                     loan_id=loan_id))
    return builder.as_markup()


def delay_keyboard(payday_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="До завтра", callback_data=PaydayCb(action="d1", payday_id=payday_id))
    builder.button(text="На 3 дня", callback_data=PaydayCb(action="d3", payday_id=payday_id))
    builder.button(text="Ввести дату", callback_data=PaydayCb(action="date", payday_id=payday_id))
    builder.adjust(2)
    return builder.as_markup()


def income_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Фиксированные суммы", callback_data="itype:fixed")
    builder.button(text="Почасовая оплата", callback_data="itype:hourly")
    builder.button(text="Пропустить", callback_data="itype:skip")
    builder.adjust(1)
    return builder.as_markup()


def tax_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="На руки (0%)", callback_data="itax:0")
    builder.button(text="До НДФЛ 13%", callback_data="itax:13")
    builder.button(text="Другой %", callback_data="itax:other")
    builder.adjust(2)
    return builder.as_markup()


def weekend_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Раньше, в пятницу", callback_data="iweekend:before")
    builder.button(text="Позже, в понедельник", callback_data="iweekend:after")
    builder.button(text="В тот же день", callback_data="iweekend:same")
    builder.adjust(1)
    return builder.as_markup()


def income_settings_keyboard(hourly: bool, has_income: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Настроить заново" if has_income else "Настроить",
                   callback_data="iset:all")
    if has_income:
        builder.button(text="🚫 Сейчас нет дохода", callback_data="iset:none")
    if hourly:
        builder.button(text="Ставка", callback_data="iset:rate")
        builder.button(text="Часы в смене", callback_data="iset:hours")
        builder.button(text="Налог", callback_data="iset:tax")
        builder.button(text="Периоды", callback_data="iset:cuts")
        builder.button(text="Через сколько дней", callback_data="iset:delay")
    builder.button(text="Выплата в выходной", callback_data="iset:weekend")
    builder.button(text="« Настройки", callback_data="settings:back")
    builder.adjust(*((1, 1, 2) if has_income else (1, 2)))
    return builder.as_markup()
