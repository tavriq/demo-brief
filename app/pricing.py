"""Prices in rubles per 1M tokens and the worst cases: one call and one pass of the pipeline.

Prices: Timeweb AI Gateway cabinet, «Модели». Text models 07.10.2026 18:45 MSK and
08.10.2026 12:05 MSK (flash-lite, 4.1 mini, Haiku). The page does not say whether VAT is
included. The gateway does not return prices in the API, so they live here and must be
updated by hand.

Worst case of a call: the prompt at 2 characters per token (Russian text is never denser, so
the input is overestimated) plus ``max_tokens`` of output.
"""

from __future__ import annotations

import math

PRICES_RUB_PER_1M: dict[str, tuple[float, float]] = {
    "yandex/yandexgpt-lite": (210, 210),
    "timeweb/gpt-oss-120b": (20, 82),
    "timeweb/gemma4:31b": (189, 540),
    "gemini/gemini-3.1-flash-lite": (34, 203),
    "openai/gpt-4.1-mini": (54, 216),
    "anthropic/claude-haiku-4-5": (135, 1080),
    "anthropic/claude-haiku-5-5": (14, 68),
    "openai/gpt-5.6-terra": (270, 1620),
}
# Unknown model: assume the most expensive known price, so budgets stay on the safe side.
FALLBACK = max(PRICES_RUB_PER_1M.values(), key=lambda p: p[0] + p[1])

CHARS_PER_TOKEN_IN = 2
# A later prompt embeds earlier answers (the parse, the spec). Their size is bounded by
# max_tokens of the step that wrote them, at most this many characters per token.
CHARS_PER_TOKEN_OUT = 4
# Block markers, headings and the remarks around the embedded inputs.
WRAP_CHARS = 1_000
# Screen text the acceptance reviewer gets (docs/SPEC.md: up to 8000 characters in all).
SCREENS_TOTAL_CHARS = 8_000


def call_cost_rub(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES_RUB_PER_1M.get(model, FALLBACK)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def prompt_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))


def call_worst_rub(model: str, chars: int, max_tokens: int) -> float:
    """The most a call can cost: ``chars`` of prompt and ``max_tokens`` of answer."""
    return call_cost_rub(model, math.ceil(chars / CHARS_PER_TOKEN_IN), max_tokens)


def messages_worst_rub(model: str, messages: list[dict], max_tokens: int) -> float:
    return call_worst_rub(model, prompt_chars(messages), max_tokens)


def step_input_chars(settings, step: str) -> int:
    """Upper bound of a step's prompt without its system prompt (the brief, earlier answers)."""
    brief = settings.max_brief_chars
    parse_out = settings.parse_max_tokens * CHARS_PER_TOKEN_OUT
    spec_out = settings.spec_max_tokens * CHARS_PER_TOKEN_OUT
    return WRAP_CHARS + {
        "parse": brief,
        "spec": brief + parse_out,
        "review": brief + spec_out,
        "prototype": spec_out,
        "accept_review": spec_out + SCREENS_TOTAL_CHARS,
    }[step]


def step_worst_rub(settings, step: str) -> float:
    from app.prompts import SYSTEM  # prompts are plain constants; imported late to keep pricing light

    conf = settings.step(step)
    return call_worst_rub(conf.model, len(SYSTEM[step]) + step_input_chars(settings, step), conf.max_tokens)


def run_worst_rub(settings) -> float:
    """One pass without rework and retries: parse, spec, review, prototype, acceptance review.
    A run starts only if the rest of the daily budget covers it (about 5 ₽ with the defaults)."""
    return sum(step_worst_rub(settings, s) for s in ("parse", "spec", "review", "prototype", "accept_review"))
