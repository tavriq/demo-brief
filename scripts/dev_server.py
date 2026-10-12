"""Local server for page development: the app on the scripted demo gateway (no key, no money) with
the fake recorded example from tests/fixtures/ (docs/SPEC.md, «Записанные прогоны»); runs go to a
fresh temp dir. Never used in production.

    python3 scripts/dev_server.py --port 8765 [--site ../tavriq-site] [--speed 0.5]

The API is at http://127.0.0.1:<port>/api/brief/…; --site also serves the page from the same
origin (the prototype CSP needs frame-ancestors 'self'). tavriq-site's build.py writes pages into
the repo root, so --site is that folder after a build. The fixture is refreshed with
``python3 scripts/record.py --dry-run --out tests/fixtures --force``.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from scripts.demo_gateway import DemoGateway  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def dev_settings(tmp: Path) -> Settings:
    return replace(Settings.from_env({}), api_key="dev-fake", base_url="http://demo-gateway.invalid",
                   var_dir=tmp / "var", recorded_dir=FIXTURES, runs_per_hour=1000)


def build(tmp: Path, speed: float = 1.0, site: Path | None = None):
    """The app on DemoGateway(speed) with the fixture example; with ``site`` also the page."""
    api = create_app(dev_settings(tmp), client=DemoGateway(speed=speed))
    if site is None:
        return api
    from starlette.staticfiles import StaticFiles

    static = StaticFiles(directory=site, html=True)

    async def app(scope, receive, send):
        target = api if scope["type"] != "http" or scope["path"].startswith("/api/") else static
        await target(scope, receive, send)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--speed", type=float, default=1.0, help="scale of the fake model delays, 0 = instant")
    ap.add_argument("--site", type=Path, default=None, help="a built page to serve at /")
    args = ap.parse_args(argv)
    import uvicorn

    tmp = Path(tempfile.mkdtemp(prefix="demo-brief-dev-"))
    print(f"demo-brief dev: http://127.0.0.1:{args.port}/api/brief/status (data in {tmp})")
    uvicorn.run(build(tmp, args.speed, args.site), host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
