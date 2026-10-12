"""The contract between the steps: JSON schemas the models answer with (strict json_schema).

Every object lists all its properties in ``required`` and forbids extra ones: strict mode of the
gateway's OpenAI-compatible API needs that. Optional values are ``null``, never a missing key.
``maxItems`` is not sent (not every model behind the gateway accepts it in strict mode): the
limits live in LIMITS and code enforces them (app/checks.py truncates and reports).

IDs: roles R1.., scenarios S1.., non-functional requirements N1.., acceptance criteria A1...
A data field is addressed as ``"<entity>.<field>"`` exactly as written in ``data`` (case-sensitive).
"""

from __future__ import annotations


def _obj(props: dict, name: str | None = None) -> dict:
    schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    return {"name": name, "strict": True, "schema": schema} if name else schema


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
NULLABLE_STR = {"type": ["string", "null"]}

# Step 1. The brief: goal, who uses it, what we assumed and what to ask the client.
PARSE = _obj({
    "niche": STR,                                   # «автосервис», «стоматология»
    "goal": STR,                                    # what the business wants to change, 1–2 sentences
    "roles": _arr(_obj({"name": STR, "need": STR})),
    "assumptions": _arr(STR),                       # what we filled in ourselves, explicitly
    "questions": _arr(_obj({"q": STR, "why": STR})),  # to the client; why = what the answer changes
    "out_of_scope": _arr(STR),
}, "brief_parse")

# Step 2. The spec (ТЗ) by the template, see docs/template.md. Code renders it to text.
SPEC = _obj({
    "title": STR,
    "goal": _obj({"text": STR, "metric": STR}),     # metric: how we know the goal is reached, with a number
    "roles": _arr(_obj({"id": STR, "name": STR, "does": STR})),
    "scenarios": _arr(_obj({"id": STR, "role": STR, "title": STR, "steps": _arr(STR), "result": STR})),
    "data": _arr(_obj({"entity": STR, "fields": _arr(STR), "source": STR})),
    "integrations": _arr(_obj({"system": STR, "purpose": STR,
                               "direction": {"type": "string", "enum": ["in", "out", "both"]},
                               "fallback": STR})),
    "nfr": _arr(_obj({"id": STR, "text": STR, "metric": STR})),
    # check: "screen" — code: the prototype has a screen for scenario `target` (an S-id);
    #        "field"  — code: the prototype has an element data-field="`target`" (Entity.field);
    #        "review" — the reviewer model decides from the spec and the prototype text; target = "".
    "acceptance": _arr(_obj({"id": STR, "text": STR, "scenario": NULLABLE_STR,
                             "check": {"type": "string", "enum": ["screen", "field", "review"]},
                             "target": STR})),
    "risks": _arr(_obj({"text": STR, "mitigation": STR})),
    "estimate": _obj({"items": _arr(_obj({"part": STR, "days": NUM})), "total_days": NUM}),
    "assumptions": _arr(STR),
    "questions": _arr(STR),
}, "spec")

# Step 3b. The reviewer goes through the checklist (docs/checklist.md), one verdict per item.
SPEC_REVIEW = _obj({
    "items": _arr(_obj({"id": STR, "ok": BOOL, "note": STR})),   # id "Ч1".."Ч8"; note: why not, "" if ok
}, "spec_review")

# Step 5b. The reviewer accepts the criteria with check == "review".
ACCEPT_REVIEW = _obj({
    "items": _arr(_obj({"id": STR, "ok": BOOL, "note": STR})),   # id "A3"; note: what is seen or missing
}, "accept_review")

SCHEMAS = {"parse": PARSE, "spec": SPEC, "spec_review": SPEC_REVIEW, "accept_review": ACCEPT_REVIEW}

# Array limits, enforced by code: longer arrays are truncated and reported as a check issue.
LIMITS = {
    "parse.roles": 6, "parse.assumptions": 8, "parse.questions": 8, "parse.out_of_scope": 6,
    "roles": 6, "scenarios": 8, "scenarios.steps": 8, "data": 8, "data.fields": 10, "integrations": 5,
    "nfr": 6, "acceptance": 12, "risks": 6, "estimate.items": 10, "assumptions": 8, "questions": 8,
    "review.items": 12,
}
