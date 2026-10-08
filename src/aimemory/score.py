from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from aimemory.apps import AppMemory, format_bytes
from aimemory.audio import is_playing
from aimemory.config import Config
from aimemory.facts import AppFacts
from aimemory.focus import FocusTracker
from aimemory.store import Store

UNKNOWN_IDLE_S = 30 * 60
LEAK_GROWTH_RATIO = 1.05
LEAK_MIN_IDLE_S = 600
BACKGROUND_USE_FACTOR = 0.5
# Gemini priority is 1–10; 5 is neutral. Scale ranking by priority/5.


class RecommendedAction(str, Enum):
    CLOSE = "close"
    RECLAIM = "reclaim"


@dataclass
class Candidate:
    app: AppMemory
    idle_seconds: float
    idle_hours: float
    leak_factor: float
    feedback_factor: float
    score: float
    recommended_action: RecommendedAction
    growth_bytes: int = 0
    facts: AppFacts | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.app.name

    @property
    def display_name(self) -> str:
        return self.facts.display_name if self.facts else self.app.name


def score_apps(
    apps: list[AppMemory],
    focus: FocusTracker,
    store: Store,
    config: Config,
    *,
    prev_memory: dict[str, int] | None = None,
    playing: set[Path] | None = None,
    swap_free: int | None = None,
) -> list[Candidate]:
    """Rank apps.

    prev_memory maps cgroup path -> memory.current from ~10 min ago; playing is the
    set of cgroups currently outputting audio; swap_free enables the reclaim check.
    """
    prev_memory = prev_memory or {}
    playing = playing or set()
    out: list[Candidate] = []
    for app in apps:
        if focus.is_focused_app(app.name, app.pids):
            continue
        if is_playing(app.cgroup_path, playing):
            continue
        facts = store.get_facts(app.name)
        notes: list[str] = []

        idle_s = focus.idle_seconds_for(app.name, app.pids)
        if idle_s is None:
            # Never focused while we were watching: idle at least that long.
            idle_s = max(UNKNOWN_IDLE_S, focus.tracked_seconds)
        idle_hours = max(idle_s / 3600.0, 1.0 / 60.0)

        leak_factor = 1.0
        growth = 0
        prev = prev_memory.get(str(app.cgroup_path))
        if prev is not None:
            growth = app.memory_current - prev
            if app.memory_current > prev * LEAK_GROWTH_RATIO and idle_s > LEAK_MIN_IDLE_S:
                leak_factor = 1.5
                notes.append(f"Grew {format_bytes(growth)} in the last 10 min while unused.")

        fb = store.feedback_factor(app.name)
        priority = facts.priority if facts else 5
        score = app.memory_total * idle_hours * leak_factor * fb * (priority / 5.0)
        if facts and facts.background_use:
            score *= BACKGROUND_USE_FACTOR

        action = (
            RecommendedAction.CLOSE
            if idle_hours >= config.close_idle_hours
            else RecommendedAction.RECLAIM
        )
        # Reclaimed anonymous memory has to go to swap; without room it barely helps.
        swap_full = swap_free is not None and swap_free < app.memory_current // 2
        if swap_full and action == RecommendedAction.RECLAIM:
            action = RecommendedAction.CLOSE
            notes.append("Swap is nearly full, so reclaiming would free little.")
        if facts and facts.background_use and action == RecommendedAction.CLOSE:
            notes.append("It may be doing background work (sync, downloads, calls).")
        if facts and facts.unsaved_work_risk:
            notes.append("Closing it may lose unsaved work.")

        out.append(
            Candidate(
                app=app,
                idle_seconds=idle_s,
                idle_hours=idle_hours,
                leak_factor=leak_factor,
                feedback_factor=fb,
                score=score,
                recommended_action=action,
                growth_bytes=growth,
                facts=facts,
                notes=notes,
            )
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out
