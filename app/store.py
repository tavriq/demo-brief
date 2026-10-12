"""SQLite: runs per client per hour, the daily budget in rubles, runs with their events and
prototypes for ``retention_days``.

Budget flow, as in demo-arena and demo-voice: ``reserve`` checks that today's spend + the call's
worst case fit the daily budget and records the worst case; ``settle`` replaces it with the real
cost. A run holds one reservation for its whole main pass (``hold``, status "held") and moves its
steps' shares out of it call by call (``reserve(..., hold=…)``), so a second run or an extra call
cannot take that money. Holds left by a dead process are freed on startup (``release_holds``).

Client keys (IPv4 or IPv6 /64) are stored only as HMAC hashes with a salt that lives in memory
and changes on every start, and only for the hourly limit. Runs keep the brief with contacts
masked (app.pii), the model answers and the prototype, no client key.

A connection per call (WAL), so the store is safe to share between threads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HOUR = 3600
MSK = timezone(timedelta(hours=3))
FINAL_EVENTS = ("done", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS starts (ip_hash TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS starts_ip_ts ON starts (ip_hash, ts);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ts REAL NOT NULL,
    rub REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_day ON spend (day);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_ts ON runs (ts);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    t_ms INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS prototypes (run_id TEXT PRIMARY KEY, html TEXT NOT NULL);
"""

RESTART_MESSAGE = "Сервис перезапустился во время прогона. Запустите прогон ещё раз."


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str | None = None
    message: str | None = None
    reservation_id: int | None = None
    retry_after_s: int | None = None


def rub_text(value: float) -> str:
    """15 -> «15 ₽», 3.1 -> «3,10 ₽»."""
    return f"{value:.0f} ₽" if value == int(value) else f"{value:.2f} ₽".replace(".", ",")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, db_path: str | Path, daily_budget_rub: float, runs_per_hour: int,
                 retention_days: int = 30, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.daily_budget_rub = daily_budget_rub
        self.runs_per_hour = runs_per_hour
        self.retention_s = retention_days * 86400
        self.clock = clock
        self._salt = secrets.token_bytes(16)
        self._purged_at = 0.0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM starts")  # hashes with an old salt only keep personal data around
        self.purge()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write(self, fn):
        """Run ``fn(conn)`` in one IMMEDIATE transaction and return its result."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def day(self, ts: float | None = None) -> str:
        """Budget day in Moscow time: the page says "до 00:00 МСК"."""
        return datetime.fromtimestamp(self.clock() if ts is None else ts, tz=MSK).date().isoformat()

    def _hash(self, key: str) -> str:
        return hmac.new(self._salt, key.encode(), hashlib.sha256).hexdigest()

    # ---------- limits ----------

    def start(self, client_key: str) -> Decision:
        """A new run: at most ``runs_per_hour`` per client."""
        now = self.clock()
        key = self._hash(client_key)

        def go(conn):
            conn.execute("DELETE FROM starts WHERE ts <= ?", (now - HOUR,))
            count, oldest = conn.execute("SELECT COUNT(*), MIN(ts) FROM starts WHERE ip_hash = ?",
                                         (key,)).fetchone()
            if count >= self.runs_per_hour:
                retry = max(1, math.ceil(oldest + HOUR - now))
                return Decision(False, "rate_limited",
                                f"Не больше {self.runs_per_hour} прогонов в час с одного адреса. "
                                f"Попробуйте через {max(1, (retry + 59) // 60)} мин. Записанные примеры "
                                "работают и сейчас.", retry_after_s=retry)
            conn.execute("INSERT INTO starts (ip_hash, ts) VALUES (?, ?)", (key, now))
            return Decision(True)

        return self._write(go)

    def budget_message(self) -> str:
        return (f"Дневной бюджет демо ({rub_text(self.daily_budget_rub)}) на сегодня закончился, живой прогон — "
                "после 00:00 по Москве. Записанные примеры работают и сейчас.")

    def hold(self, worst_rub: float) -> Decision:
        """Hold a run's main pass: like ``reserve``, released with ``settle(id, 0)``."""
        return self.reserve(worst_rub, status="held")

    def reserve(self, worst_rub: float, hold: tuple[int, float] | None = None, status: str = "reserved") -> Decision:
        """Reserve ``worst_rub`` if today's spend leaves room. ``hold``: (id of a hold, rubles) —
        up to that many rubles move out of the hold into this reservation in the same
        transaction; a refusal leaves the hold as it was."""
        now = self.clock()
        today = self.day(now)

        def go(conn):
            spent = conn.execute("SELECT COALESCE(SUM(rub), 0) FROM spend WHERE day = ?", (today,)).fetchone()[0]
            moved, freed = 0.0, 0.0
            if hold is not None:
                row = conn.execute("SELECT rub, day FROM spend WHERE id = ? AND status = 'held'",
                                   (hold[0],)).fetchone()
                if row:
                    moved = min(float(row[0]), max(0.0, float(hold[1])))
                    freed = moved if row[1] == today else 0.0  # a hold from yesterday is not in today's spend
            if spent - freed + worst_rub > self.daily_budget_rub:
                return Decision(False, "budget_exhausted", self.budget_message())
            if moved:
                conn.execute("UPDATE spend SET rub = MAX(0, rub - ?) WHERE id = ?", (moved, hold[0]))
            cur = conn.execute("INSERT INTO spend (day, ts, rub, status) VALUES (?, ?, ?, ?)",
                               (today, now, float(worst_rub), status))
            return Decision(True, reservation_id=cur.lastrowid)

        return self._write(go)

    def settle(self, reservation_id: int, rub: float) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE spend SET rub = ?, status = 'settled' WHERE id = ?", (float(rub), reservation_id))

    def spent_today(self) -> float:
        with closing(self._connect()) as conn:
            return float(conn.execute("SELECT COALESCE(SUM(rub), 0) FROM spend WHERE day = ?",
                                      (self.day(),)).fetchone()[0])

    def left_today(self) -> float:
        return self.daily_budget_rub - self.spent_today()

    # ---------- runs ----------

    def create_run(self, run_id: str, state: dict) -> None:
        """``state`` must already hold the masked brief only (app.main masks before it stores)."""
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO runs (id, ts, updated, status, state) VALUES (?, ?, ?, ?, ?)",
                         (run_id, now, now, state.get("status", "running"), _dumps(state)))
        self.maybe_purge()

    def save_run(self, run_id: str, state: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE runs SET state = ?, status = ?, updated = ? WHERE id = ?",
                         (_dumps(state), state.get("status", "running"), self.clock(), run_id))

    def load_run(self, run_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT state FROM runs WHERE id = ? AND ts > ?",
                               (run_id, self.clock() - self.retention_s)).fetchone()
        return json.loads(row[0]) if row else None

    def run_status(self, run_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ? AND ts > ?",
                               (run_id, self.clock() - self.retention_s)).fetchone()
        return row[0] if row else None

    def add_event(self, run_id: str, t_ms: int, type_: str, data: dict | None = None) -> int:
        """Append an event; returns its number in the run (1, 2, …)."""
        def go(conn):
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                               (run_id,)).fetchone()[0]
            conn.execute("INSERT INTO events (run_id, seq, t_ms, type, data) VALUES (?, ?, ?, ?, ?)",
                         (run_id, seq, int(t_ms), type_, _dumps(data or {})))
            return seq

        return self._write(go)

    def finish_run(self, run_id: str, state: dict, t_ms: int, type_: str, data: dict) -> int:
        """The final state and the final event ("done"/"failed") in one transaction: a run is
        finished exactly when its final event exists, so SSE readers never miss it."""
        def go(conn):
            conn.execute("UPDATE runs SET state = ?, status = ?, updated = ? WHERE id = ?",
                         (_dumps(state), state.get("status", "failed"), self.clock(), run_id))
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                               (run_id,)).fetchone()[0]
            conn.execute("INSERT INTO events (run_id, seq, t_ms, type, data) VALUES (?, ?, ?, ?, ?)",
                         (run_id, seq, int(t_ms), type_, _dumps(data)))
            return seq

        return self._write(go)

    def final_seq(self, run_id: str) -> int | None:
        """The number of the run's "done"/"failed" event, None while it runs."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MIN(seq) FROM events WHERE run_id = ? AND type IN (?, ?)",
                               (run_id, *FINAL_EVENTS)).fetchone()
        return row[0] if row else None

    def events_after(self, run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
        """Events with seq > ``after_seq``: ``{"seq", "t_ms", "type", **data}``."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT seq, t_ms, type, data FROM events WHERE run_id = ? AND seq > ? "
                                "ORDER BY seq LIMIT ?", (run_id, int(after_seq), limit)).fetchall()
        return [{"seq": seq, "t_ms": t_ms, "type": type_, **json.loads(data)} for seq, t_ms, type_, data in rows]

    def save_prototype(self, run_id: str, html: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("INSERT OR REPLACE INTO prototypes (run_id, html) VALUES (?, ?)", (run_id, html))

    def load_prototype(self, run_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT p.html FROM prototypes p JOIN runs r ON r.id = p.run_id "
                               "WHERE p.run_id = ? AND r.ts > ?",
                               (run_id, self.clock() - self.retention_s)).fetchone()
        return row[0] if row else None

    def release_holds(self) -> int:
        """Free the holds of runs a previous process left: nothing was spent from what is left of
        them. Their calls in flight stay reserved at the worst case. Call once on startup."""
        with closing(self._connect()) as conn:
            return conn.execute("UPDATE spend SET rub = 0, status = 'settled' WHERE status = 'held'").rowcount

    def fail_stale_runs(self) -> int:
        """Runs left "running" by a previous process: failed with code "restart" and a final event,
        so their pages stop waiting. Call once on startup, before any new run."""
        now = self.clock()
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, ts, state FROM runs WHERE status = 'running'").fetchall()
        for run_id, ts, raw in rows:
            state = json.loads(raw)
            error = {"code": "restart", "message": RESTART_MESSAGE}
            state.update(status="failed", error=error)
            self.finish_run(run_id, state, int((now - ts) * 1000), "failed", error)
        return len(rows)

    def counters(self) -> dict:
        since = self.clock() - 86400
        with closing(self._connect()) as conn:
            rows = dict(conn.execute("SELECT status, COUNT(*) FROM runs WHERE ts > ? GROUP BY status",
                                     (since,)).fetchall())
        return {"runs_24h": int(sum(rows.values())), "done_24h": int(rows.get("done", 0)),
                "failed_24h": int(rows.get("failed", 0))}

    def maybe_purge(self) -> None:
        if self.clock() - self._purged_at >= HOUR:
            self.purge()

    def purge(self) -> None:
        now = self.clock()
        self._purged_at = now
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM runs WHERE ts <= ?", (now - self.retention_s,))
            conn.execute("DELETE FROM events WHERE run_id NOT IN (SELECT id FROM runs)")
            conn.execute("DELETE FROM prototypes WHERE run_id NOT IN (SELECT id FROM runs)")
            conn.execute("DELETE FROM starts WHERE ts <= ?", (now - HOUR,))
            conn.execute("DELETE FROM spend WHERE ts <= ?", (now - 3 * 86400,))


class CapStore(Store):
    """A store whose budget is one cap for its whole life instead of a day's (scripts and evals
    with --max-rub): a recording or an eval started before midnight does not get a fresh budget
    after it."""

    def day(self, ts: float | None = None) -> str:
        return "cap"
