"""Settings from the environment. The key is never logged or returned."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path

# Probe 08.10 (evals/probe-models-2026-10-08.jsonl): Haiku writes the fullest spec and prototype,
# flash-lite parses the brief fast, the reviewer is another family (OpenAI) and answers in ~2.4 s.
DEFAULT_PARSE_MODEL = "gemini/gemini-3.1-flash-lite"
DEFAULT_MODEL = "anthropic/claude-haiku-5-5"
DEFAULT_REVIEW_MODEL = "openai/gpt-4.1-mini"

# Model steps of the pipeline (docs/SPEC.md, «Шаги конвейера»): env prefix, max_tokens, timeout in s.
STEPS = {
    "parse": ("PARSE", 1200, 40),
    "spec": ("SPEC", 7000, 90),
    "review": ("REVIEW", 1500, 60),
    "prototype": ("PROTO", 12000, 150),
    "accept_review": ("ACCEPT", 1500, 60),
}


def parse_trusted_proxies(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    """TRUSTED_PROXY: comma-separated IPs or CIDR ranges. Empty = trust nobody."""
    return tuple(ip_network(p.strip(), strict=False) for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class StepConf:
    model: str
    max_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    parse_model: str
    spec_model: str
    review_model: str
    proto_model: str
    daily_budget_rub: float
    runs_per_hour: int
    max_concurrent_runs: int
    max_brief_chars: int
    run_timeout_s: float
    retention_days: int
    var_dir: Path
    recorded_dir: Path
    trusted_proxies: tuple
    parse_max_tokens: int
    spec_max_tokens: int
    review_max_tokens: int
    proto_max_tokens: int
    accept_max_tokens: int
    parse_timeout_s: float
    spec_timeout_s: float
    review_timeout_s: float
    proto_timeout_s: float
    accept_timeout_s: float

    @property
    def live(self) -> bool:
        return bool(self.api_key and self.base_url)

    def step(self, name: str) -> StepConf:
        """Model, max_tokens and timeout of a model step (a key of STEPS)."""
        prefix = STEPS[name][0].lower()
        model = self.review_model if name == "accept_review" else getattr(self, f"{prefix}_model")
        return StepConf(model, getattr(self, f"{prefix}_max_tokens"), getattr(self, f"{prefix}_timeout_s"))

    def models(self) -> dict[str, str]:
        return {name: self.step(name).model for name in STEPS}

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        root = Path(__file__).resolve().parent.parent
        per_step = {}
        for prefix, max_tokens, timeout_s in STEPS.values():
            p = prefix.lower()
            per_step[f"{p}_max_tokens"] = int(env.get(f"{prefix}_MAX_TOKENS", str(max_tokens)))
            per_step[f"{p}_timeout_s"] = float(env.get(f"{prefix}_TIMEOUT_S", str(timeout_s)))
        return cls(
            api_key=env.get("LLM_API_KEY", "").strip(),
            base_url=env.get("LLM_BASE_URL", "").strip(),
            parse_model=env.get("PARSE_MODEL", DEFAULT_PARSE_MODEL).strip(),
            spec_model=env.get("SPEC_MODEL", DEFAULT_MODEL).strip(),
            review_model=env.get("REVIEW_MODEL", DEFAULT_REVIEW_MODEL).strip(),
            proto_model=env.get("PROTO_MODEL", DEFAULT_MODEL).strip(),
            daily_budget_rub=float(env.get("DAILY_BUDGET_RUB", "15")),
            runs_per_hour=int(env.get("RUNS_PER_HOUR", "3")),
            max_concurrent_runs=int(env.get("MAX_CONCURRENT_RUNS", "2")),
            max_brief_chars=int(env.get("MAX_BRIEF_CHARS", "500")),
            run_timeout_s=float(env.get("RUN_TIMEOUT_S", "300")),
            retention_days=int(env.get("RETENTION_DAYS", "30")),
            var_dir=Path(env.get("VAR_DIR", str(root / "var"))),
            recorded_dir=Path(env.get("RECORDED_DIR", str(root / "app" / "recorded"))),
            trusted_proxies=parse_trusted_proxies(env.get("TRUSTED_PROXY", "")),
            **per_step,
        )
