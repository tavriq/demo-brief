from dataclasses import replace

from app.config import Settings
from app.pricing import call_cost_rub, call_worst_rub, messages_worst_rub, run_worst_rub, step_worst_rub
from tests.fakes import PINNED_ENV


def test_prices():
    # 1M in + 1M out of flash-lite: 34 + 203 ₽
    assert call_cost_rub("gemini/gemini-3.1-flash-lite", 1_000_000, 1_000_000) == 237
    assert round(call_cost_rub("anthropic/claude-haiku-5-5", 2000, 1000), 4) == 0.096


def test_worst_call_rounds_the_prompt_up():
    # 3 characters -> 2 tokens in
    assert call_worst_rub("gemini/gemini-3.1-flash-lite", 3, 0) == call_cost_rub("gemini/gemini-3.1-flash-lite", 2, 0)
    messages = [{"role": "system", "content": "x" * 1000}, {"role": "user", "content": "y" * 1001}]
    assert messages_worst_rub("gemini/gemini-3.1-flash-lite", messages, 100) == \
        call_cost_rub("gemini/gemini-3.1-flash-lite", 1001, 100)


def test_one_pass_is_about_five_rubles():
    s = Settings.from_env(PINNED_ENV)
    worst = run_worst_rub(s)
    assert 4.5 < worst < 6.5
    assert worst < s.daily_budget_rub / 2  # at least two runs a day fit
    # the prototype is the expensive part: 14000 tokens out
    assert step_worst_rub(s, "prototype") > 2.8


def test_default_models_after_the_probe():
    # probe 08.10: Haiku writes the spec and the prototype, a pass is cheaper than with flash-lite
    s = Settings.from_env({})
    assert (s.spec_model, s.proto_model, s.review_model) == \
        ("anthropic/claude-haiku-5-5", "anthropic/claude-haiku-5-5", "openai/gpt-4.1-mini")
    assert 3.0 < run_worst_rub(s) < 4.5 and run_worst_rub(s) < s.daily_budget_rub / 3


def test_worst_follows_settings():
    s = Settings.from_env(PINNED_ENV)
    assert run_worst_rub(replace(s, proto_max_tokens=7000)) < run_worst_rub(s)
    assert run_worst_rub(replace(s, review_model="someone/new-model")) > run_worst_rub(s)


def test_unknown_model_priced_as_the_most_expensive():
    assert call_cost_rub("someone/new-model", 1000, 1000) >= call_cost_rub("openai/gpt-5.6-terra", 1000, 1000)
