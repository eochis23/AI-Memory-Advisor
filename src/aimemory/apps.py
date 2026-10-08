from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from aimemory.config import Config

# Only desktop-launched app units are candidates. Anything else under the user
# slice (session-N.scope, init.scope, user@.service internals) is the session
# itself; stopping it logs the user out or kills the user manager.
_APP_UNIT_PREFIXES = ("app-", "flatpak-", "snap.")
_APP_UNIT_SUFFIXES = (".scope", ".service")


def is_app_unit(unit: str) -> bool:
    return unit.startswith(_APP_UNIT_PREFIXES) and unit.endswith(_APP_UNIT_SUFFIXES)


@dataclass
class AppMemory:
    unit: str
    name: str
    cgroup_path: Path
    memory_current: int
    memory_swap: int
    pids: list[int]

    @property
    def memory_total(self) -> int:
        return self.memory_current + self.memory_swap


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _read_pids(path: Path) -> list[int]:
    pids_file = path / "cgroup.procs"
    if not pids_file.is_file():
        return []
    out: list[int] = []
    try:
        for line in pids_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
    except OSError:
        return []
    return out


def _pretty_name(unit: str) -> str:
    name = unit
    for suffix in (".scope", ".service"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name.startswith("app-"):
        name = name[4:]
    if name.startswith("flatpak-"):
        name = name[8:]
    # strip systemd escaped segments like firefox-12345
    name = name.replace("\\x2d", "-")
    # drop trailing -UID or random suffixes that look numeric
    name = re.sub(r"-[0-9a-f]{6,}$", "", name, flags=re.I)
    name = re.sub(r"-\d+$", "", name)
    return name or unit


def _user_slice_roots(uid: int | None = None) -> list[Path]:
    uid = os.getuid() if uid is None else uid
    base = Path("/sys/fs/cgroup/user.slice") / f"user-{uid}.slice"
    if not base.is_dir():
        # fallback: current process cgroup
        try:
            cg = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
            # 0::/user.slice/user-1000.slice/...
            if "::" in cg:
                rel = cg.split("::", 1)[1].lstrip("/")
                parts = Path(rel).parts
                for i, part in enumerate(parts):
                    if part.startswith("user-") and part.endswith(".slice"):
                        return [Path("/sys/fs/cgroup").joinpath(*parts[: i + 1])]
        except OSError:
            pass
        return []
    return [base]


def _iter_app_cgroups(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, _ in os.walk(root):
        p = Path(dirpath)
        if is_app_unit(p.name):
            found.append(p)
            dirnames[:] = []
    return found


def _denied(name: str, unit: str, denylist: list[str]) -> bool:
    blob = f"{name} {unit}".lower()
    for item in denylist:
        if item.lower() in blob:
            return True
    return False


def list_apps(config: Config) -> list[AppMemory]:
    apps: list[AppMemory] = []
    seen: set[str] = set()
    for root in _user_slice_roots():
        for cg in _iter_app_cgroups(root):
            unit = cg.name
            name = _pretty_name(unit)
            if _denied(name, unit, config.denylist):
                continue
            key = str(cg)
            if key in seen:
                continue
            seen.add(key)
            app = read_app(cg)
            if app is not None:
                apps.append(app)
    apps.sort(key=lambda a: a.memory_total, reverse=True)
    return apps


def read_app(cg: Path) -> AppMemory | None:
    """Read current state of an app cgroup; None if it is gone or empty."""
    if not cg.is_dir():
        return None
    mem = _read_int(cg / "memory.current")
    swap = _read_int(cg / "memory.swap.current")
    if mem <= 0 and swap <= 0:
        return None
    return AppMemory(
        unit=cg.name,
        name=_pretty_name(cg.name),
        cgroup_path=cg,
        memory_current=mem,
        memory_swap=swap,
        pids=_read_pids(cg),
    )


def format_bytes(n: int) -> str:
    for unit, suffix in ((1024**3, "GB"), (1024**2, "MB"), (1024, "KB")):
        if n >= unit:
            return f"{n / unit:.2f} {suffix}"
    return f"{n} B"
