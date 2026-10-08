from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from aimemory.config import Config


class Zone(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class PsiLine:
    avg10: float
    avg60: float
    avg300: float
    total: int


@dataclass(frozen=True)
class PressureSample:
    mem_total: int
    mem_available: int
    swap_total: int
    swap_free: int
    psi_some: PsiLine | None
    psi_full: PsiLine | None
    pswpin: int
    pswpout: int
    swap_in_rate: float
    zone: Zone
    ts: float

    @property
    def mem_available_pct(self) -> float:
        if self.mem_total <= 0:
            return 100.0
        return 100.0 * self.mem_available / self.mem_total


def _parse_psi_line(line: str) -> PsiLine | None:
    # some avg10=0.00 avg60=0.12 avg300=0.11 total=24506559
    parts = line.split()
    if len(parts) < 5:
        return None
    vals: dict[str, float] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        vals[k] = float(v)
    try:
        return PsiLine(
            avg10=vals["avg10"],
            avg60=vals["avg60"],
            avg300=vals["avg300"],
            total=int(vals["total"]),
        )
    except KeyError:
        return None


def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    out: dict[str, int] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        toks = rest.split()
        if not toks:
            continue
        # values are kB
        out[key] = int(toks[0]) * 1024
    return out


def read_psi(path: Path = Path("/proc/pressure/memory")) -> tuple[PsiLine | None, PsiLine | None]:
    if not path.is_file():
        return None, None
    some = full = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("some "):
            some = _parse_psi_line(line)
        elif line.startswith("full "):
            full = _parse_psi_line(line)
    return some, full


def read_vmstat(path: Path = Path("/proc/vmstat")) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


class PressureMonitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._prev_pswpin: int | None = None
        self._prev_pswpout: int | None = None
        self._prev_ts: float | None = None

    def sample(self) -> PressureSample:
        mem = read_meminfo()
        some, full = read_psi()
        vm = read_vmstat()
        now = time.time()
        pswpin = vm.get("pswpin", 0)
        pswpout = vm.get("pswpout", 0)
        swap_in_rate = 0.0
        if self._prev_pswpin is not None and self._prev_ts is not None:
            dt = max(now - self._prev_ts, 1e-6)
            swap_in_rate = max(0.0, (pswpin - self._prev_pswpin) / dt)
        self._prev_pswpin = pswpin
        self._prev_pswpout = pswpout
        self._prev_ts = now

        mem_total = mem.get("MemTotal", 0)
        mem_available = mem.get("MemAvailable", 0)
        avail_pct = 100.0 * mem_available / mem_total if mem_total else 100.0
        psi10 = some.avg10 if some else 0.0

        zone = Zone.GREEN
        if (
            avail_pct <= self.config.red_mem_available_pct
            or psi10 >= self.config.red_psi_avg10
            or swap_in_rate >= self.config.swap_in_red_pages_per_s
        ):
            zone = Zone.RED
        elif (
            avail_pct <= self.config.yellow_mem_available_pct
            or psi10 >= self.config.yellow_psi_avg10
        ):
            zone = Zone.YELLOW

        return PressureSample(
            mem_total=mem_total,
            mem_available=mem_available,
            swap_total=mem.get("SwapTotal", 0),
            swap_free=mem.get("SwapFree", 0),
            psi_some=some,
            psi_full=full,
            pswpin=pswpin,
            pswpout=pswpout,
            swap_in_rate=swap_in_rate,
            zone=zone,
            ts=now,
        )
