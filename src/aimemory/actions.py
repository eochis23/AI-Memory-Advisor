from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
from dataclasses import dataclass

from aimemory.apps import AppMemory, format_bytes, is_app_unit, read_app
from aimemory.score import RecommendedAction

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    ok: bool
    message: str


def perform(action: RecommendedAction, app: AppMemory) -> ActionResult:
    # The notification may have been open for minutes; act on fresh state so we
    # never signal a recycled PID.
    current = read_app(app.cgroup_path)
    if current is None:
        return ActionResult(True, f"{app.name} is no longer running")
    if action == RecommendedAction.RECLAIM:
        return reclaim(current)
    return close_app(current)


def reclaim(app: AppMemory) -> ActionResult:
    path = app.cgroup_path / "memory.reclaim"
    if not path.is_file():
        return ActionResult(False, f"memory.reclaim not available for {app.name}")
    # ask kernel to reclaim about half of current memory
    amount = max(app.memory_current // 2, 16 * 1024**2)
    try:
        path.write_text(str(amount), encoding="utf-8")
    except OSError as e:
        if e.errno == errno.EAGAIN:
            freed = max(0, app.memory_current - _memory_current(app))
            return ActionResult(
                True,
                f"Reclaimed ~{format_bytes(freed)} from {app.name} (less than requested)",
            )
        log.warning("reclaim failed for %s: %s", app.name, e)
        return ActionResult(False, f"Failed to reclaim from {app.name}: {e}")
    return ActionResult(
        True,
        f"Asked kernel to reclaim ~{format_bytes(amount)} from {app.name}",
    )


def _memory_current(app: AppMemory) -> int:
    fresh = read_app(app.cgroup_path)
    return fresh.memory_current if fresh else 0


def close_app(app: AppMemory) -> ActionResult:
    unit = app.unit
    if not is_app_unit(unit):
        return ActionResult(False, f"Refusing to close non-app unit {unit}")
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            return ActionResult(True, f"Stopped {app.name} ({unit})")
        log.info("systemctl stop %s: %s", unit, proc.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("systemctl stop failed: %s", e)

    # Fallback: SIGTERM main pid (first in cgroup.procs)
    current = read_app(app.cgroup_path)
    if current is None:
        return ActionResult(True, f"Stopped {app.name}")
    if not current.pids:
        return ActionResult(False, f"No PIDs to signal for {app.name}")
    pid = current.pids[0]
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return ActionResult(False, f"Failed to SIGTERM {app.name} pid={pid}: {e}")
    return ActionResult(True, f"Sent SIGTERM to {app.name} (pid {pid})")
