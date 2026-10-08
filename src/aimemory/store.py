from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from aimemory.facts import DEFAULT_PRIORITY, AppFacts


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                app TEXT NOT NULL,
                unit TEXT,
                action TEXT NOT NULL,
                recommended TEXT,
                zone TEXT,
                memory_bytes INTEGER,
                idle_hours REAL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback (
                app TEXT PRIMARY KEY,
                accepts INTEGER NOT NULL DEFAULT 0,
                dismisses INTEGER NOT NULL DEFAULT 0,
                last_ts REAL
            );
            CREATE TABLE IF NOT EXISTS app_facts (
                app TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                background_use INTEGER NOT NULL,
                unsaved_work_risk INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 5,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_stats (
                app TEXT PRIMARY KEY,
                window_start REAL NOT NULL,
                focus_seconds REAL NOT NULL DEFAULT 0,
                audio_seconds REAL NOT NULL DEFAULT 0,
                unfocused_seconds REAL NOT NULL DEFAULT 0,
                cpu_busy_seconds REAL NOT NULL DEFAULT 0,
                memory_peak INTEGER NOT NULL DEFAULT 0,
                memory_sum INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                swap_last INTEGER NOT NULL DEFAULT 0,
                process_start_ts REAL,
                last_ts REAL NOT NULL
            );
            """
        )
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(app_facts)").fetchall()
        }
        if "priority" not in cols:
            self._conn.execute(
                "ALTER TABLE app_facts ADD COLUMN priority INTEGER NOT NULL DEFAULT 5"
            )
            # Force a re-lookup so Gemini fills real priorities.
            self._conn.execute("UPDATE app_facts SET ts = 0")
        self._conn.commit()

    def get_facts(self, app: str) -> AppFacts | None:
        row = self._conn.execute(
            "SELECT display_name, kind, background_use, unsaved_work_risk, priority"
            " FROM app_facts WHERE app = ?",
            (app,),
        ).fetchone()
        if row is None:
            return None
        priority = row["priority"]
        return AppFacts(
            display_name=row["display_name"],
            kind=row["kind"],
            background_use=bool(row["background_use"]),
            unsaved_work_risk=bool(row["unsaved_work_risk"]),
            priority=int(priority) if priority is not None else DEFAULT_PRIORITY,
        )

    def fact_age_s(self, app: str) -> float | None:
        row = self._conn.execute(
            "SELECT ts FROM app_facts WHERE app = ?", (app,)
        ).fetchone()
        if row is None:
            return None
        return max(0.0, time.time() - float(row["ts"]))

    def stale_fact_apps(self, apps: list[str], *, ttl_s: float) -> list[str]:
        """Apps with no facts or facts older than ttl_s."""
        now = time.time()
        stale: list[str] = []
        for app in apps:
            row = self._conn.execute(
                "SELECT ts FROM app_facts WHERE app = ?", (app,)
            ).fetchone()
            if row is None or now - float(row["ts"]) >= ttl_s:
                stale.append(app)
        return stale

    def all_facts(self) -> dict[str, AppFacts]:
        rows = self._conn.execute("SELECT app FROM app_facts ORDER BY app").fetchall()
        return {r["app"]: f for r in rows if (f := self.get_facts(r["app"]))}

    def put_facts(self, facts: dict[str, AppFacts]) -> None:
        now = time.time()
        self._conn.executemany(
            "INSERT OR REPLACE INTO app_facts"
            " (app, display_name, kind, background_use, unsaved_work_risk, priority, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    app,
                    f.display_name,
                    f.kind,
                    int(f.background_use),
                    int(f.unsaved_work_risk),
                    int(f.priority),
                    now,
                )
                for app, f in facts.items()
            ],
        )
        self._conn.commit()

    def clear_facts(self) -> None:
        self._conn.execute("DELETE FROM app_facts")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def log_event(
        self,
        *,
        app: str,
        unit: str | None,
        action: str,
        recommended: str | None,
        zone: str | None,
        memory_bytes: int | None,
        idle_hours: float | None,
        detail: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO events (ts, app, unit, action, recommended, zone, memory_bytes, idle_hours, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                app,
                unit,
                action,
                recommended,
                zone,
                memory_bytes,
                idle_hours,
                detail,
            ),
        )
        self._conn.commit()

    def record_accept(self, app: str) -> None:
        self._upsert_feedback(app, accept=True)

    def record_dismiss(self, app: str) -> None:
        self._upsert_feedback(app, accept=False)

    def feedback_counts(self, app: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT accepts, dismisses FROM feedback WHERE app = ?", (app,)
        ).fetchone()
        if row is None:
            return 0, 0
        return int(row["accepts"]), int(row["dismisses"])

    def _upsert_feedback(self, app: str, *, accept: bool) -> None:
        row = self._conn.execute(
            "SELECT accepts, dismisses FROM feedback WHERE app = ?", (app,)
        ).fetchone()
        now = time.time()
        if row is None:
            self._conn.execute(
                "INSERT INTO feedback (app, accepts, dismisses, last_ts) VALUES (?, ?, ?, ?)",
                (app, 1 if accept else 0, 0 if accept else 1, now),
            )
        else:
            accepts = int(row["accepts"]) + (1 if accept else 0)
            dismisses = int(row["dismisses"]) + (0 if accept else 1)
            self._conn.execute(
                "UPDATE feedback SET accepts = ?, dismisses = ?, last_ts = ? WHERE app = ?",
                (accepts, dismisses, now, app),
            )
        self._conn.commit()

    def feedback_factor(self, app: str) -> float:
        """Lower factor after repeated dismissals (min 0.15)."""
        accepts, dismisses = self.feedback_counts(app)
        if dismisses <= 0:
            return 1.0
        factor = max(0.15, 1.0 - 0.25 * dismisses + 0.1 * accepts)
        return min(1.0, factor)

    def record_usage_sample(
        self,
        *,
        app: str,
        dt_s: float,
        focused: bool,
        playing_audio: bool,
        cpu_seconds: float,
        memory_bytes: int,
        swap_bytes: int,
        process_start_ts: float | None,
        now: float | None = None,
        window_s: float = 24 * 3600,
    ) -> None:
        """Accumulate one sampling interval into the rolling 24h usage window."""
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT * FROM usage_stats WHERE app = ?", (app,)
        ).fetchone()
        if row is None or now - float(row["window_start"]) >= window_s:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO usage_stats (
                    app, window_start, focus_seconds, audio_seconds, unfocused_seconds,
                    cpu_busy_seconds, memory_peak, memory_sum, sample_count, swap_last,
                    process_start_ts, last_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    app,
                    now,
                    dt_s if focused else 0.0,
                    dt_s if playing_audio else 0.0,
                    0.0 if focused else dt_s,
                    0.0 if focused else max(0.0, cpu_seconds),
                    memory_bytes,
                    memory_bytes,
                    swap_bytes,
                    process_start_ts,
                    now,
                ),
            )
        else:
            focus_s = float(row["focus_seconds"]) + (dt_s if focused else 0.0)
            audio_s = float(row["audio_seconds"]) + (dt_s if playing_audio else 0.0)
            unfocused_s = float(row["unfocused_seconds"]) + (0.0 if focused else dt_s)
            cpu_s = float(row["cpu_busy_seconds"]) + (
                0.0 if focused else max(0.0, cpu_seconds)
            )
            peak = max(int(row["memory_peak"]), memory_bytes)
            mem_sum = int(row["memory_sum"]) + memory_bytes
            samples = int(row["sample_count"]) + 1
            start_ts = row["process_start_ts"]
            if process_start_ts is not None:
                start_ts = (
                    process_start_ts
                    if start_ts is None
                    else min(float(start_ts), process_start_ts)
                )
            self._conn.execute(
                """
                UPDATE usage_stats SET
                    focus_seconds = ?, audio_seconds = ?, unfocused_seconds = ?,
                    cpu_busy_seconds = ?, memory_peak = ?, memory_sum = ?,
                    sample_count = ?, swap_last = ?, process_start_ts = ?, last_ts = ?
                WHERE app = ?
                """,
                (
                    focus_s,
                    audio_s,
                    unfocused_s,
                    cpu_s,
                    peak,
                    mem_sum,
                    samples,
                    swap_bytes,
                    start_ts,
                    now,
                    app,
                ),
            )
        self._conn.commit()

    def usage_rollup(self, app: str, *, now: float | None = None) -> dict[str, float | int | None]:
        """Rolling-window usage for Gemini; empty-ish defaults if never sampled."""
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT * FROM usage_stats WHERE app = ?", (app,)
        ).fetchone()
        since = now - 7 * 24 * 3600
        ev = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN action IN ('close', 'reclaim') THEN 1 ELSE 0 END) AS accepts,
                SUM(CASE WHEN action = 'ignore' THEN 1 ELSE 0 END) AS dismisses,
                COUNT(*) AS total
            FROM events WHERE app = ? AND ts >= ?
            """,
            (app, since),
        ).fetchone()
        accepts_7d = int(ev["accepts"] or 0)
        dismisses_7d = int(ev["dismisses"] or 0)
        suggestions_7d = int(ev["total"] or 0)

        if row is None:
            return {
                "window_hours": 0.0,
                "focus_minutes": 0.0,
                "audio_minutes": 0.0,
                "unfocused_minutes": 0.0,
                "cpu_busy_ratio_unfocused": None,
                "memory_peak_bytes": None,
                "memory_avg_bytes": None,
                "swap_bytes": None,
                "process_age_hours": None,
                "suggestions_7d": suggestions_7d,
                "accepts_7d": accepts_7d,
                "dismisses_7d": dismisses_7d,
            }

        unfocused = float(row["unfocused_seconds"])
        cpu_busy = float(row["cpu_busy_seconds"])
        samples = max(int(row["sample_count"]), 1)
        start = row["process_start_ts"]
        age_h = (now - float(start)) / 3600.0 if start is not None else None
        return {
            "window_hours": round((now - float(row["window_start"])) / 3600.0, 2),
            "focus_minutes": round(float(row["focus_seconds"]) / 60.0, 1),
            "audio_minutes": round(float(row["audio_seconds"]) / 60.0, 1),
            "unfocused_minutes": round(unfocused / 60.0, 1),
            "cpu_busy_ratio_unfocused": (
                round(min(1.0, cpu_busy / unfocused), 3) if unfocused > 1.0 else None
            ),
            "memory_peak_bytes": int(row["memory_peak"]),
            "memory_avg_bytes": int(row["memory_sum"]) // samples,
            "swap_bytes": int(row["swap_last"]),
            "process_age_hours": round(age_h, 2) if age_h is not None else None,
            "suggestions_7d": suggestions_7d,
            "accepts_7d": accepts_7d,
            "dismisses_7d": dismisses_7d,
        }

    def all_usage_apps(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT app FROM usage_stats ORDER BY memory_peak DESC"
        ).fetchall()
        return [r["app"] for r in rows]
