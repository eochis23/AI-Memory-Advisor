"""Accumulate per-app usage so Gemini gets a real local snapshot, not a guess."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from aimemory.apps import AppMemory
from aimemory.audio import is_playing
from aimemory.focus import FocusTracker
from aimemory.store import Store

log = logging.getLogger(__name__)

CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
BOOT_TIME: float | None = None


def _boot_time() -> float:
    global BOOT_TIME
    if BOOT_TIME is None:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                BOOT_TIME = float(line.split()[1])
                break
        else:
            BOOT_TIME = time.time() - 1.0
    return BOOT_TIME


def _cpu_ticks(pid: int) -> int | None:
    try:
        # After comm: utime stime cutime cstime are fields 14–17 (1-indexed).
        parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[-1].split()
        return int(parts[11]) + int(parts[12]) + int(parts[13]) + int(parts[14])
    except (OSError, ValueError, IndexError):
        return None


def _process_start_ts(pid: int) -> float | None:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[-1].split()
        start_ticks = int(parts[19])  # starttime
        return _boot_time() + start_ticks / CLK_TCK
    except (OSError, ValueError, IndexError):
        return None


class UsageRecorder:
    """Tracks CPU deltas between ticks and writes rolling 24h stats to the store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._prev_cpu: dict[str, tuple[int, float]] = {}
        self._prev_tick_ts: float | None = None

    def record(
        self,
        apps: list[AppMemory],
        focus: FocusTracker,
        playing: set[Path],
        *,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        dt = 0.0
        if self._prev_tick_ts is not None:
            dt = max(0.0, min(now - self._prev_tick_ts, 30.0))
        self._prev_tick_ts = now
        if dt <= 0:
            for app in apps:
                ticks = _cpu_ticks(app.pids[0]) if app.pids else None
                if ticks is not None:
                    self._prev_cpu[app.name] = (ticks, now)
            return

        for app in apps:
            focused = focus.is_focused_app(app.name, app.pids)
            audio = is_playing(app.cgroup_path, playing)
            cpu_delta_s = 0.0
            if app.pids:
                ticks = _cpu_ticks(app.pids[0])
                prev = self._prev_cpu.get(app.name)
                if ticks is not None and prev is not None:
                    d_ticks = ticks - prev[0]
                    if d_ticks >= 0:
                        cpu_delta_s = d_ticks / CLK_TCK
                if ticks is not None:
                    self._prev_cpu[app.name] = (ticks, now)

            started = _process_start_ts(app.pids[0]) if app.pids else None
            self.store.record_usage_sample(
                app=app.name,
                dt_s=dt,
                focused=focused,
                playing_audio=audio,
                cpu_seconds=cpu_delta_s,
                memory_bytes=app.memory_total,
                swap_bytes=app.memory_swap,
                process_start_ts=started,
                now=now,
            )

        live = {a.name for a in apps}
        self._prev_cpu = {k: v for k, v in self._prev_cpu.items() if k in live}
