"""Маскирование персональных данных до сохранения и до отправки в LLM, а также
повторно на выходе модели.

Перенос maskPII из demo-n8n/src/lib/pii.js: те же правила в том же порядке, те же метки.

Ловит: ссылки на профили (t.me, vk.com, wa.me и др.), email (включая кириллические
и «ivan собака mail точка ru»), номера карт, ИНН/СНИЛС/паспорт РФ, телефоны
(РФ в любых разделителях, международные с +), @ники и ники после «тг:», «telegram» и т.п.
Бюджеты («150 000 руб», «от 80 000-900 000»), даты и короткие числа не трогает.
Расчётный счёт из 20 цифр подряд не трогает.

Не ловит: телефон словами, адреса, ФИО, ник без @ и без слова-маркера,
цифры не из ASCII (полноширинные и т.п.: как и в JS, где \\d = [0-9]).

Ложные срабатывания (как в JS): любое число из 13–19 цифр считается картой,
поэтому ОГРН (13) и ОГРНИП (15) становятся «[номер карты скрыт]», а расчётный
счёт с пробелами («4070 2810 9000 0000 0101») режется на «[номер карты скрыт] 0101».

Перенос с JS: \\p{L} -> [^\\W\\d_], \\p{L}|\\p{N} -> [^\\W_], [\\p{L}\\p{N}_] -> \\w
(в re для str они юникодные, кириллица и ё входят). JS-шный \\d и \\w без флага u
ASCII-шные, поэтому здесь явно [0-9] и [A-Za-z0-9_].
Регистр: re.I в Python считает латинскую i равной турецким İ/ı, а JS с флагами iu — нет
(простое case folding), зато JS сворачивает ſ (U+017F) в s и знак Кельвина (U+212A) в k.
Поэтому латиница в регистронезависимых шаблонах — через (?-i:[iI]) и явные
\\u017f\\u212a в классах.
Единственное известное расхождение: [^\\W\\d_] считает буквой ещё и ², ½, Ⅻ (\\p{No}, \\p{Nl}),
поэтому «a@b.ru²» уйдёт в метку вместе с «²», а «м²ИНН 7712345678» вплотную не замаскируется.

Сложность квадратичная по длине токена без пробелов (как и в JS): 20 КБ base64 одним
словом — около 1,5 с. Длину входа ограничивать до вызова.
"""

from __future__ import annotations

import re
from collections.abc import Callable

LABELS: dict[str, str] = {
    "link": "[ссылка скрыта]",
    "email": "[email скрыт]",
    "card": "[номер карты скрыт]",
    "doc": "[номер документа скрыт]",
    "phone": "[телефон скрыт]",
    "handle": "[ник скрыт]",
}

# ссылки на профили целиком: в них бывают телефоны (wa.me/7999...) и ники
_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog|vk\.com|vk\.ru|m\.vk\.com|wa\.me"
    r"|ap(?-i:[iI])\.whatsapp\.com|(?-i:[iI])nstagram\.com|ok\.ru|facebook\.com|fb\.com)/[^\s,;)\]]+",
    re.IGNORECASE,
)

# email: латиница и кириллица, IDN-домены.
# «++» (possessive, 3.11+) здесь и в _EMAIL_WORDS: следующий символ не из класса, так что
# результат тот же, что у «+» в JS, но без отката по символу на длинных токенах (base64 и т.п.)
_EMAIL = re.compile(r"[\w.%+\-]++@(?:[^\W_]|-)+(?:\.(?:[^\W_]|-)+)*\.[^\W\d_]{2,}")
# email без домена верхнего уровня: «ivan@gmail»
_EMAIL_NO_TLD = re.compile(r"(?<![\w.%+\-])[\w.%+\-]{2,}@(?:[^\W_]|-){2,}(?![^\W_]|[.@])")
# email словами: «ivan собака mail точка ru», «ivan [at] mail [dot] ru»
# (голые «at»/«dot» без скобок не берём: «look at this dot net» — не адрес)
_EMAIL_WORDS = re.compile(
    r"[A-Za-z0-9._%+\-\u017f\u212a]++(?:\s*[(\[]\s*(?i:at|dog|собака)\s*[)\]]\s*|\s+(?i:собака)\s+)[A-Za-z0-9\-\u017f\u212a]+"
    r"(?:(?:\s*[(\[]\s*(?i:dot|точка)\s*[)\]]\s*|\s+(?i:точка)\s+)[A-Za-z\u017f\u212a]{2,})+"
)

# номер карты: 13-19 цифр
_CARD = re.compile(r"(?<![0-9])(?:[0-9][ \-]?){12,18}[0-9](?![0-9])")

# ИНН (10 или 12 цифр) рядом со словом «ИНН»
_INN = re.compile(r"(?<![^\W\d_])(ИНН|(?-i:[iI])nn)(\s*[:№#]?\s*)[0-9]{10}(?:[0-9]{2})?(?![0-9])", re.IGNORECASE)
# СНИЛС: 123-456-789 01
_SNILS = re.compile(r"(?<![0-9])[0-9]{3}[- ][0-9]{3}[- ][0-9]{3}[- ][0-9]{2}(?![0-9])")
# паспорт РФ: 4510 123456, 45 10 123456, «паспорт 4510123456»
_PASSPORT = re.compile(
    r"(?<![0-9])[0-9]{2}[ \u00a0]?[0-9]{2}[ \u00a0]?(?:№[ \u00a0]?)?[0-9]{6}(?![0-9])"
    r"(?![ \u00a0]*(?:руб|р\.|₽|тыс))"
)
_PASSPORT_WORD = re.compile(r"(паспорт\S*\s*(?:серия\s*)?)([0-9]{10})(?![0-9])", re.IGNORECASE)

_PHONE_PATTERNS = (
    # РФ: +7 / 7 / 8, затем 10 цифр с пробелами, дефисами, скобками
    re.compile(r"(?<![0-9+])(?:\+7|8|7)[ \-]?\(?[0-9]{3}\)?[ \-]?[0-9]{3}[ \-]?[0-9]{2}[ \-]?[0-9]{2}(?![0-9])"),
    # мобильный РФ без префикса: 9XX XXX XX XX
    re.compile(r"(?<![0-9+])9[0-9]{2}[ \-]?[0-9]{3}[ \-]?[0-9]{2}[ \-]?[0-9]{2}(?![0-9])"),
    # международный с плюсом
    re.compile(r"(?<![0-9+])\+[0-9]{1,3}[ \-]?\(?[0-9]{1,4}\)?(?:[ \-]?[0-9]{2,4}){2,4}(?![0-9])"),
)

# разделитель между цифрами телефона: пробел, точка, дефис, скобка (с пробелами вокруг)
_SEP = r"(?:[ \u00a0]?[().\-\u2013][ \u00a0]?|[ \u00a0])"
_DIGIT_RUN = re.compile(r"(?<![\w+])\+?\(?[0-9](?:" + _SEP + r"?\(?[0-9]\)?)*")
# после серии цифр идёт валюта или «тыс»: это сумма, не телефон
_MONEY_AFTER = re.compile(r"\s*(?:руб|р\.|₽|тыс|т\.р|млн|к(?![а-яё]))", re.IGNORECASE)

# @ник
_HANDLE = re.compile(r"(?<![A-Za-z0-9_@.])@[A-Za-z][A-Za-z0-9_]{4,31}(?![A-Za-z0-9_])")
# ник после слова-маркера: «tg: ivan_petrov», «телеграм ivan_petrov», «инста ivan.petrov».
# Маркер — отдельное слово: иначе «installation» и «инструмент Makita» теряли бы слово
_HANDLE_AFTER_MARKER = re.compile(
    r"(?<!\w)((?i:tg|тг|телег[^\W\d_]*|telegram|инст(?:а|у|ой|е|аграм[^\W\d_]*)|(?-i:[iI])nsta(?:gram)?|skype"
    r"|скайп[^\W\d_]*|ник))(?!\w)(\s*[:\-—]?\s*)([A-Za-z\u017f\u212a][A-Za-z0-9_.\u017f\u212a]{3,31})(?![A-Za-z0-9_\u017f\u212a])"
)


def _digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s)


def _is_money_range(run: str) -> bool:
    """«80 000 - 900 000», «15 000–20 000»: диапазон сумм с группами по три цифры, не телефон."""
    parts = re.split(r"\s*[-\u2013]\s*", run)
    return len(parts) >= 2 and all(re.fullmatch(r"[0-9]{1,3}(?:[ \u00a0][0-9]{3})+", p.strip()) for p in parts)


def _looks_like_phone(run: str) -> bool:
    digits = _digits(run)
    if len(digits) == 11 and digits[0] in "78":
        return True
    if len(digits) == 11 and run.strip().startswith("+"):
        return True
    if len(digits) == 10 and digits[0] == "9":
        return True
    # (495)123-45-67: код города в скобках без префикса
    if len(digits) == 10 and re.match(r"\(\s*[0-9](?:[ \u00a0]?[0-9]){2}\s*\)", run.strip()):
        return True
    return False


def _mask_run(run: str, label: Callable[[], str]) -> str:
    """В серии цифр ищет телефон: вся серия или окно из соседних групп цифр
    (телефон, за которым через пробел идёт ещё число). Возвращает серию с метками."""
    groups = [(g.start(), g.end()) for g in re.finditer(r"[0-9]+", run)]

    def start(i: int) -> int:
        s = groups[i][0]
        while s > 0 and (run[s - 1] in "(+" or (s >= 2 and run[s - 1] == " " and run[s - 2] == "+")):
            s -= 1
        return s

    out: list[str] = []
    pos = 0
    i = 0
    while i < len(groups):
        digits = 0
        for j in range(i, len(groups)):
            digits += groups[j][1] - groups[j][0]
            if digits > 11:
                break
            if digits < 10:
                continue
            end = groups[j][1]
            if end < len(run) and run[end] == ")":
                end += 1
            s = start(i)
            span = run[s:end]
            if _looks_like_phone(span) and not _is_money_range(span):
                out.append(run[pos:s])
                out.append(label())
                pos = end
                i = j
                break
        i += 1
    return run if pos == 0 else "".join(out) + run[pos:]


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """Возвращает текст с метками вместо персональных данных и счётчики найденного по видам."""
    text = "" if text is None else str(text)
    found = dict.fromkeys(LABELS, 0)

    def put(kind: str) -> str:
        found[kind] += 1
        return LABELS[kind]

    text = _LINK.sub(lambda m: put("link"), text)

    text = _EMAIL.sub(lambda m: put("email"), text)
    text = _EMAIL_NO_TLD.sub(lambda m: put("email"), text)
    text = _EMAIL_WORDS.sub(lambda m: put("email"), text)

    text = _CARD.sub(lambda m: put("card") if 13 <= len(_digits(m[0])) <= 19 else m[0], text)

    text = _INN.sub(lambda m: m[1] + m[2] + put("doc"), text)
    text = _SNILS.sub(lambda m: put("doc"), text)
    # без пробела между частями это скорее телефон или сумма: берём только с «паспорт» рядом
    text = _PASSPORT.sub(lambda m: put("doc") if re.search(r"\s|№", m[0]) else m[0], text)
    text = _PASSPORT_WORD.sub(lambda m: m[1] + put("doc"), text)

    for pattern in _PHONE_PATTERNS:
        text = pattern.sub(lambda m: put("phone") if 10 <= len(_digits(m[0])) <= 15 else m[0], text)

    # остальные записи телефона: «(916)123-45-67», «+7.916.123.45.67», «8 9 1 6 1 2 3 4 5 6 7»,
    # «8 (4 9 5) 123-45-67». Берём серию цифр с разделителями и смотрим на число цифр и префикс.
    def digit_run(m: re.Match[str]) -> str:
        after = m.string[m.end():m.end() + 12]
        if _MONEY_AFTER.match(after):
            return m[0]
        return _mask_run(m[0], lambda: put("phone"))

    text = _DIGIT_RUN.sub(digit_run, text)

    text = _HANDLE.sub(lambda m: put("handle"), text)
    text = _HANDLE_AFTER_MARKER.sub(lambda m: m[1] + m[2] + put("handle"), text)

    return text, found
