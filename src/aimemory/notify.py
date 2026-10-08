from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum

from aimemory.apps import format_bytes
from aimemory.config import Config
from aimemory.score import Candidate, RecommendedAction

log = logging.getLogger(__name__)


class NotifyChoice(str, Enum):
    RECLAIM = "reclaim"
    CLOSE = "close"
    IGNORE = "ignore"


@dataclass
class NotifyOutcome:
    choice: NotifyChoice
    action: RecommendedAction | None


IGNORED = NotifyOutcome(NotifyChoice.IGNORE, None)

_ACTION_BY_KEY = {
    "reclaim": (NotifyChoice.RECLAIM, RecommendedAction.RECLAIM),
    "close": (NotifyChoice.CLOSE, RecommendedAction.CLOSE),
}


class PendingNotification:
    """A notify-send process waiting for the user; poll() without blocking."""

    def __init__(self, proc: subprocess.Popen[str], deadline: float) -> None:
        self.proc = proc
        self.deadline = deadline

    def poll(self) -> NotifyOutcome | None:
        if self.proc.poll() is None:
            if time.monotonic() < self.deadline:
                return None
            self.cancel()
            return IGNORED
        stdout = self.proc.stdout.read() if self.proc.stdout else ""
        # --print-id writes the id first; the chosen action key (if any) follows.
        lines = stdout.strip().splitlines()
        key = lines[-1].strip() if lines else ""
        if key in _ACTION_BY_KEY:
            choice, action = _ACTION_BY_KEY[key]
            return NotifyOutcome(choice, action)
        return IGNORED

    def cancel(self) -> None:
        """Withdraw the notification (notify-send closes it on SIGINT)."""
        if self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def summary_line(c: Candidate) -> str:
    return (
        f"{c.display_name} uses {format_bytes(c.app.memory_total)}, "
        f"unused for {_idle_text(c.idle_seconds)}. "
        f"Recommended: {c.recommended_action.value}."
    )


def _idle_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{seconds / 3600:.1f} h"


def show_recommendation(candidate: Candidate, config: Config) -> PendingNotification | None:
    if not shutil.which("notify-send"):
        log.error("notify-send not found")
        return None

    # GNOME hides an action named "default" (body click). Use explicit ids so
    # both reclaim and close appear as buttons; recommended action first.
    if candidate.recommended_action == RecommendedAction.RECLAIM:
        actions = [("reclaim", "Reclaim memory"), ("close", "Close app")]
    else:
        actions = [("close", "Close app"), ("reclaim", "Reclaim memory")]
    body = "\n".join([summary_line(candidate), *candidate.notes])
    cmd = [
        "notify-send",
        "--app-name=AIMEMORY",
        "--urgency=normal",
        "--print-id",
        f"--expire-time={config.notify_expire_ms}",
    ]
    for key, label in actions:
        cmd.extend(["-A", f"{key}={label}"])
    cmd.extend(["Free memory?", body])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as e:
        log.error("notify-send failed: %s", e)
        return None
    # GNOME ignores expire-time for normal urgency, so enforce it ourselves.
    deadline = time.monotonic() + config.notify_expire_ms / 1000.0
    return PendingNotification(proc, deadline)


def show_result(message: str, *, success: bool = True) -> None:
    if not shutil.which("notify-send"):
        return
    urgency = "low" if success else "critical"
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=AIMEMORY",
                f"--urgency={urgency}",
                "--expire-time=5000",
                "AIMEMORY",
                message,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notify-send failed: %s", e)
