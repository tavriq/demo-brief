import json
import time

from fastapi.testclient import TestClient

from app import checks
from app.main import load_examples
from scripts import dev_server, record
from scripts.demo_gateway import PROTOTYPE, SPEC_V1, SPEC_V2


def test_demo_answers_are_what_they_claim():
    assert checks.check_spec(SPEC_V2)[1] == [] and checks.check_prototype(PROTOTYPE, SPEC_V2) == []
    assert [i["id"] for i in checks.check_spec(SPEC_V1)[1]] == ["K7"]
    assert len(checks.screens_text(PROTOTYPE)) == 3


def test_record_dry_run(tmp_path, capsys):
    assert record.main(["--dry-run", "--out", str(tmp_path), "--name", "demo"]) == 0
    data = json.loads((tmp_path / "demo.json").read_text(encoding="utf-8"))
    assert set(data) == {"name", "title", "brief", "state", "events", "prototype_html"}
    state = data["state"]
    assert state["status"] == "done" and [v["red"] for v in state["specs"]] == [True, False]
    assert state["prototype"]["url"] == "/api/brief/examples/demo/prototype"
    assert data["events"][-1]["type"] == "done" and data["events"][-1]["t_ms"] > 5000  # the virtual clock moved
    assert data["prototype_html"].count("data-screen=") == 3
    assert state["totals"]["accepted"] == 7 and state["totals"]["total"] == 8
    assert "7 из 8" in capsys.readouterr().out
    assert load_examples(tmp_path)["demo"]["state"]["status"] == "done"
    # no overwrite without --force, and the name is checked
    assert record.main(["--dry-run", "--out", str(tmp_path), "--name", "demo"]) == 1
    assert record.main(["--dry-run", "--out", str(tmp_path), "--name", "demo", "--force"]) == 0
    assert record.main(["--dry-run", "--out", str(tmp_path), "--name", "../evil"]) == 1
    assert not (tmp_path.parent / "evil.json").exists()


def test_record_live_needs_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert record.main(["--out", str(tmp_path), "--name", "x", "--title", "т", "--brief", "б"]) == 2
    assert record.main(["--out", str(tmp_path)]) == 2 and not list(tmp_path.iterdir())


def test_record_dry_run_never_writes_into_app_recorded(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setattr(record, "ROOT", root)
    monkeypatch.setattr(record.tempfile, "tempdir", str(tmp_path))  # the temp folder stays in tmp_path
    assert record.main(["--dry-run", "--out", str(root / "app" / "recorded")]) == 2
    assert record.main(["--dry-run"]) == 0 and not (root / "app" / "recorded").exists()
    written = capsys.readouterr().out.split(":")[0]
    assert "demo-brief-dry-run-" in written and written.endswith("demo.json")


def test_record_refuses_a_cap_below_one_pass(tmp_path):
    assert record.main(["--dry-run", "--out", str(tmp_path), "--max-rub", "1"]) == 2


def _without_run_identity(data: dict) -> dict:
    state = {k: v for k, v in data["state"].items() if k not in ("id", "created")}
    return {**data, "state": state}


def test_fixture_is_the_current_dry_run(tmp_path):
    # SPEC «Записанные прогоны»: the page is developed on a fake file in tests/fixtures/, never in app/recorded
    fixture = json.loads((dev_server.FIXTURES / "demo.json").read_text(encoding="utf-8"))
    assert record.main(["--dry-run", "--out", str(tmp_path)]) == 0
    fresh = json.loads((tmp_path / "demo.json").read_text(encoding="utf-8"))
    assert _without_run_identity(fixture) == _without_run_identity(fresh), \
        "stale fixture: python3 scripts/record.py --dry-run --out tests/fixtures --force"
    assert sorted(p.name for p in dev_server.FIXTURES.iterdir()) == ["demo.json"]
    assert not (record.ROOT / "app" / "recorded" / "demo.json").exists()


def test_dev_server_app(tmp_path):
    before = {p.name: p.stat().st_mtime_ns for p in dev_server.FIXTURES.iterdir()}
    client = TestClient(dev_server.build(tmp_path, speed=0))
    assert client.get("/api/brief/examples").json()[0]["name"] == "demo"
    assert client.get("/api/brief/examples/demo/prototype").status_code == 200
    run_id = client.post("/api/brief/runs", json={"brief": "Нужна запись в барбершоп"}).json()["id"]
    deadline = time.monotonic() + 5
    while (state := client.get(f"/api/brief/runs/{run_id}").json())["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert state["status"] == "done" and len(state["specs"]) == 2
    assert client.get(state["prototype"]["url"]).status_code == 200
    assert {p.name: p.stat().st_mtime_ns for p in dev_server.FIXTURES.iterdir()} == before


def test_dev_server_serves_the_page(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<!doctype html><title>page</title>", encoding="utf-8")
    client = TestClient(dev_server.build(tmp_path / "data", speed=0, site=site))
    assert "page" in client.get("/").text and client.get("/api/brief/health").json() == {"ok": True}
