from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DBUS_DEST = "org.gnome.Shell"
DBUS_PATH = "/org/gnome/shell/extensions/FocusedWindow"
DBUS_METHOD = "org.gnome.shell.extensions.FocusedWindow.Get"


@dataclass
class FocusSnapshot:
    title: str | None
    wm_class: str | None
    pid: int | None
    available: bool
    raw: dict = field(default_factory=dict)


@dataclass
class FocusTracker:
    """Tracks last-focus times keyed by wm_class and pid."""

    last_focus: dict[str, float] = field(default_factory=dict)
    last_pid_focus: dict[int, float] = field(default_factory=dict)
    current: FocusSnapshot | None = None
    extension_ok: bool | None = None
    started_at: float = field(default_factory=time.time)
    _warned: bool = False

    @property
    def tracked_seconds(self) -> float:
        """How long focus has been observed; 0 if the extension never answered."""
        if not self.extension_ok:
            return 0.0
        return max(0.0, time.time() - self.started_at)

    def poll(self) -> FocusSnapshot:
        snap = query_focused_window()
        self.current = snap
        if snap.available:
            now = time.time()
            if not self.extension_ok:
                self.started_at = now
            self.extension_ok = True
            if snap.wm_class:
                self.last_focus[snap.wm_class.lower()] = now
            if snap.pid:
                self.last_pid_focus[snap.pid] = now
        else:
            if self.extension_ok is None:
                self.extension_ok = False
            if not self._warned:
                log.warning(
                    "Focused Window D-Bus extension unavailable; idle scoring degraded"
                )
                self._warned = True
        return snap

    def idle_seconds_for(
        self,
        name: str,
        pids: list[int],
        wm_hints: list[str] | None = None,
    ) -> float | None:
        """Return seconds since last focus, or None if unknown."""
        now = time.time()
        candidates: list[float] = []
        keys = [name.lower()]
        if wm_hints:
            keys.extend(h.lower() for h in wm_hints)
        for key in keys:
            for stored, ts in self.last_focus.items():
                if key in stored or stored in key:
                    candidates.append(ts)
        for pid in pids:
            if pid in self.last_pid_focus:
                candidates.append(self.last_pid_focus[pid])
        if not candidates:
            return None
        return max(0.0, now - max(candidates))

    def is_focused_app(self, name: str, pids: list[int]) -> bool:
        cur = self.current
        if not cur or not cur.available:
            return False
        if cur.pid and cur.pid in pids:
            return True
        if cur.wm_class and name.lower() in cur.wm_class.lower():
            return True
        if cur.wm_class and cur.wm_class.lower() in name.lower():
            return True
        return False


def query_focused_window() -> FocusSnapshot:
    if not shutil.which("gdbus"):
        return FocusSnapshot(None, None, None, False)
    try:
        proc = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                DBUS_DEST,
                "--object-path",
                DBUS_PATH,
                "--method",
                DBUS_METHOD,
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("gdbus focus query failed: %s", e)
        return FocusSnapshot(None, None, None, False)

    if proc.returncode != 0:
        return FocusSnapshot(None, None, None, False)

    inner = _unwrap_gvariant_string(proc.stdout.strip())
    if inner is None:
        return FocusSnapshot(None, None, None, False)

    try:
        data = json.loads(inner)
    except json.JSONDecodeError:
        return FocusSnapshot(None, None, None, False)
    if not isinstance(data, dict):
        return FocusSnapshot(None, None, None, False)

    pid = data.get("pid")
    try:
        pid_i = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_i = None

    return FocusSnapshot(
        title=data.get("title"),
        wm_class=data.get("wm_class") or data.get("wm_class_instance"),
        pid=pid_i,
        available=True,
        raw=data,
    )


def _unwrap_gvariant_string(raw_text: str) -> str | None:
    """Extract the string from gdbus output like ('{...}',)."""
    # GVariant text escapes are a subset of Python literal escapes, and
    # non-ASCII text is printed verbatim.
    try:
        value = ast.literal_eval(raw_text)
    except (ValueError, SyntaxError):
        value = None
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start < 0 or end < 0:
        return None
    return raw_text[start : end + 1]


def extension_available() -> bool:
    return query_focused_window().available
