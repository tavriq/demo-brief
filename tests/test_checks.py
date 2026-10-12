"""checks: K1–K12 on the spec, P1–P5 on the prototype, acceptance by code, screen text, render."""

import copy
import json

import pytest

from app.checks import (accept_by_code, check_prototype, check_spec, extract_html, is_red_by_code,
                        render_spec_md, screens_text, truncate_parse, truncate_review)
from app.schema import LIMITS
from tests.fixtures_spec import VALID_PROTOTYPE, VALID_SPEC


def spec(**changes):
    s = copy.deepcopy(VALID_SPEC)
    s.update(changes)
    return s


def issues_of(s, id_=None):
    _, issues = check_spec(s)
    return [x for x in issues if id_ is None or x["id"] == id_]


def texts(s, id_):
    return " | ".join(x["text"] for x in issues_of(s, id_))


def well_formed(issues):
    for x in issues:
        assert set(x) == {"id", "level", "text"}
        assert x["level"] in ("error", "warn")
        assert isinstance(x["text"], str) and x["text"]
    return True


# --- the valid spec ---------------------------------------------------------------------------

def test_valid_spec_passes():
    s = copy.deepcopy(VALID_SPEC)
    out, issues = check_spec(s)
    assert issues == []
    assert out == VALID_SPEC
    assert not is_red_by_code(issues)
    assert s == VALID_SPEC  # input untouched


def test_valid_spec_through_json():
    out, issues = check_spec(json.loads(json.dumps(VALID_SPEC, ensure_ascii=False)))
    assert issues == []


def test_empty_integrations_are_fine():
    assert issues_of(spec(integrations=[])) == []


# --- K1 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("key, name", [("roles", "Роли"), ("scenarios", "Сценарии"), ("data", "Данные"),
                                       ("nfr", "Нефункциональные требования"), ("acceptance", "Критерии приёмки"),
                                       ("risks", "Риски")])
def test_k1_required_list_empty_or_missing(key, name):
    assert f"Не заполнен раздел «{name}»" in texts(spec(**{key: []}), "K1")
    s = spec()
    del s[key]
    assert f"Не заполнен раздел «{name}»" in texts(s, "K1")
    assert is_red_by_code(issues_of(s))


@pytest.mark.parametrize("goal, word", [({"text": "", "metric": "40%"}, "текста"),
                                        ({"text": "Цель", "metric": ""}, "метрики"),
                                        ("строка", "объект"), (None, "объект")])
def test_k1_goal(goal, word):
    assert word in texts(spec(goal=goal), "K1")


def test_k1_estimate():
    assert "Оценка" in texts(spec(estimate=None), "K1")
    assert "items" in texts(spec(estimate={"items": [], "total_days": 0}), "K1")
    assert "не объект" in texts(spec(estimate={"items": ["4 дня"], "total_days": 4}), "K1")


def test_k1_wrong_types_of_sections():
    s = spec(roles="Клиент, Мастер", integrations={"system": "SMS"}, questions=[{"q": "?"}])
    t = texts(s, "K1")
    assert "«Роли» должен быть списком" in t and "«Интеграции» должен быть списком" in t
    assert "«Вопросы клиенту»: пункты №1 — не строка" in t
    assert "«Сценарии»: пункты №2 — не объект" in texts(spec(scenarios=[VALID_SPEC["scenarios"][0], "S2"]), "K1")


def test_k1_spec_not_object():
    for bad in (None, "ТЗ", [], 42):
        out, issues = check_spec(bad)
        assert out == {} and [x["id"] for x in issues] == ["K1"] and is_red_by_code(issues)


# --- K2 ---------------------------------------------------------------------------------------

def test_k2_duplicate_id():
    s = spec()
    s["roles"][1]["id"] = "R1"
    assert "id R1 повторяется" in texts(s, "K2")


@pytest.mark.parametrize("key, bad", [("roles", "Role1"), ("scenarios", "s1"), ("nfr", "N0"), ("acceptance", "A-1")])
def test_k2_format(key, bad):
    s = spec()
    s[key][0]["id"] = bad
    assert f"«{bad}» не по формату" in texts(s, "K2")


def test_k2_cyrillic_letter_hinted():
    s = spec()
    s["acceptance"][0]["id"] = "А1"  # Cyrillic А
    assert "кириллицей" in texts(s, "K2")


def test_k2_missing_or_not_string_id():
    s = spec()
    s["nfr"][1]["id"] = ""
    s["roles"][0]["id"] = 1
    t = texts(s, "K2")
    assert "«Нефункциональные требования», пункт №2: нет id" in t
    assert "«Роли», пункт №1: нет id" in t


# --- K3, K4 -----------------------------------------------------------------------------------

def test_k3_unknown_role():
    s = spec()
    s["scenarios"][2]["role"] = "R9"
    assert "У сценария S3 роль «R9»" in texts(s, "K3")


def test_k3_missing_role_and_wrong_type():
    s = spec()
    s["scenarios"][0]["role"] = ""
    s["scenarios"][1]["role"] = ["R1", "R2"]
    t = texts(s, "K3")
    assert "У сценария S1 не указана роль" in t and "S2 роль должна быть одна" in t


def test_k3_role_by_name_is_accepted():
    s = spec()
    s["scenarios"][3]["role"] = "управляющий"
    assert issues_of(s) == []


def test_k4_role_without_scenario():
    s = spec()
    s["roles"].append({"id": "R4", "name": "Бухгалтер", "does": "сверяет оплаты"})
    assert texts(s, "K4") == "У роли R4 («Бухгалтер») нет ни одного сценария"


# --- K5, K6 -----------------------------------------------------------------------------------

def test_k5_scenario_without_criterion():
    s = spec()
    s["acceptance"] = [a for a in s["acceptance"] if a["scenario"] != "S2"]
    assert texts(s, "K5") == "У сценария S2 нет критерия приёмки"


def test_k5_target_counts_as_link():
    s = spec()
    for a in s["acceptance"]:
        a["scenario"] = None
    s["acceptance"].append({"id": "A9", "text": "Есть экран загрузки постов", "scenario": None,
                            "check": "screen", "target": "S4"})
    # S1–S4 all have a screen criterion with target = the scenario id
    assert issues_of(s, "K5") == []


def test_k6_screen_to_missing_scenario():
    s = spec()
    s["acceptance"][0]["target"] = "S7"
    assert "Критерий A1 (screen) ссылается на сценарий «S7», его нет" in texts(s, "K6")


@pytest.mark.parametrize("target", ["Запись.почта", "запись.телефон", "Телефон", "Запись.телефо"])
def test_k6_field_to_missing_field(target):
    s = spec()
    s["acceptance"][1]["target"] = target
    assert "Критерий A2 (field) ссылается на поле" in texts(s, "K6")


def test_k6_review_with_target_and_unknown_check():
    s = spec()
    s["acceptance"][2]["target"] = "S1"
    s["acceptance"][3]["check"] = "manual"
    s["acceptance"][4]["check"] = None
    t = texts(s, "K6")
    assert "A3 (review) target должен быть пустым" in t
    assert "A4 неизвестный способ проверки «manual»" in t and "A5 неизвестный способ проверки «null»" in t


def test_k6_dangling_scenario_and_empty_target():
    s = spec()
    s["acceptance"][2]["scenario"] = "S9"
    s["acceptance"][0]["target"] = ""
    t = texts(s, "K6")
    assert "A3 привязан к сценарию «S9»" in t and "A1 (screen) не указан сценарий" in t


# --- K7 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Интерфейс удобный для клиента", "Запись работает быстро", "Интуитивно понятная форма",
    "Современный дизайн", "Качественная вёрстка", "Красивый экран записи", "Понятные подписи у полей",
    "Клиенту легко записаться", "Лёгкая форма", "Оптимальный путь клиента", "Эффективная работа мастера",
    "Надёжная запись", "Надежное хранение", "Простой интерфейс", "Запись просто сделать",
    "НЕУДОБНО не бывает", "БЫСТРАЯ ЗАПИСЬ",
    # an id is not a number
    "Экран S1 удобный и понятный", "Пациент быстро записывается на экране S12", "A3 и R2 работают надёжно",
])
def test_k7_vague_without_digit(text):
    s = spec()
    s["acceptance"][2]["text"] = text
    assert "Критерий A3 расплывчатый" in texts(s, "K7")


@pytest.mark.parametrize("text", [
    "Запись создаётся быстрее чем за 30 секунд", "Форма из 3 простых полей",
    "Легковые и грузовые автомобили можно выбрать в списке", "Мастер проставляет статус записи",
    "Пространство между кнопками не мешает нажатию", "Клиент видит прежнее время записи",
    "Экран S1 открывается быстрее 2 с", "Быстро: не больше 30с на запись", "Оценка удобства от 4,5",
])
def test_k7_not_vague(text):
    s = spec()
    s["acceptance"][2]["text"] = text
    assert issues_of(s, "K7") == []


def test_k7_empty_criterion_text():
    s = spec()
    s["acceptance"][0]["text"] = ""
    assert "У критерия A1 нет текста" in texts(s, "K7")


# --- K8 ---------------------------------------------------------------------------------------

def test_k8_goal_metric_without_number():
    s = spec(goal={"text": "Клиенты записываются сами", "metric": "больше записей через сайт"})
    assert "В метрике цели нет числа" in texts(s, "K8")


def test_k8_nfr_metric():
    s = spec()
    s["nfr"][0]["metric"] = "открывается мгновенно"
    s["nfr"][1]["metric"] = ""
    t = texts(s, "K8")
    assert "НФТ N1 нет числа" in t and "У НФТ N2 нет метрики" in t


@pytest.mark.parametrize("metric", ["как в S1", "быстрее, чем N1 требует", "уровень A2"])
def test_k8_an_id_is_not_a_number(metric):
    s = spec()
    s["nfr"][0]["metric"] = metric
    assert "НФТ N1 нет числа" in texts(s, "K8")


def test_k8_numeric_metric_is_fine():
    s = spec()
    s["nfr"][0]["metric"] = 2
    assert issues_of(s, "K8") == []


# --- K9 ---------------------------------------------------------------------------------------

def test_k9_sum_mismatch():
    s = spec()
    s["estimate"]["total_days"] = 15
    assert texts(s, "K9") == "Оценка не сходится: части дают 13 дн., а total_days = 15"


def test_k9_zero_and_negative_days():
    s = spec()
    s["estimate"]["items"][0]["days"] = 0
    s["estimate"]["items"][1]["days"] = -1
    s["estimate"]["total_days"] = 6
    t = texts(s, "K9")
    assert "«Запись для клиента»: дней 0" in t and "«Кабинет мастера-приёмщика»: дней -1" in t


def test_k9_numeric_strings_accepted():
    s = spec()
    s["estimate"]["items"][2]["days"] = "2,5"
    s["estimate"]["total_days"] = "13"
    assert issues_of(s) == []


@pytest.mark.parametrize("days", ["три", None, [], {}, True, "nan", float("inf"), 10 ** 400])
def test_k9_days_not_a_number(days):
    s = spec()
    s["estimate"]["items"][0]["days"] = days
    assert "дни не число" in texts(s, "K9")


@pytest.mark.parametrize("total", [None, "много", [13]])
def test_k9_total_not_a_number(total):
    s = spec()
    s["estimate"]["total_days"] = total
    assert "total_days не число" in texts(s, "K9")


# --- K10, K11 ---------------------------------------------------------------------------------

def test_k10_risk_and_integration():
    s = spec()
    s["risks"][0]["mitigation"] = ""
    s["integrations"][0]["fallback"] = "  "
    t = texts(s, "K10")
    assert "У риска «Клиенты записываются и не приезжают» не написано, что делаем" in t
    assert "У интеграции «SMS-шлюз» нет запасного пути" in t


def test_k11_steps_and_result():
    s = spec()
    s["scenarios"][0]["steps"] = ["Записывается"]
    s["scenarios"][1]["steps"] = ["Открывает ссылку", ""]
    s["scenarios"][2]["result"] = ""
    s["scenarios"][3]["steps"] = "Открывает расписание, закрывает слоты"
    t = texts(s, "K11")
    assert "В сценарии S1 шагов 1" in t and "В сценарии S2 шагов 1" in t
    assert "У сценария S3 не указан результат" in t and "S4 шаги должны быть списком" in t


# --- K12 and truncation -----------------------------------------------------------------------

def test_k12_truncates_arrays_and_warns():
    s = spec()
    extra = [{"text": f"Риск {i}", "mitigation": "следим"} for i in range(10)]
    s["risks"] = s["risks"] + extra
    s["scenarios"][0]["steps"] = [f"Шаг {i}" for i in range(12)]
    s["data"][0]["fields"] = s["data"][0]["fields"] + [f"поле{i}" for i in range(5)]
    out, issues = check_spec(s)
    assert len(out["risks"]) == LIMITS["risks"]
    assert len(out["scenarios"][0]["steps"]) == LIMITS["scenarios.steps"]
    assert len(out["data"][0]["fields"]) == LIMITS["data.fields"]
    k12 = [x for x in issues if x["id"] == "K12"]
    assert [x["level"] for x in k12] == ["warn"] * 3
    assert "Риски: пунктов 13, предел 6" in k12[0]["text"]
    assert any("Шаги сценария S1: пунктов 12, предел 8" in x["text"] for x in k12)
    assert any("Поля сущности «Запись»" in x["text"] for x in k12)
    assert not is_red_by_code(issues)
    assert len(s["risks"]) == 13 and len(s["scenarios"][0]["steps"]) == 12  # input untouched


def test_k12_steps_of_cut_scenarios_not_reported():
    s = spec()
    many = [{"id": f"S{i}", "role": "R1", "title": "т", "steps": ["а"] * 10, "result": "р"} for i in range(5, 15)]
    s["scenarios"] = s["scenarios"] + many
    out, issues = check_spec(s)
    assert len(out["scenarios"]) == 8
    steps_warns = [x for x in issues if x["id"] == "K12" and "Шаги" in x["text"]]
    assert len(steps_warns) == 4  # S5..S8 kept, S9..S14 cut away without a word


def test_k12_estimate_items():
    s = spec()
    s["estimate"] = {"items": [{"part": f"Часть {i}", "days": 1} for i in range(12)], "total_days": 10}
    out, issues = check_spec(s)
    assert len(out["estimate"]["items"]) == 10
    assert [x["id"] for x in issues] == ["K12"]
    assert "Части оценки: пунктов 12" in issues[0]["text"]


def test_truncate_parse():
    parse = {"niche": "автосервис", "goal": "запись без звонка", "roles": [{"name": "Клиент", "need": "запись"}] * 9,
             "assumptions": ["а"] * 8, "questions": [{"q": "?", "why": "объём"}] * 10, "out_of_scope": ["оплата"]}
    out, issues = truncate_parse(parse)
    assert len(out["roles"]) == 6 and len(out["assumptions"]) == 8 and len(out["questions"]) == 8
    assert [x["text"] for x in issues] == ["Разбор, роли: пунктов 9, предел 6 — лишние отброшены",
                                           "Разбор, вопросы клиенту: пунктов 10, предел 8 — лишние отброшены"]
    assert all(x["id"] == "K12" and x["level"] == "warn" for x in issues)
    assert len(parse["roles"]) == 9
    assert truncate_parse(None) == ({}, [])
    assert truncate_parse({"roles": "строка"}) == ({"roles": "строка"}, [])


def test_truncate_review():
    out, issues = truncate_review({"items": [{"id": "Ч1", "ok": True, "note": ""}] * 15})
    assert len(out["items"]) == 12 and issues[0]["id"] == "K12"
    assert truncate_review("нет") == ({}, [])


# --- robustness -------------------------------------------------------------------------------

GARBAGE = [None, "", "x", 0, -1.5, True, [], {}, ["x"], [1], [None], [[]], {"a": 1}, float("nan"), 10 ** 400]


def _variants():
    """VALID_SPEC with one value replaced by garbage: top-level keys and fields of first items."""
    for key in VALID_SPEC:
        for g in GARBAGE:
            yield spec(**{key: g})
        value = VALID_SPEC[key]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            for field in value[0]:
                for g in GARBAGE:
                    s = spec()
                    s[key][0][field] = g
                    yield s
            for g in GARBAGE:
                s = spec()
                s[key][0] = g
                yield s
        if isinstance(value, dict):
            for field in value:
                for g in GARBAGE:
                    s = spec()
                    s[key][field] = g
                    yield s
    for g in GARBAGE:
        s = spec()
        s["estimate"]["items"][0] = g
        yield s
        s = spec()
        s["estimate"]["items"][0]["days"] = g
        yield s


def test_garbage_never_raises():
    n = 0
    for s in _variants():
        out, issues = check_spec(s)
        assert isinstance(out, dict) and well_formed(issues)
        render_spec_md(s)
        render_spec_md(out)
        accept_by_code(s, VALID_PROTOTYPE)
        check_prototype(VALID_PROTOTYPE, s)
        n += 1
    assert n > 500


@pytest.mark.parametrize("bad", [None, "ТЗ", 42, [], [VALID_SPEC], {"goal": {"text": 1, "metric": [1]}},
                                 {"roles": [{"id": None}], "scenarios": [{"id": ["S1"], "role": {"id": "R1"}}]}])
def test_garbage_spec_is_red(bad):
    out, issues = check_spec(bad)
    assert is_red_by_code(issues) and well_formed(issues)
    assert isinstance(render_spec_md(bad), str)
    assert accept_by_code(bad, VALID_PROTOTYPE) == []
    assert isinstance(check_prototype(VALID_PROTOTYPE, bad), list)


def test_is_red_by_code():
    assert not is_red_by_code([])
    assert not is_red_by_code([{"id": "K12", "level": "warn", "text": "обрезано"}])
    assert is_red_by_code([{"id": "K12", "level": "warn", "text": "w"}, {"id": "K5", "level": "error", "text": "e"}])
    assert not is_red_by_code(None)
    assert not is_red_by_code(["error", None])


def test_issues_ordered_by_check():
    s = spec()
    s["estimate"]["total_days"] = 1          # K9
    s["roles"][1]["id"] = "R1"               # K2 duplicate, K3: S3 points to R2, which is gone
    s["risks"] = s["risks"] + [{"text": "р", "mitigation": "м"}] * 5  # K12
    ids = [x["id"] for x in issues_of(s)]
    assert ids == ["K2", "K3", "K9", "K12"]


# --- extract_html -----------------------------------------------------------------------------

DOC = "<!doctype html><html><body><p>Привет</p></body></html>"


@pytest.mark.parametrize("text", [
    f"Вот прототип:\n```html\n{DOC}\n```\nГотово.",
    f"```HTML\n{DOC}\n```",
    f"```html\n{DOC}```",
    f"Прототип:\n{DOC}\nКонец.",
    f"```\n{DOC}\n```",
    f"```html {DOC}```",
])
def test_extract_html(text):
    assert extract_html(text) == DOC


def test_extract_html_from_html_tag_without_doctype():
    assert extract_html("Ответ: <html><body>x</body></html> всё") == "<html><body>x</body></html>"


def test_extract_html_cut_answer_runs_to_end():
    assert extract_html("```html\n<!doctype html><html><body><p>обрыв") == "<!doctype html><html><body><p>обрыв"
    assert extract_html("Вот: <!doctype html><html><body>обрыв\n```") == "<!doctype html><html><body>обрыв"


def test_extract_html_prefers_fence_and_first_block():
    text = "<html>старый</html>\n```html\n<!doctype html><body>новый</body>\n```\n```html\n<b>второй</b>\n```"
    assert extract_html(text) == "<!doctype html><body>новый</body>"


@pytest.mark.parametrize("text", ["", "Не могу сделать прототип.", "```html\n\n```", None, 42, ["<html>"]])
def test_extract_html_none(text):
    assert extract_html(text) is None


# --- check_prototype --------------------------------------------------------------------------

def proto_with(snippet: str) -> str:
    return VALID_PROTOTYPE.replace("</body>", snippet + "\n</body>")


def test_valid_prototype_passes():
    assert check_prototype(VALID_PROTOTYPE, VALID_SPEC) == []


@pytest.mark.parametrize("html", [None, "", "   ", 42])
def test_p1_no_html(html):
    assert [x["id"] for x in check_prototype(html, VALID_SPEC)] == ["P1"]


def test_p1_no_body():
    html = "<section data-screen='S1'><button data-go='S1'>x</button></section>"
    issues = check_prototype(html, {"scenarios": [{"id": "S1"}]})
    assert [x["id"] for x in issues] == ["P1"]
    assert issues[0]["level"] == "error"


def test_p2_too_big():
    html = proto_with("<p>" + "я" * 41_000 + "</p>")  # 2 bytes each
    issues = [x for x in check_prototype(html, VALID_SPEC) if x["id"] == "P2"]
    assert issues and issues[0]["level"] == "error" and "больше предела 80 000" in issues[0]["text"]


@pytest.mark.parametrize("snippet, kind", [
    ('<img src="https://example.com/a.png">', "внешний src у <img>"),
    ('<img src="//cdn.example.com/a.png">', "внешний src у <img>"),
    ('<img src="\\\\cdn.example.com/a.png">', "внешний src у <img>"),
    ('<img src=" HTTP://example.com/a.png">', "внешний src у <img>"),
    ('<img src="ht\ntp://example.com/a.png">', "внешний src у <img>"),
    ('<script src="https://cdn.example.com/x.js"></script>', "внешний src у <script>"),
    ('<link rel="stylesheet" href="https://fonts.example.com/css">', "внешний href у <link>"),
    ('<link rel="preload" href="//cdn.example.com/f.woff2">', "внешний href у <link>"),
    ('<link rel="prefetch" href="https://example.com/next">', "внешний href у <link>"),
    ('<a href="https://example.com">сайт</a>', "внешний href у <a>"),
    ('<form action="https://example.com/send"><button>ok</button></form>', "внешний action у <form>"),
    ('<form><button formaction="//example.com/x">ok</button></form>', "внешний formaction у <button>"),
    ('<img srcset="a.png 1x, https://example.com/b.png 2x">', "внешний srcset у <img>"),
    ('<video poster="https://example.com/p.jpg"></video>', "внешний poster у <video>"),
    ('<a href="ftp://example.com/f">f</a>', "внешний href у <a>"),
    ('<a href="blob:x">x</a>', "href со схемой blob: у <a>"),
    ('<svg><image xlink:href="https://example.com/i.png"/></svg>', "внешний xlink:href у <image>"),
    ('<a href="#S1" ping="https://example.com/t">x</a>', "внешний ping у <a>"),
    ('<div style="background:url(https://example.com/bg.png)">x</div>', "url(…) наружу в CSS"),
    ("<style>.a{background:url('//example.com/bg.png')}</style>", "url(…) наружу в CSS"),
    ("<style>@import 'theme.css';</style>", "@import в CSS"),
    ("<script>fetch('/api/brief/status')</script>", "fetch("),
    ("<script>window.fetch (u)</script>", "fetch("),
    ("<script>var x = new XMLHttpRequest();</script>", "XMLHttpRequest"),
    ("<script>new WebSocket('wss://example.com')</script>", "WebSocket"),
    ("<script>new EventSource('/events')</script>", "EventSource"),
    ("<script>navigator.sendBeacon('/t', 'x')</script>", "sendBeacon"),
    ("<script>importScripts('w.js')</script>", "importScripts"),
    ("<script>new Worker('w.js')</script>", "new Worker"),
    ("<script>import('./m.js')</script>", "import()"),
    ("<script>var i = new Image(); i.src = 'https://example.com/p';</script>", "адрес http(s) в скрипте"),
    ('<button onclick="location.href=\'https://example.com\'">x</button>', "адрес http(s) в скрипте"),
    ('<iframe srcdoc="x"></iframe>', "<iframe>"),
    ('<object data="x.swf"></object>', "<object>"),
    ('<embed src="x.swf">', "<embed>"),
    ('<base href="/">', "<base>"),
    ('<meta http-equiv="refresh" content="0;url=https://example.com">', '<meta http-equiv="refresh">'),
    ("<META CONTENT='5' HTTP-EQUIV='Refresh'>", '<meta http-equiv="refresh">'),
    ("<script>document.body.innerHTML = '<iframe src=x>';</script>", "<iframe>"),
])
def test_p3_network(snippet, kind):
    issues = [x for x in check_prototype(proto_with(snippet), VALID_SPEC) if x["id"] == "P3"]
    assert len(issues) == 1 and issues[0]["level"] == "error"
    assert kind in issues[0]["text"]
    assert "<!doctype" not in issues[0]["text"]  # kinds, not the HTML


@pytest.mark.parametrize("snippet", [
    '<img src="data:image/png;base64,iVBORw0KGgo=">',
    '<img srcset="data:image/png;base64,AAA=, data:image/png;base64,BBB= 2x">',
    '<a href="#S2">дальше</a>', '<a href="">x</a>', '<img src="logo.png">', '<a href="/new/brief/">x</a>',
    '<div style="background:url(data:image/png;base64,AAA=)">x</div>',
    "<script>var u = URL.createObjectURL(blob); var p = new URL(location.href);</script>",
    "<script>document.createElementNS('http://www.w3.org/2000/svg', 'svg')</script>",
    '<p>Мы пришлём SMS с подтверждением, сеть не нужна</p>',
    "<script>function prefetchScreens() {}</script>",
])
def test_p3_safe(snippet):
    assert [x for x in check_prototype(proto_with(snippet), VALID_SPEC) if x["id"] == "P3"] == []


@pytest.mark.parametrize("snippet, kind", [
    ('<a href="javascript:void(0)" data-go="S2">x</a>', "href со схемой javascript: у <a>"),
    ('<a href="mailto:a@example.com">x</a>', "href со схемой mailto: у <a>"),
    ('<a href="tel:+70000000000">Позвонить</a>', "href со схемой tel: у <a>"),
])
def test_p3_links_that_load_nothing_are_a_warning(snippet, kind):
    issues = [x for x in check_prototype(proto_with(snippet), VALID_SPEC) if x["id"] == "P3"]
    assert len(issues) == 1 and issues[0]["level"] == "warn"
    assert kind in issues[0]["text"] and "сеть" not in issues[0]["text"]


def test_p3_address_inside_a_javascript_link_is_network():
    html = proto_with('<a href="javascript:location=\'https://example.com/?p=\'+1">x</a>')
    levels = {x["level"]: x["text"] for x in check_prototype(html, VALID_SPEC) if x["id"] == "P3"}
    assert "адрес http(s) в скрипте" in levels["error"] and "javascript:" in levels["warn"]


def test_p3_lists_each_kind_once():
    html = proto_with('<img src="https://a.example/1.png"><img src="https://a.example/2.png"><iframe></iframe>')
    (p3,) = [x for x in check_prototype(html, VALID_SPEC) if x["id"] == "P3"]
    assert p3["text"] == "Прототип обращается в сеть: внешний src у <img>, <iframe>"


def test_p4_missing_screen():
    html = VALID_PROTOTYPE.replace('data-screen="S3"', 'data-screen="S9"')
    issues = check_prototype(html, VALID_SPEC)
    p4 = [x for x in issues if x["id"] == "P4"]
    assert p4 and p4[0]["level"] == "error" and "S3" in p4[0]["text"]


def test_p4_no_screens_at_all():
    html = "<!doctype html><html><body><p>Запись</p></body></html>"
    ids = [x["id"] for x in check_prototype(html, VALID_SPEC)]
    assert ids == ["P4"]


def test_p5_no_transitions_is_warning():
    html = VALID_PROTOTYPE.replace("data-go=", "data-x=")
    issues = check_prototype(html, VALID_SPEC)
    assert [(x["id"], x["level"]) for x in issues] == [("P5", "warn")]
    assert not is_red_by_code(issues)


def test_p5_transition_to_missing_screen():
    html = VALID_PROTOTYPE.replace('data-go="S4"', 'data-go="S8"')
    issues = check_prototype(html, VALID_SPEC)
    assert [(x["id"], x["level"]) for x in issues] == [("P5", "warn")]
    assert "S8" in issues[0]["text"]


def test_prototype_with_spec_garbage():
    assert check_prototype(VALID_PROTOTYPE, None) == []
    assert check_prototype(VALID_PROTOTYPE, {"scenarios": "S1"}) == []


# --- screens_text -----------------------------------------------------------------------------

def test_screens_text_visible_only():
    t = screens_text(VALID_PROTOTYPE)
    assert list(t) == ["S1", "S2", "S3", "S4"]
    assert "Телефон" in t["S1"] and "+7 900 000-00-00" in t["S1"] and "Записаться" in t["S1"]
    assert "Замена масла" in t["S1"] and "Схема проезда" in t["S1"]   # option text, alt
    assert "Перенести" in t["S2"]                                     # input type=submit value
    assert "Автосервис" not in " ".join(t.values())                   # outside any screen
    for text in t.values():
        assert "<" not in text and "querySelector" not in text and "font-family" not in text
        assert "  " not in text


def test_screens_text_nested_and_self_closing():
    html = ('<body><div data-screen="S1">Один <div data-screen="S2">Два <input placeholder="Имя"/></div> снова один'
            '<p>абзац без конца</div><section data-screen="S3"/></body>')
    assert screens_text(html) == {"S1": "Один снова один абзац без конца", "S2": "Два Имя", "S3": ""}


def test_screens_text_per_screen_limit():
    html = (f'<body><section data-screen="S1">{"слово " * 600}</section>'
            '<section data-screen="S2">кратко</section></body>')
    t = screens_text(html, per_screen=100)
    assert len(t["S1"]) == 100 and t["S1"].endswith("…")
    assert t["S2"] == "кратко"


def test_screens_text_total_limit_is_fair():
    screens = "".join(f'<section data-screen="S{i}">{"а" * 1500}</section>' for i in range(1, 7))
    html = f'<body>{screens}<section data-screen="S7">коротко</section></body>'
    t = screens_text(html)
    assert len(t) == 7 and sum(map(len, t.values())) <= 8000
    assert t["S7"] == "коротко"
    assert all(len(t[f"S{i}"]) > 1000 for i in range(1, 7))  # the last long screens are not starved


def test_screens_text_defaults_and_garbage():
    big = "".join(f'<section data-screen="S{i}">{"б" * 3000}</section>' for i in range(1, 9))
    t = screens_text(f"<body>{big}</body>")
    assert all(len(v) <= 1500 for v in t.values()) and sum(map(len, t.values())) <= 8000
    assert screens_text(None) == {} and screens_text("") == {} and screens_text("<p>без экранов</p>") == {}


# --- accept_by_code ---------------------------------------------------------------------------

def test_accept_by_code_all_present():
    rows = accept_by_code(VALID_SPEC, VALID_PROTOTYPE)
    assert [r["id"] for r in rows] == ["A1", "A2", "A4", "A5", "A6", "A7"]   # review criteria skipped
    assert all(r["ok"] and r["by"] == "code" and set(r) == {"id", "text", "by", "ok", "note"} for r in rows)
    assert rows[0]["note"] == "экран S1 есть"
    assert rows[1]["note"] == "поле «Запись.телефон» есть"


def test_accept_by_code_missing_screen_and_field():
    html = VALID_PROTOTYPE.replace('data-screen="S2"', 'data-screen="S20"').replace('data-field="Запись.пост"', "")
    rows = {r["id"]: r for r in accept_by_code(VALID_SPEC, html)}
    assert not rows["A4"]["ok"] and rows["A4"]["note"] == "экрана S2 нет в прототипе"
    assert not rows["A6"]["ok"] and "Запись.пост" in rows["A6"]["note"]
    assert rows["A1"]["ok"] and rows["A2"]["ok"]


@pytest.mark.parametrize("html", [None, "", 42])
def test_accept_by_code_no_prototype(html):
    rows = accept_by_code(VALID_SPEC, html)
    assert len(rows) == 6 and all(not r["ok"] and r["note"] == "прототипа нет" for r in rows)


def test_accept_by_code_empty_target():
    s = spec()
    s["acceptance"][0]["target"] = ""
    rows = accept_by_code(s, VALID_PROTOTYPE)
    assert not rows[0]["ok"] and "target пустой" in rows[0]["note"]


# --- render_spec_md ---------------------------------------------------------------------------

def test_render_in_template_order():
    md = render_spec_md(VALID_SPEC)
    heads = ["# Онлайн-запись в автосервис", "## 1. Цель", "## 2. Роли", "## 3. Сценарии", "## 4. Данные",
             "## 5. Интеграции", "## 6. Нефункциональные требования", "## 7. Критерии приёмки", "## 8. Риски",
             "## 9. Оценка", "## 10. Допущения и вопросы клиенту"]
    positions = [md.index(h) for h in heads]
    assert positions == sorted(positions)
    for needle in ("**R2. Мастер-приёмщик**", "### S3. Подтвердить запись (R2, Мастер-приёмщик)",
                   "1. Открывает список новых записей", "**Метрика:** Через 3 месяца",
                   "| Расписание и загрузка постов | 2,5 |",
                   "| **Итого** | **13** |", "SMS-шлюз** (из системы)", "проверяет код: поле Запись.телефон",
                   "Какие услуги нельзя записывать онлайн?"):
        assert needle in md


def test_render_empty_sections():
    md = render_spec_md(spec(integrations=[], assumptions=[]))
    assert "## 5. Интеграции\n\nНет." in md and "**Допущения**\n\nНет." in md
    md = render_spec_md({})
    assert md.startswith("# ТЗ\n") and "## 10. Допущения" in md


def test_render_no_double_stop():
    # модель сама ставит точку в конце «source», «purpose», «fallback» (прогон 12.10) — вторую не дописываем
    md = render_spec_md(spec(data=[{"entity": "Запись", "fields": ["дата"], "source": "вводит клиент."}],
                             integrations=[{"system": "SMS", "purpose": "сообщить о записи.", "direction": "out",
                                            "fallback": "мастер звонит сам."}]))
    assert "Откуда: вводит клиент.\n" in md and "— сообщить о записи. Если недоступна: мастер звонит сам.\n" in md
    assert ".." not in md.replace("...", "")
