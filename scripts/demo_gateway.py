"""Scripted gateway for page development and dry runs (scripts/dev_server.py, scripts/record.py
--dry-run). Never used in production: the Docker image does not copy scripts/.

Whatever the brief, it answers about one made-up case (online booking for a barbershop): the
first spec version is red on purpose (a vague criterion, K7, and the reviewer's Ч6), so one rework
round shows; the second is clean. The prototype is a small working HTML with 3 screens. Each
answer takes 0.5–2 s: real sleeps scaled by ``speed``, or, with ``clock``, a virtual clock is
moved instead (recorded examples then get realistic ``t_ms`` without waiting).
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time

from app.gateway import Chat
from app.pricing import call_cost_rub

DEMO_NAME = "demo"
DEMO_TITLE = "Онлайн-запись в барбершоп (фейк)"
DEMO_BRIEF = ("Нужна онлайн-запись в барбершоп: клиент сам выбирает мастера и время, мастер видит свои записи, "
              "администратор ведёт мастеров и услуги.")

DELAYS_S = {"brief_parse": 0.8, "spec": 2.0, "spec_review": 1.0, "prototype": 2.0, "accept_review": 0.6}

PARSE = {
    "niche": "барбершоп",
    "goal": "Клиенты записываются сами в любое время, администратор меньше времени тратит на звонки, "
            "у мастеров меньше пустых окон.",
    "roles": [{"name": "Клиент", "need": "записаться к своему мастеру на удобное время"},
              {"name": "Мастер", "need": "видеть свои записи на день"},
              {"name": "Администратор", "need": "вести мастеров, их графики и услуги"}],
    "assumptions": ["Один барбершоп, до 6 мастеров", "Оплата на месте, предоплата не нужна"],
    "questions": [{"q": "Нужны ли напоминания клиенту перед визитом?", "why": "SMS-рассылка — отдельная интеграция"},
                  {"q": "Можно ли записаться к любому свободному мастеру?", "why": "меняет сценарий записи"}],
    "out_of_scope": ["Онлайн-оплата", "Программа лояльности"],
}

SPEC_V2 = {
    "title": "Онлайн-запись в барбершоп",
    "goal": {"text": "Клиенты записываются сами, без звонка: администратор свободен от телефона, у мастеров "
                     "меньше пустых окон.",
             "metric": "Через 2 месяца не меньше 50% записей приходит онлайн, пустых окон у мастеров на 20% меньше"},
    "roles": [{"id": "R1", "name": "Клиент", "does": "записывается к мастеру на выбранное время"},
              {"id": "R2", "name": "Мастер", "does": "смотрит свои записи на день"},
              {"id": "R3", "name": "Администратор", "does": "заводит мастеров, их графики и услуги"}],
    "scenarios": [
        {"id": "S1", "role": "R1", "title": "Записаться к мастеру",
         "steps": ["Выбирает услугу", "Выбирает мастера и свободное время", "Вводит имя и телефон",
                   "Подтверждает запись"],
         "result": "Запись создана, время у мастера занято"},
        {"id": "S2", "role": "R2", "title": "Посмотреть записи на день",
         "steps": ["Открывает расписание", "Выбирает день"],
         "result": "Видит записи дня: время, услуга, имя клиента"},
        {"id": "S3", "role": "R3", "title": "Добавить мастера",
         "steps": ["Открывает список мастеров", "Вводит имя, услуги и график", "Сохраняет мастера"],
         "result": "Мастер появился в записи для клиентов"},
    ],
    "data": [
        {"entity": "Запись", "fields": ["услуга", "мастер", "дата", "время", "имя", "телефон"],
         "source": "вводит клиент"},
        {"entity": "Мастер", "fields": ["имя", "услуги", "график"], "source": "заводит администратор"},
        {"entity": "Услуга", "fields": ["название", "длительность", "цена"], "source": "заводит администратор"},
    ],
    "integrations": [],
    "nfr": [{"id": "N1", "text": "Страница записи открывается на телефоне", "metric": "до 2 с на 4G"},
            {"id": "N2", "text": "Нагрузка", "metric": "до 30 клиентов одновременно без ошибок"}],
    "acceptance": [
        {"id": "A1", "text": "Клиент выбирает услугу, мастера и время и создаёт запись", "scenario": "S1",
         "check": "screen", "target": "S1"},
        {"id": "A2", "text": "В форме записи есть поле для телефона", "scenario": "S1", "check": "field",
         "target": "Запись.телефон"},
        {"id": "A3", "text": "Запись занимает не больше 4 шагов: услуга, мастер и время, контакты, подтверждение",
         "scenario": "S1", "check": "review", "target": ""},
        {"id": "A4", "text": "Мастер видит свои записи на выбранный день", "scenario": "S2", "check": "screen",
         "target": "S2"},
        {"id": "A5", "text": "У каждой записи в расписании видны время, услуга и имя клиента", "scenario": "S2",
         "check": "review", "target": ""},
        {"id": "A6", "text": "Администратор добавляет мастера с графиком работы", "scenario": "S3",
         "check": "screen", "target": "S3"},
        {"id": "A7", "text": "В карточке мастера есть поле графика", "scenario": "S3", "check": "field",
         "target": "Мастер.график"},
        {"id": "A8", "text": "Новый мастер сразу появляется в списке мастеров при записи", "scenario": "S3",
         "check": "review", "target": ""},
    ],
    "risks": [{"text": "Клиенты записываются и не приходят",
               "mitigation": "подтверждение записи по телефону администратором в первый месяц"},
              {"text": "Мастер не обновляет график", "mitigation": "администратор правит график раз в неделю"}],
    "estimate": {"items": [{"part": "Запись для клиента", "days": 3}, {"part": "Расписание мастера", "days": 1.5},
                           {"part": "Мастера и услуги", "days": 1.5}, {"part": "Тестирование и запуск", "days": 1}],
                 "total_days": 7},
    "assumptions": ["Один барбершоп, до 6 мастеров", "Оплата на месте"],
    "questions": ["Нужны ли напоминания клиенту перед визитом?", "Можно ли записаться к любому свободному мастеру?"],
}

SPEC_V1 = copy.deepcopy(SPEC_V2)
SPEC_V1["acceptance"][2]["text"] = "Клиент быстро записывается на услугу"
VAGUE_MARK = "быстро записывается"

PROTOTYPE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Барбершоп — запись</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 420px; margin: 0 auto; padding: 12px; color: #1d1d1f; }
  nav { display: flex; gap: 6px; margin-bottom: 12px; }
  nav button, .primary { border: 1px solid #888; background: #f4f4f4; border-radius: 8px; padding: 8px 10px; }
  .primary { background: #1d1d1f; color: #fff; width: 100%; margin-top: 10px; }
  label { display: block; margin: 8px 0 2px; font-size: 14px; }
  input, select { width: 100%; box-sizing: border-box; padding: 8px; font-size: 16px; }
  .slot { display: inline-block; border: 1px solid #888; border-radius: 6px; padding: 4px 8px; margin: 2px; }
  .done { background: #e8f5e9; padding: 8px; border-radius: 8px; margin-top: 10px; }
  [hidden] { display: none; }
</style>
</head>
<body>
<nav>
  <button type="button" data-go="S1">Запись</button>
  <button type="button" data-go="S2">Мастер</button>
  <button type="button" data-go="S3">Админ</button>
</nav>
<section data-screen="S1">
  <h1>Записаться к мастеру</h1>
  <p>Клиент</p>
  <label>Услуга <select data-field="Запись.услуга"><option>Стрижка</option><option>Борода</option></select></label>
  <label>Мастер <select data-field="Запись.мастер"><option>Артём</option><option>Ильдар</option></select></label>
  <label>Дата <input type="date" data-field="Запись.дата" value="2026-10-14"></label>
  <p>Свободное время: <span class="slot" data-field="Запись.время">11:00</span><span class="slot">12:30</span></p>
  <label>Имя <input data-field="Запись.имя" placeholder="Как к вам обращаться"></label>
  <label>Телефон <input type="tel" data-field="Запись.телефон" placeholder="+7 900 000-00-00"></label>
  <button type="button" class="primary" id="book">Записаться</button>
  <p class="done" id="booked" hidden>Запись создана: стрижка у Артёма, 14 октября, 11:00</p>
</section>
<section data-screen="S2" hidden>
  <h1>Записи на день</h1>
  <p>Мастер: Артём</p>
  <label>День <input type="date" value="2026-10-14"></label>
  <ul>
    <li>11:00 — Стрижка — Иван</li>
    <li>12:30 — Борода — Олег</li>
  </ul>
  <button type="button" data-go="S1">К записи</button>
</section>
<section data-screen="S3" hidden>
  <h1>Добавить мастера</h1>
  <p>Администратор</p>
  <label>Имя мастера <input data-field="Мастер.имя" placeholder="Имя"></label>
  <label>Услуги <input data-field="Мастер.услуги" placeholder="Стрижка, борода"></label>
  <label>График <input data-field="Мастер.график" placeholder="Пн–Пт 10:00–20:00"></label>
  <button type="button" class="primary" id="save">Сохранить мастера</button>
  <p class="done" id="saved" hidden>Мастер сохранён</p>
</section>
<script>
  function show(id) {
    document.querySelectorAll("[data-screen]").forEach(function (s) { s.hidden = s.dataset.screen !== id; });
  }
  function reveal(button, message) {
    document.getElementById(button).addEventListener("click", function () {
      document.getElementById(message).hidden = false;
    });
  }
  document.querySelectorAll("[data-go]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.dataset.go); });
  });
  reveal("book", "booked");
  reveal("save", "saved");
</script>
</body>
</html>
"""

_CRITERIA = re.compile(r"<критерии>\s*(.*?)\s*</критерии>", re.DOTALL)


class VirtualClock:
    """A monotonic clock that only moves when told to: pass it to the pipeline and the gateway."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self.lock = threading.Lock()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.t += seconds


def _review(red: bool) -> dict:
    items = [{"id": f"Ч{i}", "ok": True, "note": ""} for i in range(1, 9)]
    if red:
        items[5] = {"id": "Ч6", "ok": False,
                    "note": "A3: «быстро» не проверить «да / нет» — нужно число шагов или секунд"}
    return {"items": items}


def _accept(user: str) -> dict:
    found = _CRITERIA.search(user)
    try:
        criteria = json.loads(found[1]) if found else []
    except ValueError:
        criteria = []
    items = [{"id": c.get("id"), "ok": True, "note": "на экране это видно"} for c in criteria if isinstance(c, dict)]
    if items:
        items[-1] = {"id": items[-1]["id"], "ok": False,
                     "note": "на экране записи список мастеров не связан с экраном администратора"}
    return {"items": items}


class DemoGateway:
    def __init__(self, speed: float = 1.0, clock: VirtualClock | None = None):
        self.speed = speed
        self.clock = clock

    def _wait(self, seconds: float) -> None:
        if self.clock is not None:
            self.clock.advance(seconds)
        elif self.speed > 0:
            time.sleep(seconds * self.speed)

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None) -> Chat:
        kind = schema["name"] if schema else "prototype"
        user = messages[-1]["content"] if messages else ""
        whole = "\n".join(m.get("content") or "" for m in messages)
        if kind == "brief_parse":
            content = json.dumps(PARSE, ensure_ascii=False)
        elif kind == "spec":
            content = json.dumps(SPEC_V2 if "Прошлая версия ТЗ" in whole else SPEC_V1, ensure_ascii=False)
        elif kind == "spec_review":
            content = json.dumps(_review(VAGUE_MARK in whole), ensure_ascii=False)
        elif kind == "accept_review":
            content = json.dumps(_accept(user), ensure_ascii=False)
        else:
            content = "```html\n" + PROTOTYPE + "```"
        delay = DELAYS_S.get(kind, 1.0)
        self._wait(delay)
        tin, tout = len(whole) // 3, min(max_tokens, len(content) // 3)
        return Chat(content, tin, tout, int(delay * 1000), call_cost_rub(model, tin, tout), "stop", True)
