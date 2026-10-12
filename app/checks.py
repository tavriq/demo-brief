"""Checks by code: the spec (K1–K12), the prototype (P1–P5), acceptance of "screen"/"field"
criteria, the prototype's screen text for the reviewer and the spec rendered as text.

See docs/checklist.md and docs/template.md. Pure functions, no I/O, stdlib only.

An issue is ``{"id": "K5", "level": "error"|"warn", "text": "…"}``; the text is Russian, names
the ids involved and goes to the spec's author as a remark. Model output is untrusted: any field
may have a wrong type, so every check reads through the tolerant helpers below, never raises and
reports an error instead. Model answers are not edited, only arrays are cut by ``schema.LIMITS``.

Beyond the checklist, on purpose: K6 also checks that ``acceptance[].scenario`` (when set)
names an existing scenario; K3/K4 accept a scenario's role written as the role's name instead of
its id; P5 also warns about ``data-go`` leading to a missing screen; P3 only warns about links
with a scheme that loads nothing (javascript:, mailto:, tel:): the P3 error means network, and a
prototype with it is not served (app.pipeline).
"""

from __future__ import annotations

import math
import re
from html.parser import HTMLParser

from app.schema import LIMITS, SPEC

Issue = dict

MAX_PROTO_BYTES = 80_000

_ID_FORMAT = {"roles": "R", "scenarios": "S", "nfr": "N", "acceptance": "A"}
_SECTION_NAMES = {
    "roles": "Роли", "scenarios": "Сценарии", "data": "Данные", "integrations": "Интеграции",
    "nfr": "Нефункциональные требования", "acceptance": "Критерии приёмки", "risks": "Риски",
    "assumptions": "Допущения", "questions": "Вопросы клиенту",
}
# K1: sections that must have at least one item (integrations may be empty).
_REQUIRED_LISTS = ("roles", "scenarios", "data", "nfr", "acceptance", "risks")
_OPTIONAL_LISTS = ("integrations", "assumptions", "questions")

_LIMIT_NAMES = {
    "parse.roles": "Разбор, роли", "parse.assumptions": "Разбор, допущения",
    "parse.questions": "Разбор, вопросы клиенту", "parse.out_of_scope": "Разбор, что не входит",
    "roles": "Роли", "scenarios": "Сценарии", "scenarios.steps": "Шаги сценария {owner}", "data": "Данные",
    "data.fields": "Поля сущности «{owner}»", "integrations": "Интеграции",
    "nfr": "Нефункциональные требования", "acceptance": "Критерии приёмки", "risks": "Риски",
    "estimate.items": "Части оценки", "assumptions": "Допущения", "questions": "Вопросы клиенту",
    "review.items": "Пункты ревьюера",
}

# K7: vague stems at a word start (an optional «не» before), case-insensitive. Lookaheads drop
# words that only share the stem: легковой, проставить, пространство, простыня, простить, простуда.
_VAGUE = re.compile(
    r"(?<![^\W\d_])(?:не)?(?:удобн|быстр|интуитивн|современ|качествен|красив|понятн|л[её]гк(?!ов)"
    r"|оптимальн|эффективн|над[её]жн|прост(?!ав|ын|и|уд|р))[^\W\d_]*",
    re.IGNORECASE,
)
# K7/K8: a number, not a digit glued to a letter: «S1», «A3», «R2» are ids, not measures.
_NUMBER = re.compile(r"(?<![^\W_])\d")


# --- tolerant readers -------------------------------------------------------------------------

def issue(id_: str, level: str, text: str) -> Issue:
    return {"id": id_, "level": level, "text": text}


def _err(id_: str, text: str) -> Issue:
    return issue(id_, "error", text)


def _fmt_num(x: float) -> str:
    """3.0 -> «3», 2.5 -> «2,5»."""
    return f"{x:g}".replace(".", ",")


def _str(v) -> str:
    """A stripped string; a number becomes its text; anything else is empty."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return _fmt_num(float(v)) if isinstance(v, float) else str(v)
        except (OverflowError, ValueError):
            return ""
    return ""


def _line(v, limit: int = 0) -> str:
    """_str with inner whitespace collapsed, optionally cut to ``limit`` characters."""
    s = re.sub(r"\s+", " ", _str(v))
    return s[:limit - 1] + "…" if limit and len(s) > limit else s


def _num(v) -> float | None:
    """A finite number from a number or a numeric string («3», «2,5»), else None."""
    if isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)):
            x = float(v)
        elif isinstance(v, str):
            x = float(v.strip().replace(",", ".").replace(" ", "").replace(" ", ""))
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _type_name(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "да/нет"
    if isinstance(v, str):
        return "строка"
    if isinstance(v, (int, float)):
        return "число"
    if isinstance(v, list):
        return "список"
    if isinstance(v, dict):
        return "объект"
    return type(v).__name__


def _get(d, key):
    return d.get(key) if isinstance(d, dict) else None


def _items(spec: dict, key: str) -> list[tuple[int, dict]]:
    """(index, item) for the dict items of a list section; other items are skipped (K1 reports)."""
    value = _get(spec, key)
    if not isinstance(value, list):
        return []
    return [(i, item) for i, item in enumerate(value) if isinstance(item, dict)]


def _id(item: dict, key: str = "id") -> str:
    """A string id (or name) stripped; a number or anything else is no id (K2 reports it)."""
    v = item.get(key)
    return v.strip() if isinstance(v, str) else ""


def _label(item: dict, i: int, key: str = "id", limit: int = 40) -> str:
    """The item's id (or another key) for a message, «№3» when there is none."""
    return _line(_id(item, key), limit) or f"№{i + 1}"


def _ids(spec: dict, key: str) -> list[str]:
    return [s for _, item in _items(spec, key) if (s := _id(item))]


def _field_names(spec: dict) -> set[str]:
    """«Сущность.поле» for every field in the data section, exactly as written (stripped)."""
    names = set()
    for _, item in _items(spec, "data"):
        entity = _str(item.get("entity"))
        fields = item.get("fields")
        if entity and isinstance(fields, list):
            names.update(f"{entity}.{f}" for f in map(_str, fields) if f)
    return names


# --- truncation by LIMITS (K12) ---------------------------------------------------------------

def _cut(node: dict, parts: list[str], limit: int, name: str, owner: str, out: list[Issue]) -> dict:
    """``node`` (or a shallow copy) with the array at ``parts`` cut to ``limit``; input untouched."""
    head, rest = parts[0], parts[1:]
    child = node.get(head)
    if not rest:
        if isinstance(child, list) and len(child) > limit:
            where = name.format(owner=owner)
            out.append(issue("K12", "warn", f"{where}: пунктов {len(child)}, предел {limit} — лишние отброшены"))
            return {**node, head: child[:limit]}
        return node
    if isinstance(child, dict):
        new = _cut(child, rest, limit, name, owner, out)
        return node if new is child else {**node, head: new}
    if isinstance(child, list):
        new_list = [_cut(item, rest, limit, name, _label(item, i, "entity" if head == "data" else "id"), out)
                    if isinstance(item, dict) else item for i, item in enumerate(child)]
        changed = any(a is not b for a, b in zip(new_list, child))
        return {**node, head: new_list} if changed else node
    return node


def _truncate(obj: dict, keys: list[str], prefix: str) -> tuple[dict, list[Issue]]:
    out: list[Issue] = []
    obj = dict(obj)
    # outer arrays first: steps of a scenario that is cut away are not reported
    for key in sorted(keys, key=lambda k: k.count(".")):
        parts = key[len(prefix):].split(".")
        obj = _cut(obj, parts, LIMITS[key], _LIMIT_NAMES.get(key, key), "", out)
    return obj, out


def truncate_parse(parse: dict) -> tuple[dict, list[Issue]]:
    """The brief's parse with arrays cut by LIMITS "parse.*"; K12 warnings for each cut."""
    if not isinstance(parse, dict):
        return {}, []
    return _truncate(parse, [k for k in LIMITS if k.startswith("parse.")], "parse.")


def truncate_review(review: dict) -> tuple[dict, list[Issue]]:
    """The reviewer's answer with ``items`` cut by LIMITS "review.items"."""
    if not isinstance(review, dict):
        return {}, []
    return _truncate(review, ["review.items"], "review.")


_SPEC_KEYS = set(SPEC["schema"]["properties"])


def _truncate_spec(spec: dict) -> tuple[dict, list[Issue]]:
    return _truncate(spec, [k for k in LIMITS if k.split(".")[0] in _SPEC_KEYS], "")


# --- spec checks K1–K11 -----------------------------------------------------------------------

def _k1(spec: dict) -> list[Issue]:
    out = []
    goal = spec.get("goal")
    if not isinstance(goal, dict):
        out.append(_err("K1", f"Не заполнен раздел «Цель»: ждали объект с text и metric, пришло: {_type_name(goal)}"))
    else:
        if not _str(goal.get("text")):
            out.append(_err("K1", "В разделе «Цель» нет текста цели"))
        if not _str(goal.get("metric")):
            out.append(_err("K1", "В разделе «Цель» нет метрики: по какой цифре поймём, что цель достигнута"))
    for key in _REQUIRED_LISTS + _OPTIONAL_LISTS:
        name = _SECTION_NAMES[key]
        value = spec.get(key)
        if value is None or value == []:
            if key in _REQUIRED_LISTS:
                out.append(_err("K1", f"Не заполнен раздел «{name}»"))
            continue
        if not isinstance(value, list):
            out.append(_err("K1", f"Раздел «{name}» должен быть списком, пришло: {_type_name(value)}"))
            continue
        want = str if key in ("assumptions", "questions") else dict
        bad = [str(i + 1) for i, item in enumerate(value) if not isinstance(item, want)]
        if bad:
            kind = "строка" if want is str else "объект"
            out.append(_err("K1", f"Раздел «{name}»: пункты №{', №'.join(bad)} — не {kind}"))
    estimate = spec.get("estimate")
    if not isinstance(estimate, dict):
        out.append(_err("K1", f"Не заполнен раздел «Оценка»: ждали объект с items и total_days, "
                              f"пришло: {_type_name(estimate)}"))
    else:
        items = estimate.get("items")
        if not isinstance(items, list) or not items:
            out.append(_err("K1", "В разделе «Оценка» нет частей работы (items)"))
        else:
            bad = [str(i + 1) for i, item in enumerate(items) if not isinstance(item, dict)]
            if bad:
                out.append(_err("K1", f"Раздел «Оценка»: части №{', №'.join(bad)} — не объект"))
    return out


def _k2(spec: dict) -> list[Issue]:
    out = []
    for key, letter in _ID_FORMAT.items():
        name = _SECTION_NAMES[key]
        seen: set[str] = set()
        for i, item in _items(spec, key):
            raw = item.get("id")
            if not isinstance(raw, str) or not raw.strip():
                out.append(_err("K2", f"Раздел «{name}», пункт №{i + 1}: нет id (нужен вида {letter}1)"))
                continue
            id_ = raw.strip()
            if not re.fullmatch(letter + r"[1-9][0-9]*", id_):
                hint = " (буква кириллицей)" if re.search(r"[А-Яа-яЁё]", id_) else ""
                out.append(_err("K2", f"Раздел «{name}»: id «{_line(id_, 20)}» не по формату {letter}1, "
                                      f"{letter}2…{hint}"))
            if id_ in seen:
                out.append(_err("K2", f"Раздел «{name}»: id {_line(id_, 20)} повторяется"))
            seen.add(id_)
    return out


def _role_map(spec: dict) -> dict[str, str]:
    """Role id -> id and role name (casefolded) -> id, so a scenario may name its role either way."""
    by = {}
    for _, item in _items(spec, "roles"):
        id_ = _id(item)
        if not id_:
            continue
        by.setdefault(id_, id_)
        name = _str(item.get("name")).casefold()
        if name:
            by.setdefault(name, id_)
    return by


def _k3_k4(spec: dict) -> list[Issue]:
    out = []
    roles = _role_map(spec)
    used: set[str] = set()
    for i, sc in _items(spec, "scenarios"):
        label = _label(sc, i)
        role = sc.get("role")
        if role is not None and not isinstance(role, str):
            out.append(_err("K3", f"У сценария {label} роль должна быть одна (строка), пришло: {_type_name(role)}"))
            continue
        role = _str(role)
        if not role:
            out.append(_err("K3", f"У сценария {label} не указана роль"))
        elif role in roles or role.casefold() in roles:
            used.add(roles.get(role) or roles[role.casefold()])
        else:
            out.append(_err("K3", f"У сценария {label} роль «{_line(role, 40)}» — такой нет в разделе «Роли»"))
    for _, item in _items(spec, "roles"):
        id_ = _id(item)
        if id_ and id_ not in used:
            name = _line(item.get("name"), 40)
            out.append(_err("K4", f"У роли {id_}{f' («{name}»)' if name else ''} нет ни одного сценария"))
    return out


def _k5_k6(spec: dict) -> list[Issue]:
    out = []
    scenarios = _ids(spec, "scenarios")
    known = set(scenarios)
    fields = _field_names(spec)
    covered: set[str] = set()
    for i, item in _items(spec, "acceptance"):
        label = _label(item, i)
        sc_raw, check = item.get("scenario"), item.get("check")
        target = _str(item.get("target"))
        if sc_raw is not None and not isinstance(sc_raw, str):
            out.append(_err("K6", f"У критерия {label} сценарий должен быть строкой или null, "
                                  f"пришло: {_type_name(sc_raw)}"))
        elif _str(sc_raw):
            if _str(sc_raw) in known:
                covered.add(_str(sc_raw))
            else:
                out.append(_err("K6", f"Критерий {label} привязан к сценарию «{_line(sc_raw, 20)}», его нет"))
        if target in known:
            covered.add(target)
        if check == "screen":
            if not target:
                out.append(_err("K6", f"У критерия {label} (screen) не указан сценарий в target"))
            elif target not in known:
                out.append(_err("K6", f"Критерий {label} (screen) ссылается на сценарий «{_line(target, 20)}», "
                                      f"его нет"))
        elif check == "field":
            if not target:
                out.append(_err("K6", f"У критерия {label} (field) не указано поле в target (Сущность.поле)"))
            elif target not in fields:
                out.append(_err("K6", f"Критерий {label} (field) ссылается на поле «{_line(target, 40)}», "
                                      f"его нет в разделе «Данные» (нужно Сущность.поле, как там написано)"))
        elif check == "review":
            if target:
                out.append(_err("K6", f"У критерия {label} (review) target должен быть пустым, "
                                      f"а там «{_line(target, 40)}»"))
        else:
            shown = _line(check, 20) if isinstance(check, str) else _type_name(check)
            out.append(_err("K6", f"У критерия {label} неизвестный способ проверки «{shown}»: "
                                  f"нужно screen, field или review"))
    for sid in scenarios:
        if sid not in covered:
            out.append(_err("K5", f"У сценария {sid} нет критерия приёмки"))
    return out


def _k7(spec: dict) -> list[Issue]:
    out = []
    for i, item in _items(spec, "acceptance"):
        text = _str(item.get("text"))
        if not text:
            out.append(_err("K7", f"У критерия {_label(item, i)} нет текста"))
            continue
        if _NUMBER.search(text):
            continue
        words = list(dict.fromkeys(m[0].lower() for m in _VAGUE.finditer(text)))
        if words:
            shown = ", ".join(f"«{w}»" for w in words[:3])
            out.append(_err("K7", f"Критерий {_label(item, i)} расплывчатый: {shown} без числа — "
                                  f"нельзя проверить «да / нет»"))
    return out


def _k8(spec: dict) -> list[Issue]:
    out = []
    metric = _str(_get(spec.get("goal"), "metric"))
    if metric and not _NUMBER.search(metric):
        out.append(_err("K8", f"В метрике цели нет числа: «{_line(metric, 60)}»"))
    for i, item in _items(spec, "nfr"):
        label = _label(item, i)
        m = _str(item.get("metric"))
        if not m:
            out.append(_err("K8", f"У НФТ {label} нет метрики"))
        elif not _NUMBER.search(m):
            out.append(_err("K8", f"В метрике НФТ {label} нет числа: «{_line(m, 60)}»"))
    return out


def _k9(spec: dict) -> list[Issue]:
    estimate = spec.get("estimate")
    if not isinstance(estimate, dict):
        return []  # K1
    out = []
    total = 0.0
    sum_ok = True
    for i, item in _items(estimate, "items"):
        part = _line(item.get("part"), 40) or f"№{i + 1}"
        days = _num(item.get("days"))
        if days is None:
            sum_ok = False
            out.append(_err("K9", f"Часть оценки «{part}»: дни не число ({_type_name(item.get('days'))})"))
        elif days <= 0:
            out.append(_err("K9", f"Часть оценки «{part}»: дней {_fmt_num(days)}, нужно больше 0"))
        if days is not None:
            total += days
    declared = _num(estimate.get("total_days"))
    if declared is None:
        out.append(_err("K9", f"В оценке total_days не число ({_type_name(estimate.get('total_days'))})"))
    elif sum_ok and _items(estimate, "items") and abs(total - declared) > 0.01:
        out.append(_err("K9", f"Оценка не сходится: части дают {_fmt_num(round(total, 2))} дн., "
                              f"а total_days = {_fmt_num(declared)}"))
    return out


def _k10(spec: dict) -> list[Issue]:
    out = []
    for i, item in _items(spec, "risks"):
        text = _line(item.get("text"), 60) or f"№{i + 1}"
        if not _str(item.get("mitigation")):
            out.append(_err("K10", f"У риска «{text}» не написано, что делаем"))
    for i, item in _items(spec, "integrations"):
        system = _line(item.get("system"), 40) or f"№{i + 1}"
        if not _str(item.get("fallback")):
            out.append(_err("K10", f"У интеграции «{system}» нет запасного пути на случай недоступности"))
    return out


def _k11(spec: dict) -> list[Issue]:
    out = []
    for i, sc in _items(spec, "scenarios"):
        label = _label(sc, i)
        steps = sc.get("steps")
        if not isinstance(steps, list):
            out.append(_err("K11", f"У сценария {label} шаги должны быть списком, пришло: {_type_name(steps)}"))
        else:
            n = sum(1 for s in steps if _str(s))
            if n < 2:
                out.append(_err("K11", f"В сценарии {label} шагов {n}, нужно хотя бы 2"))
        if not _str(sc.get("result")):
            out.append(_err("K11", f"У сценария {label} не указан результат"))
    return out


def check_spec(spec: dict) -> tuple[dict, list[Issue]]:
    """The spec cut by LIMITS and the issues K1–K12 (docs/checklist.md), K1 first."""
    if not isinstance(spec, dict):
        return {}, [_err("K1", f"ТЗ должно быть объектом JSON, пришло: {_type_name(spec)}")]
    spec, warns = _truncate_spec(spec)
    issues: list[Issue] = []
    for check in (_k1, _k2, _k3_k4, _k5_k6, _k7, _k8, _k9, _k10, _k11):
        issues += check(spec)
    issues.sort(key=lambda x: int(x["id"][1:]))
    return spec, issues + warns


def is_red_by_code(issues) -> bool:
    """True if any issue is an error (warnings do not make the spec red)."""
    return isinstance(issues, list) and any(isinstance(x, dict) and x.get("level") == "error" for x in issues)


# --- the prototype ----------------------------------------------------------------------------

_FENCE = re.compile(r"```[ \t]*html[ \t]*\r?\n(.*?)(?:```|\Z)", re.IGNORECASE | re.DOTALL)
_HTML_START = re.compile(r"<!doctype|<html", re.IGNORECASE)
_HTML_END = re.compile(r"</html\s*>", re.IGNORECASE)


def extract_html(text: str) -> str | None:
    """HTML from a model answer: a ```html block (an unclosed one runs to the end), else from
    <!doctype or <html to the last </html> (or to the end, if the answer was cut), else None."""
    if not isinstance(text, str):
        return None
    for m in _FENCE.finditer(text):
        body = m[1].strip()
        if "<" in body:
            return body
    start = _HTML_START.search(text)
    if not start:
        return None
    ends = list(_HTML_END.finditer(text, start.start()))
    html = text[start.start():ends[-1].end()] if ends else text[start.start():].rstrip().rstrip("`")
    return html.strip() or None


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
         "track", "wbr", "frame", "keygen"}
# attributes that make the browser load or send something
_URL_ATTRS = {"src", "href", "xlink:href", "action", "formaction", "poster", "background", "ping", "manifest",
              "data", "codebase", "lowsrc", "dynsrc", "longdesc", "srcset", "imagesrcset"}
_TEXT_INPUTS = {"", "text", "email", "tel", "number", "date", "time", "datetime-local", "month", "week", "search",
                "url", "submit", "button", "reset"}
_URL_STRIP = "".join(map(chr, range(33)))
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")
_NET_SCHEMES = {"http", "https", "ftp", "ws", "wss"}
# schemes that load nothing: the sandbox does not open them (a P3 warning, not "network")
_DEAD_SCHEMES = {"javascript", "mailto", "tel", "sms", "about"}

_NET_TAGS = re.compile(r"<(iframe|frame|frameset|object|embed|applet|base|portal)(?=[\s/>])", re.IGNORECASE)
_META_REFRESH = re.compile(r"<meta\b[^>]*?http-equiv\s*=\s*[\"']?\s*refresh", re.IGNORECASE)
_NET_JS = (
    (re.compile(r"\bfetch\s*\("), "fetch("),
    (re.compile(r"\bXMLHttpRequest\b"), "XMLHttpRequest"),
    (re.compile(r"\bWebSocket\b"), "WebSocket"),
    (re.compile(r"\bEventSource\b"), "EventSource"),
    (re.compile(r"\bsendBeacon\b"), "sendBeacon"),
    (re.compile(r"\bimportScripts\b"), "importScripts"),
    (re.compile(r"\bnew\s+(?:Shared)?Worker\b"), "new Worker"),
    (re.compile(r"\bserviceWorker\b"), "serviceWorker"),
    (re.compile(r"\bRTCPeerConnection\b"), "RTCPeerConnection"),
    (re.compile(r"\bimport\s*\("), "import()"),
)
# lower case only: JS «new URL(…)» is not CSS (an address in a script is caught by _SCRIPT_URL)
_CSS_URL = re.compile(r"(?<![\w.$])url\(\s*[\"']?\s*([^\"')\s]*)")
_CSS_IMPORT = re.compile(r"@import\b", re.IGNORECASE)
# a network address inside a script; the SVG namespace (createElementNS) is not a request
_SCRIPT_URL = re.compile(r"\b(?:https?|wss?|ftp):(?:\\?/){2}(?!www\.w3\.org/)", re.IGNORECASE)


def _url_kind(value: str) -> str | None:
    """None for data:, #anchors and relative URLs; else what makes the URL leave the page."""
    v = re.sub(r"[\t\n\r]", "", value).strip(_URL_STRIP)
    if re.match(r"[/\\]{2}", v):
        return "//"
    m = _SCHEME.match(v)
    if not m or m[1].lower() == "data":
        return None
    return m[1].lower()


def _srcset_urls(value: str) -> list[str]:
    """Candidate URLs of a srcset: the first token after the start or a comma. A comma inside a
    data: URL yields a base64 tail, which reads as a harmless relative URL."""
    return re.findall(r"(?:^|,)\s*([^\s,]+)", value)


class _Scan(HTMLParser):
    """One pass over the prototype: body, screens, transitions, fields, network, screen text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.has_body = False
        self.screens: list[str] = []
        self.gos: list[str] = []
        self.fields: list[str] = []
        self.net: list[str] = []
        self.dead: list[str] = []
        self.scripts: list[str] = []
        self.texts: dict[str, list[str]] = {}
        self._stack: list[tuple[str, str | None]] = []
        self._raw: str | None = None   # inside <script> or <style>

    def _screen(self) -> str | None:
        for _, screen in reversed(self._stack):
            if screen:
                return screen
        return None

    def _text(self, s: str | None) -> None:
        screen = self._screen()
        if screen and s and s.strip():
            self.texts.setdefault(screen, []).append(s.strip())

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> tuple[str | None, dict]:
        if tag == "body":
            self.has_body = True
        screen = None
        a = {}
        for name, value in attrs:
            value = value or ""
            a.setdefault(name, value)
            if name == "data-screen" and value.strip():
                screen = value.strip()
                self.screens.append(screen)
                self.texts.setdefault(screen, [])
            elif name == "data-go":
                self.gos.append(value.strip())
            elif name == "data-field":
                self.fields.append(value.strip())
            elif name.startswith("on"):
                self.scripts.append(value)
            if name in _URL_ATTRS:
                urls = _srcset_urls(value) if name.endswith("srcset") else [value]
                for kind in filter(None, map(_url_kind, urls)):
                    if kind in _NET_SCHEMES or kind == "//":
                        self.net.append(f"внешний {name} у <{tag}>")
                    elif kind in _DEAD_SCHEMES:
                        self.dead.append(f"{name} со схемой {kind}: у <{tag}>")
                        if kind == "javascript":
                            self.scripts.append(value)  # an address inside is still caught
                    else:
                        self.net.append(f"{name} со схемой {kind}: у <{tag}>")
        return screen, a

    def _visible_attrs(self, tag: str, a: dict) -> None:
        for name in ("aria-label", "alt", "placeholder"):
            self._text(a.get(name))
        if tag == "input" and a.get("type", "").lower() in _TEXT_INPUTS:
            self._text(a.get("value"))

    def handle_starttag(self, tag, attrs):
        screen, a = self._open(tag, attrs)
        if tag not in _VOID:
            self._stack.append((tag, screen))
        if tag in ("script", "style"):
            self._raw = tag
        self._visible_attrs(tag, a)

    def handle_startendtag(self, tag, attrs):
        screen, a = self._open(tag, attrs)
        self._stack.append((tag, screen))   # so attribute text lands in its own screen
        self._visible_attrs(tag, a)
        self._stack.pop()

    def handle_endtag(self, tag):
        if tag == self._raw:
            self._raw = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._raw == "script":
            self.scripts.append(data)
        elif self._raw is None:
            self._text(data)


def _scan(html: str) -> _Scan | None:
    scan = _Scan()
    try:
        scan.feed(html)
        scan.close()
    except Exception:   # html.parser is lenient; anything here means the HTML is broken (P1)
        return None
    return scan


def _net_kinds(html: str, scan: _Scan | None) -> list[str]:
    kinds = list(scan.net) if scan else []
    kinds += [f"<{m[1].lower()}>" for m in _NET_TAGS.finditer(html)]
    if _META_REFRESH.search(html):
        kinds.append('<meta http-equiv="refresh">')
    kinds += [label for pattern, label in _NET_JS if pattern.search(html)]
    for m in _CSS_URL.finditer(html):
        if _url_kind(m[1]):
            kinds.append("url(…) наружу в CSS")
    if _CSS_IMPORT.search(html):
        kinds.append("@import в CSS")
    if scan and any(_SCRIPT_URL.search(s) for s in scan.scripts):
        kinds.append("адрес http(s) в скрипте")
    return list(dict.fromkeys(kinds))


def _bytes(html: str) -> int:
    return len(html.encode("utf-8", "replace"))


def check_prototype(html: str, spec: dict) -> list[Issue]:
    """P1–P5 (docs/checklist.md): P1–P4 are errors, P5 is a warning."""
    if not isinstance(html, str) or not html.strip():
        return [_err("P1", "Прототипа нет: в ответе модели не нашёлся HTML")]
    out: list[Issue] = []
    scan = _scan(html)
    if scan is None:
        out.append(_err("P1", "HTML прототипа не разбирается парсером"))
    elif not scan.has_body:
        out.append(_err("P1", "В прототипе нет <body>"))
    size = _bytes(html)
    if size > MAX_PROTO_BYTES:
        out.append(_err("P2", f"Прототип {size:,} байт — больше предела {MAX_PROTO_BYTES:,}".replace(",", " ")))
    kinds = _net_kinds(html, scan)
    if kinds:
        shown = ", ".join(kinds[:8]) + (f" и ещё {len(kinds) - 8}" if len(kinds) > 8 else "")
        out.append(_err("P3", f"Прототип обращается в сеть: {shown}"))
    links = list(dict.fromkeys(scan.dead)) if scan else []
    if links:
        out.append(issue("P3", "warn", f"Ссылки, которые в песочнице не откроются: {', '.join(links[:5])}"))
    screens = set(scan.screens) if scan else set()
    scenarios = _ids(spec, "scenarios")
    missing = [s for s in scenarios if s not in screens]
    if not screens:
        out.append(_err("P4", 'В прототипе нет ни одного экрана (элемент с data-screen="S1")'))
    elif missing:
        out.append(_err("P4", f"Нет экрана для сценариев {', '.join(missing)} "
                              f'(нужен элемент data-screen="{missing[0]}")'))
    gos = [g for g in (scan.gos if scan else []) if g]
    if screens and not gos:
        out.append(issue("P5", "warn", 'Нет переходов между экранами: ни одного элемента с data-go="S…"'))
    dead = list(dict.fromkeys(g for g in gos if g not in screens))
    if screens and dead:
        out.append(issue("P5", "warn", f"data-go ведёт на несуществующие экраны: {', '.join(map(_line, dead[:5]))}"))
    return out


def _fair_cap(lengths: list[int], total: int) -> int:
    """The largest cap c with sum(min(l, c)) <= total: short screens keep all, long share the rest."""
    if sum(lengths) <= total:
        return max(lengths, default=0)
    remaining, n = total, len(lengths)
    for i, length in enumerate(sorted(lengths)):
        share = remaining // (n - i)
        if length > share:
            return share
        remaining -= length
    return remaining


def _cut_text(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    return s[:cap - 1].rstrip() + "…" if cap > 1 else ""


def screens_text(html: str, per_screen: int = 1500, total: int = 8000) -> dict[str, str]:
    """data-screen id -> its visible text (labels, placeholders, button text; no tags, scripts or
    styles), at most ``per_screen`` characters each and ``total`` in all, in document order. When
    the total is short, every screen gets a fair share instead of the last ones getting nothing."""
    if not isinstance(html, str):
        return {}
    scan = _scan(html)
    if scan is None:
        return {}
    texts = {k: _cut_text(re.sub(r"\s+", " ", " ".join(v)).strip(), max(per_screen, 0))
             for k, v in scan.texts.items()}
    cap = _fair_cap([len(t) for t in texts.values()], max(total, 0))
    return {k: _cut_text(t, cap) for k, t in texts.items()}


def accept_by_code(spec: dict, html: str | None) -> list[dict]:
    """Acceptance criteria checked by code: "screen" (a data-screen for the scenario) and "field"
    (a data-field with the name). No prototype — every such criterion fails."""
    scan = _scan(html) if isinstance(html, str) and html.strip() else None
    screens = set(scan.screens) if scan else set()
    fields = set(scan.fields) if scan else set()
    out = []
    for i, item in _items(spec, "acceptance"):
        check = item.get("check")
        if check not in ("screen", "field"):
            continue
        target = _str(item.get("target"))
        row = {"id": _label(item, i), "text": _str(item.get("text")), "by": "code"}
        if scan is None:
            out.append({**row, "ok": False, "note": "прототипа нет"})
        elif not target:
            out.append({**row, "ok": False, "note": "в критерии не указано, что искать (target пустой)"})
        elif check == "screen":
            ok = target in screens
            out.append({**row, "ok": ok, "note": f"экран {target} есть" if ok else f"экрана {target} нет в прототипе"})
        else:
            ok = target in fields
            out.append({**row, "ok": ok, "note": f"поле «{target}» есть" if ok
                        else f"поля «{target}» нет в прототипе (data-field)"})
    return out


# --- the spec as text -------------------------------------------------------------------------

_DIRECTIONS = {"in": "в систему", "out": "из системы", "both": "в обе стороны"}


def _dot(s: str) -> str:
    """``s`` ending in a stop, without doubling one the model already put («…администратор.» stays as is)."""
    return s if s.endswith((".", "!", "?", "…")) else s + "."


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {s}" for s in lines) if lines else "Нет."


def _strings(spec: dict, key: str) -> list[str]:
    value = _get(spec, key)
    return [s for s in map(_line, value) if s] if isinstance(value, list) else []


def render_spec_md(spec: dict) -> str:
    """The spec as Russian markdown in the template's order (docs/template.md)."""
    spec = spec if isinstance(spec, dict) else {}
    goal = spec.get("goal") if isinstance(spec.get("goal"), dict) else {}
    parts = [f"# {_line(spec.get('title')) or 'ТЗ'}"]

    parts.append("## 1. Цель\n\n" + (_str(goal.get("text")) or "Не указана.")
                 + f"\n\n**Метрика:** {_line(goal.get('metric')) or 'не указана'}")

    roles = [f"**{_label(r, i, limit=0)}. {_line(r.get('name')) or 'без названия'}** — {_line(r.get('does')) or '…'}"
             for i, r in _items(spec, "roles")]
    parts.append("## 2. Роли\n\n" + _bullets(roles))

    role_names = {_id(r): _line(r.get("name")) for _, r in _items(spec, "roles")}
    scenarios = []
    for i, sc in _items(spec, "scenarios"):
        role = _line(sc.get("role"))
        who = ", ".join(filter(None, (role, role_names.get(role))))
        head = f"### {_label(sc, i, limit=0)}. {_line(sc.get('title')) or 'без названия'}"
        head += f" ({who})" if who else ""
        steps = [s for s in map(_line, sc.get("steps") if isinstance(sc.get("steps"), list) else []) if s]
        body = "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)) or "Шаги не указаны."
        scenarios.append(f"{head}\n\n{body}\n\n**Результат:** {_line(sc.get('result')) or 'не указан'}")
    parts.append("## 3. Сценарии\n\n" + ("\n\n".join(scenarios) or "Нет."))

    data = []
    for i, d in _items(spec, "data"):
        fields = [s for s in map(_line, d.get("fields") if isinstance(d.get("fields"), list) else []) if s]
        source = _line(d.get("source"))
        data.append(f"**{_label(d, i, 'entity', 0)}** — поля: {', '.join(fields) or 'не указаны'}."
                    + (f" Откуда: {_dot(source)}" if source else ""))
    parts.append("## 4. Данные\n\n" + _bullets(data))

    integrations = []
    for i, it in _items(spec, "integrations"):
        d = it.get("direction")
        direction = _DIRECTIONS.get(d, _line(d)) if isinstance(d, str) else ""
        integrations.append(f"**{_label(it, i, 'system', 0)}**" + (f" ({direction})" if direction else "")
                            + f" — {_dot(_line(it.get('purpose')) or '…')} Если недоступна: "
                            + _dot(_line(it.get("fallback")) or "не указано"))
    parts.append("## 5. Интеграции\n\n" + _bullets(integrations))

    nfr = [f"**{_label(n, i, limit=0)}.** {_line(n.get('text')) or '…'} — "
           f"метрика: {_line(n.get('metric')) or 'не указана'}"
           for i, n in _items(spec, "nfr")]
    parts.append("## 6. Нефункциональные требования\n\n" + _bullets(nfr))

    criteria = []
    for i, a in _items(spec, "acceptance"):
        check, target, sc = a.get("check"), _line(a.get("target")), _line(a.get("scenario"))
        how = {"screen": f"код: экран {target} в прототипе", "field": f"код: поле {target} в прототипе",
               "review": "ревьюер"}.get(check if isinstance(check, str) else "", "способ не указан")
        where = f"сценарий {sc}; " if sc else ""
        criteria.append(f"**{_label(a, i, limit=0)}.** {_line(a.get('text')) or '…'} _({where}проверяет {how})_")
    parts.append("## 7. Критерии приёмки\n\n" + _bullets(criteria))

    risks = [f"{_line(r.get('text')) or '…'} — что делаем: {_line(r.get('mitigation')) or 'не указано'}"
             for _, r in _items(spec, "risks")]
    parts.append("## 8. Риски\n\n" + _bullets(risks))

    estimate = spec.get("estimate") if isinstance(spec.get("estimate"), dict) else {}
    rows = []
    for i, e in _items(estimate, "items"):
        days = _num(e.get("days"))
        rows.append(f"| {_line(e.get('part')) or f'№{i + 1}'} | "
                    f"{_fmt_num(days) if days is not None else _line(e.get('days')) or '?'} |")
    total = _num(estimate.get("total_days"))
    if rows or total is not None:
        table = "| Часть работы | Дней |\n|---|---|\n" + "\n".join(rows)
        table += f"\n| **Итого** | **{_fmt_num(total) if total is not None else '?'}** |"
        parts.append("## 9. Оценка\n\n" + table.replace("\n\n", "\n"))
    else:
        parts.append("## 9. Оценка\n\nНет.")

    parts.append("## 10. Допущения и вопросы клиенту\n\n**Допущения**\n\n" + _bullets(_strings(spec, "assumptions"))
                 + "\n\n**Вопросы клиенту**\n\n" + _bullets(_strings(spec, "questions")))
    return "\n\n".join(parts) + "\n"
