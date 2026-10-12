"""Record one run for the page's examples: app/recorded/<name>.json (docs/SPEC.md, «Записанные
прогоны»): ``{"name", "title", "brief", "state", "events", "prototype_html"}``.

Live: runs the pipeline in-process against the real gateway (LLM_API_KEY, LLM_BASE_URL from the
environment, never printed) with a throwaway store capped by --max-rub. The image has no
scripts/, so mount them where the key lives:

    docker run --rm --user "$(id -u)" --env-file ../llm.env -v "$PWD/scripts:/app/scripts:ro" \\
        -v "$PWD/app/recorded:/app/app/recorded" --entrypoint python demo-brief:local scripts/record.py \\
        --name autoservice --title "Онлайн-запись в автосервис" --brief "Нужна онлайн-запись в автосервис…"

--dry-run uses the scripted demo gateway: no key, no money. Its fake example never goes to
app/recorded (the image serves that folder): without --out it is written to a temp folder. The
page's development fixture is this example (tests test that it is up to date):

    python3 scripts/record.py --dry-run --out tests/fixtures --force
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.gateway import ChatClient, Gateway  # noqa: E402
from app.main import EXAMPLE_NAME  # noqa: E402
from app.pii import mask_pii  # noqa: E402
from app.pipeline import Deps, run  # noqa: E402
from app.pricing import run_worst_rub  # noqa: E402
from app.store import CapStore  # noqa: E402
from scripts.demo_gateway import DEMO_BRIEF, DEMO_NAME, DEMO_TITLE, DemoGateway, VirtualClock  # noqa: E402


class RecordError(Exception):
    pass


def record(name: str, title: str, brief: str, gateway: ChatClient, settings: Settings, out_dir: Path,
           clock: Callable[[], float] | None = None, force: bool = False) -> Path:
    """Run the brief once and write ``out_dir/<name>.json``. Only a finished run is written."""
    if not EXAMPLE_NAME.fullmatch(name):
        raise RecordError("name: латиница в нижнем регистре, цифры, «-» и «_», до 40 знаков")
    path = out_dir / f"{name}.json"
    if path.exists() and not force:
        raise RecordError(f"{path} уже есть: --force, чтобы перезаписать")
    masked = mask_pii(brief.strip())[0]
    with tempfile.TemporaryDirectory(prefix="demo-brief-record-") as tmp:
        store = CapStore(Path(tmp) / "record.sqlite3", settings.daily_budget_rub, runs_per_hour=1, retention_days=1)
        run_id = secrets.token_urlsafe(16)
        deps = Deps(gateway, store, settings) if clock is None else Deps(gateway, store, settings, clock)
        state = run(run_id, masked, deps)
        events = store.events_after(run_id, 0, limit=100_000)
        html = store.load_prototype(run_id)
    if state["status"] != "done":
        raise RecordError(f"прогон не дошёл до конца: {state['error']}")
    if isinstance(state.get("prototype"), dict):
        state["prototype"]["url"] = f"/api/brief/examples/{name}/prototype" if html else None
    data = {"name": name, "title": title, "brief": masked, "state": state, "events": events, "prototype_html": html}
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", help="file name in --out; with --dry-run defaults to the demo")
    ap.add_argument("--title")
    ap.add_argument("--brief")
    ap.add_argument("--out", type=Path, help="default: app/recorded; with --dry-run a temp folder")
    ap.add_argument("--max-rub", type=float, default=8.0, help="cap of this recording's spend")
    ap.add_argument("--dry-run", action="store_true", help="scripted demo gateway, no key and no money")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args(argv)

    settings = replace(Settings.from_env(), daily_budget_rub=args.max_rub)
    clock = None
    if args.dry_run:
        args.name, args.title, args.brief = args.name or DEMO_NAME, args.title or DEMO_TITLE, args.brief or DEMO_BRIEF
    if not (args.name and args.title and args.brief):
        print("нужны --name, --title и --brief", file=sys.stderr)
        return 2
    recorded = ROOT / "app" / "recorded"
    if args.out is None:
        args.out = Path(tempfile.mkdtemp(prefix="demo-brief-dry-run-")) if args.dry_run else recorded
    if args.dry_run and args.out.resolve() == recorded.resolve():
        print("--dry-run пишет фейковый пример: не в app/recorded, укажите другой --out", file=sys.stderr)
        return 2
    if args.dry_run:
        settings = replace(settings, api_key="dry-run", base_url="http://demo-gateway.invalid")
        clock = VirtualClock()
        gateway: ChatClient = DemoGateway(clock=clock)
    elif not settings.live:
        print("LLM_API_KEY и LLM_BASE_URL не заданы: живая запись невозможна (есть --dry-run)", file=sys.stderr)
        return 2
    else:
        gateway = Gateway(settings.base_url, settings.api_key)
    worst = run_worst_rub(settings)
    if worst > args.max_rub:
        print(f"один проход стоит до {worst:.2f} ₽, больше --max-rub {args.max_rub}", file=sys.stderr)
        return 2
    try:
        path = record(args.name, args.title, args.brief, gateway, settings, args.out, clock, args.force)
    except RecordError as e:
        print(str(e), file=sys.stderr)
        return 1
    state = json.loads(path.read_text(encoding="utf-8"))["state"]
    t = state["totals"]
    print(f"{path}: {t['accepted']} из {t['total']}, кругов {len(state['specs'])}, {t['ms'] / 1000:.1f} с, "
          f"{t['rub']:.2f} ₽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
