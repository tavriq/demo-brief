import sqlite3
import threading
from datetime import datetime

import pytest

from app.pipeline import initial_state
from app.store import MSK, CapStore, Store
from tests.fakes import FakeClock


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 8, 12, 0, tzinfo=MSK).timestamp())


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "s.sqlite3", 15, 3, retention_days=30, clock=clock)


def test_reserve_and_settle(store):
    a = store.reserve(5)
    assert a.allowed and store.spent_today() == 5
    store.settle(a.reservation_id, 0.4)
    assert store.spent_today() == pytest.approx(0.4) and store.left_today() == pytest.approx(14.6)
    assert store.reserve(14.5).allowed
    refused = store.reserve(0.2)
    assert not refused.allowed and refused.code == "budget_exhausted" and "15 ₽" in refused.message


def test_reserve_moves_money_out_of_a_hold(store):
    hold = store.hold(10)
    assert not store.reserve(6).allowed  # only 5 are free: the hold is spent for everyone else
    call = store.reserve(6, hold=(hold.reservation_id, 4))
    assert call.allowed and store.spent_today() == pytest.approx(16 - 4)
    assert not store.reserve(10, hold=(hold.reservation_id, 6)).allowed  # refused: the hold stays as it was
    assert store.reserve(3, hold=(hold.reservation_id, 100)).allowed  # at most what the hold has left
    store.settle(hold.reservation_id, 0)
    assert store.spent_today() == pytest.approx(9)
    assert store.reserve(1, hold=(hold.reservation_id, 5)).allowed and store.spent_today() == pytest.approx(10)


def test_a_hold_from_yesterday_frees_nothing_today(store, clock):
    hold = store.hold(10)
    clock.t = datetime(2026, 10, 9, 0, 1, tzinfo=MSK).timestamp()
    store.reserve(14)
    assert not store.reserve(2, hold=(hold.reservation_id, 5)).allowed


def test_cap_store_does_not_reset_at_midnight(tmp_path, clock):
    capped = CapStore(tmp_path / "c.sqlite3", 15, 3, clock=clock)
    capped.reserve(14)
    clock.t = datetime(2026, 10, 9, 0, 1, tzinfo=MSK).timestamp()
    assert not capped.reserve(2).allowed and capped.left_today() == pytest.approx(1)


def test_budget_day_is_moscow(store, clock):
    store.reserve(15)
    assert not store.reserve(1).allowed
    clock.t = datetime(2026, 10, 8, 23, 59, tzinfo=MSK).timestamp()
    assert not store.reserve(1).allowed
    clock.t = datetime(2026, 10, 9, 0, 1, tzinfo=MSK).timestamp()
    assert store.reserve(1).allowed


def test_hourly_starts_per_client(store, clock):
    assert all(store.start("192.0.2.1").allowed for _ in range(3))
    refused = store.start("192.0.2.1")
    assert not refused.allowed and refused.code == "rate_limited" and refused.retry_after_s == 3600
    assert store.start("192.0.2.2").allowed
    clock.advance(3601)
    assert store.start("192.0.2.1").allowed


def test_no_client_key_in_the_database(store):
    store.start("198.51.100.7")
    dump = "\n".join(str(r) for r in sqlite3.connect(store.db_path).execute("SELECT * FROM starts"))
    assert "198.51.100.7" not in dump


def test_events_and_final_event(store):
    store.create_run("r1", initial_state("r1", "бриф", 1.0))
    assert store.add_event("r1", 10, "step_start", {"step": "parse"}) == 1
    assert store.add_event("r1", 20, "step_end", {"step": "parse", "ok": True}) == 2
    assert store.final_seq("r1") is None
    state = store.load_run("r1")
    state["status"] = "done"
    assert store.finish_run("r1", state, 30, "done", {"status": "done"}) == 3
    assert store.run_status("r1") == "done" and store.final_seq("r1") == 3
    assert store.events_after("r1", 1) == [{"seq": 2, "t_ms": 20, "type": "step_end", "step": "parse", "ok": True},
                                           {"seq": 3, "t_ms": 30, "type": "done", "status": "done"}]


def test_event_numbers_unique_across_threads(store):
    store.create_run("r1", initial_state("r1", "бриф", 1.0))
    seqs = []

    def add():
        for _ in range(20):
            seqs.append(store.add_event("r1", 0, "tick"))

    threads = [threading.Thread(target=add) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seqs) == list(range(1, 81))


def test_prototype_and_unknown_run(store):
    assert store.load_run("nope") is None and store.run_status("nope") is None
    store.create_run("r1", initial_state("r1", "бриф", 1.0))
    store.save_prototype("r1", "<html></html>")
    assert store.load_prototype("r1") == "<html></html>" and store.load_prototype("r2") is None


def test_stale_runs_fail_on_restart(tmp_path, clock):
    path = tmp_path / "s.sqlite3"
    first = Store(path, 15, 3, clock=clock)
    first.create_run("r1", initial_state("r1", "бриф", clock()))
    first.add_event("r1", 5, "step_start", {"step": "parse"})
    clock.advance(12)
    second = Store(path, 15, 3, clock=clock)
    assert second.fail_stale_runs() == 1
    state = second.load_run("r1")
    assert state["status"] == "failed" and state["error"]["code"] == "restart"
    last = second.events_after("r1", 1)
    assert last == [{"seq": 2, "t_ms": 12000, "type": "failed", "code": "restart",
                     "message": state["error"]["message"]}]
    assert second.fail_stale_runs() == 0


def test_retention(store, clock):
    store.create_run("old", initial_state("old", "бриф", clock()))
    store.add_event("old", 0, "step_start")
    store.save_prototype("old", "<html></html>")
    clock.advance(30 * 86400 + 1)
    assert store.load_run("old") is None and store.load_prototype("old") is None  # hidden at once
    store.create_run("new", initial_state("new", "бриф", clock()))  # purges: an hour has passed
    db = sqlite3.connect(store.db_path)
    assert [r[0] for r in db.execute("SELECT id FROM runs")] == ["new"]
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM prototypes").fetchone()[0] == 0


def test_counters(store):
    for i, status in enumerate(["done", "done", "failed", "running"]):
        store.create_run(f"r{i}", {**initial_state(f"r{i}", "б", 1.0), "status": status})
    assert store.counters() == {"runs_24h": 4, "done_24h": 2, "failed_24h": 1}
