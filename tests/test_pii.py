"""mask_pii: кейсы перенесены из demo-n8n/tests/test_lib.js + свои для писем."""

import pytest

from app.pii import mask_pii


def masked(s: str) -> str:
    return mask_pii(s)[0]


# --- перенос из test_lib.js ---

@pytest.mark.parametrize("p", ["+7 (999) 123-45-67", "8 999 123 45 67", "89991234567", "+79991234567",
                               "8-999-123-45-67", "999 123 45 67"])
def test_phones_ru_formats(p):
    text, found = mask_pii("звоните " + p + " вечером")
    assert text == "звоните [телефон скрыт] вечером"
    assert found["phone"] == 1


def test_two_phones_with_space():
    assert masked("89991234567 89997654321") == "[телефон скрыт] [телефон скрыт]"


def test_international_phone():
    assert masked("tel +44 20 7946 0958") == "tel [телефон скрыт]"


def test_email_and_handle():
    text, found = mask_pii("пишите ivan.petrov@mail.ru или @ivan_petrov")
    assert text == "пишите [email скрыт] или [ник скрыт]"
    assert sum(found.values()) == 2


@pytest.mark.parametrize("s", ["тг ivan_petrov", "tg: ivan_petrov", "инста ivan.petrov", "инстаграм ivan_petrov",
                               "insta: ivan_petrov", "Instagram ivan_petrov"])
def test_handle_after_marker(s):
    assert masked(s).endswith("[ник скрыт]")


@pytest.mark.parametrize("s", ['{"category":"installation"}', "инструмент Makita", "инструкция Bosch",
                               "instant Kärcher", "никогда Stihl"])
def test_marker_only_as_separate_word(s):
    assert masked(s) == s


def test_card():
    assert masked("карта 4276 1234 5678 9012") == "карта [номер карты скрыт]"


@pytest.mark.parametrize("s", ["бюджет 150 000 руб", "от 50 000 - 200 000 ₽", "приезжайте 05.10.2026 10:30",
                               "нужно 3 машины на 2 дня", "заказ 1500000"])
def test_budgets_dates_counts_untouched(s):
    assert masked(s) == s


@pytest.mark.parametrize("p", ["(916)123-45-67", "+7.916.123.45.67", "8 9 1 6 1 2 3 4 5 6 7",
                               "+7 9 1 6 1 2 3 4 5 6 7", "8 (4 9 5) 123-45-67", "(495)123-45-67", "8.916.123.45.67"])
def test_phones_unusual_notation(p):
    assert masked("звоните " + p + " вечером") == "звоните [телефон скрыт] вечером"


def test_phone_followed_by_dot_and_number():
    assert masked("звоните 8.916.123.45.67. 2 раза") == "звоните [телефон скрыт]. 2 раза"


@pytest.mark.parametrize("e", ["иван@почта.рф", "ivan@gmail", "ivan собака mail точка ru", "ivan [at] mail [dot] ru",
                               "ivan(at)mail(dot)ru"])
def test_email_cyrillic_no_tld_words(e):
    assert masked("пишите " + e + " днём") == "пишите [email скрыт] днём"


def test_bare_at_dot_is_not_email():
    assert masked("look at this dot net") == "look at this dot net"


@pytest.mark.parametrize("s, expected", [
    ("мой t.me/ivan_petrov", "мой [ссылка скрыта]"),
    ("https://vk.com/id12345 и wa.me/79161234567", "[ссылка скрыта] и [ссылка скрыта]"),
    ("tg: ivan_petrov", "tg: [ник скрыт]"),
    ("телеграм ivan_petrov", "телеграм [ник скрыт]"),
    ("пишите в телеграм или whatsapp", "пишите в телеграм или whatsapp"),
])
def test_links_and_marker_handles(s, expected):
    assert masked(s) == expected


@pytest.mark.parametrize("s, expected", [
    ("паспорт 4510 123456", "паспорт [номер документа скрыт]"),
    ("паспорт 4510123456", "паспорт [номер документа скрыт]"),
    ("СНИЛС 123-456-789 01", "СНИЛС [номер документа скрыт]"),
    ("ИНН 771234567890", "ИНН [номер документа скрыт]"),
    ("ИНН: 7712345678", "ИНН: [номер документа скрыт]"),
])
def test_documents(s, expected):
    assert masked(s) == expected


@pytest.mark.parametrize("s", ["от 80 000-900 000", "от 70 000 - 100 000", "нужно 7 000 000 - 8 000 000 руб",
                               "бюджет 15-20 тыс", "с 9.00 до 18.00", "IP 192.168.1.100", "площадь 120 м2, 14 окон",
                               "ошибка E21", "бюджет 8 916 123 руб"])
def test_money_ranges_ip_time_not_phone(s):
    assert masked(s) == s


# --- свои: что важно для писем ---

NOTHING = {"link": 0, "email": 0, "card": 0, "doc": 0, "phone": 0, "handle": 0}


@pytest.mark.parametrize("s", ["р/с 40702810900000000101", "к/с 30101810400000000225", "49 000 ₽", "18 400 руб.",
                               "итого 49 000 ₽ и 18 400 руб.", "сумма 18400 руб", "счёт на 49000₽"])
def test_account_and_sums_untouched(s):
    assert mask_pii(s) == (s, NOTHING)


def test_email_example_domain():
    text, found = mask_pii("ответ на ivan.petrov@sevveter.example")
    assert text == "ответ на [email скрыт]"
    assert found["email"] == 1


def test_phone_test_range():
    text, found = mask_pii("звоните +7 900 555-01-02 до 18:00")
    assert text == "звоните [телефон скрыт] до 18:00"
    assert found["phone"] == 1


def test_letter_masks_contacts_keeps_requisites():
    letter = ("Счёт INV-2026-0412 от ООО «ПринтСнаб» на 18 400 руб., оплатить на р/с 40702810900000000101, "
              "БИК 044525225. Вопросы — maria@zerno-coffee.example, +7 900 555-01-02.")
    text, found = mask_pii(letter)
    assert text == ("Счёт INV-2026-0412 от ООО «ПринтСнаб» на 18 400 руб., оплатить на р/с 40702810900000000101, "
                    "БИК 044525225. Вопросы — [email скрыт], [телефон скрыт].")
    assert found == {**NOTHING, "email": 1, "phone": 1}


def test_counts_all_kinds():
    _, found = mask_pii("t.me/ivan a@b.example 4276 1234 5678 9012 ИНН 7712345678 89991234567 @ivan_petrov")
    assert found == {"link": 1, "email": 1, "card": 1, "doc": 1, "phone": 1, "handle": 1}


def test_none_and_empty():
    assert mask_pii("") == ("", NOTHING)
    assert mask_pii(None) == ("", NOTHING)


# Фактическое поведение JS, не желаемое: любые 13–19 цифр = «карта».
@pytest.mark.parametrize("s, expected", [
    ("р/с 4070 2810 9000 0000 0101", "р/с [номер карты скрыт] 0101"),
    ("ОГРН 1027700132195", "ОГРН [номер карты скрыт]"),
    ("ОГРНИП 304500116000157", "ОГРНИП [номер карты скрыт]"),
])
def test_js_false_positive_card(s, expected):
    assert masked(s) == expected
