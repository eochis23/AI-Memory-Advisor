"""Which app cgroups are playing audio right now (via PipeWire's pulse server)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")


def _cgroup_of(pid: int) -> Path | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return CGROUP_ROOT / line[3:].lstrip("/")
    return None


def playing_cgroups() -> set[Path]:
    """cgroups of processes with an uncorked (playing) output stream."""
    if not shutil.which("pactl"):
        return set()
    try:
        proc = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        streams = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log.debug("pactl failed: %s", e)
        return set()

    out: set[Path] = set()
    for stream in streams if isinstance(streams, list) else []:
        if stream.get("corked", True):
            continue
        pid = (stream.get("properties") or {}).get("application.process.id")
        if pid is None or not str(pid).isdigit():
            continue
        cg = _cgroup_of(int(pid))
        if cg is not None:
            out.add(cg)
    return out


def is_playing(app_cgroup: Path, playing: set[Path]) -> bool:
    return any(cg == app_cgroup or app_cgroup in cg.parents for cg in playing)
