from __future__ import annotations

import ctypes
import logging
import os
import signal
import time
from collections import deque
from typing import Any

from aimemory.actions import perform
from aimemory.apps import list_apps
from aimemory.audio import playing_cgroups
from aimemory.config import Config
from aimemory.facts import FACTS_TTL_S, GeminiClient, refresh_facts
from aimemory.focus import FocusTracker
from aimemory.notify import (
    NotifyChoice,
    PendingNotification,
    show_recommendation,
    show_result,
    summary_line,
)
from aimemory.pressure import PressureMonitor, Zone
from aimemory.sampler import UsageRecorder
from aimemory.score import Candidate, score_apps
from aimemory.store import Store

log = logging.getLogger(__name__)

MCL_CURRENT = 1
MCL_ONFAULT = 4

LEAK_WINDOW_S = 600.0
MEMORY_SNAPSHOT_STEP_S = 60.0
FACTS_CHECK_INTERVAL_S = 60.0
FACTS_RETRY_AFTER_FAILURE_S = 15 * 60.0


def try_mlockall() -> None:
    # No MCL_FUTURE: with a finite RLIMIT_MEMLOCK it makes later allocations
    # fail outright, which would crash the daemon exactly under pressure.
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.mlockall(MCL_CURRENT | MCL_ONFAULT)
        if rc != 0:
            err = ctypes.get_errno()
            log.info("mlockall unavailable (non-fatal): %s", os.strerror(err))
        else:
            log.info("mlockall enabled")
    except (OSError, AttributeError):
        log.info("mlockall not supported")


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pressure = PressureMonitor(config)
        self.focus = FocusTracker()
        self.store = Store(config.db_path)
        self.gemini = GeminiClient(config)
        self.usage = UsageRecorder(self.store)
        self._stop = False
        self._last_focus_poll = 0.0
        self._next_facts_check = 0.0
        self._cooldown_until: dict[str, float] = {}
        self._memory_history: deque[tuple[float, dict[str, int]]] = deque()
        self._last_zone = Zone.GREEN
        self._quiet_until = 0.0
        self._pending: PendingNotification | None = None
        self._pending_alert: tuple[Candidate, Zone] | None = None

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    def run(self) -> None:
        try_mlockall()
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        if not self.gemini.configured:
            log.warning("no Gemini API key (aimemory set-key); app facts disabled")

        log.info("aimemory daemon started")
        while not self._stop:
            try:
                self._tick()
            except Exception:
                log.exception("tick failed")
            time.sleep(self.config.poll_interval_s)
        if self._pending is not None:
            self._pending.cancel()
        self.store.close()
        log.info("aimemory daemon stopped")

    def _memory_baseline(self, now: float, mem_map: dict[str, int]) -> dict[str, int]:
        """Return the newest memory snapshot that is at least LEAK_WINDOW_S old."""
        history = self._memory_history
        if not history or now - history[-1][0] >= MEMORY_SNAPSHOT_STEP_S:
            history.append((now, mem_map))
        while len(history) > 1 and now - history[1][0] >= LEAK_WINDOW_S:
            history.popleft()
        ts, snapshot = history[0]
        return snapshot if now - ts >= LEAK_WINDOW_S else {}

    def _tick(self) -> None:
        now = time.time()
        if now - self._last_focus_poll >= self.config.focus_poll_interval_s:
            self.focus.poll()
            self._last_focus_poll = now

        if self._pending is not None:
            self._check_pending()

        sample = self.pressure.sample()
        apps = list_apps(self.config)
        playing = playing_cgroups()
        # Always sample — Gemini's daily priority needs a real usage history.
        self.usage.record(apps, self.focus, playing, now=now)
        baseline = self._memory_baseline(
            now, {str(a.cgroup_path): a.memory_current for a in apps}
        )

        self._maybe_refresh_facts(now, apps)

        if sample.zone == Zone.GREEN:
            self._last_zone = sample.zone
            return

        if self._pending is not None:
            return

        worsened = sample.zone == Zone.RED and self._last_zone != Zone.RED
        self._last_zone = sample.zone
        if now < self._quiet_until and not worsened:
            return
        self._cooldown_until = {k: v for k, v in self._cooldown_until.items() if v > now}

        candidates = score_apps(
            apps,
            self.focus,
            self.store,
            self.config,
            prev_memory=baseline,
            playing=playing,
            swap_free=sample.swap_free,
        )
        if not worsened:
            candidates = [c for c in candidates if c.name not in self._cooldown_until]
        if not candidates:
            return

        top = candidates[0]
        log.info("alert zone=%s app=%s: %s", sample.zone.value, top.name, summary_line(top))
        self._cooldown_until[top.name] = now + self.config.cooldown_minutes * 60
        self._quiet_until = now + self.config.notify_gap_minutes * 60
        pending = show_recommendation(top, self.config)
        if pending is not None:
            self._pending = pending
            self._pending_alert = (top, sample.zone)

    def _maybe_refresh_facts(self, now: float, apps: list) -> None:
        if not self.gemini.configured or now < self._next_facts_check:
            return
        self._next_facts_check = now + FACTS_CHECK_INTERVAL_S
        try:
            requested, added = refresh_facts(
                self.gemini, self.store, apps, focus=self.focus, ttl_s=FACTS_TTL_S
            )
            if added:
                log.info("refreshed gemini facts/priority for %d/%d apps", added, requested)
        except Exception as e:
            log.warning("gemini lookup failed: %s", e)
            self._next_facts_check = now + FACTS_RETRY_AFTER_FAILURE_S
            return
        if requested and added < requested:
            self._next_facts_check = now + FACTS_RETRY_AFTER_FAILURE_S

    def _check_pending(self) -> None:
        assert self._pending is not None and self._pending_alert is not None
        outcome = self._pending.poll()
        if outcome is None:
            return
        top, zone = self._pending_alert
        self._pending = None
        self._pending_alert = None
        self._quiet_until = time.time() + self.config.notify_gap_minutes * 60

        if outcome.choice == NotifyChoice.IGNORE or outcome.action is None:
            self.store.record_dismiss(top.name)
            self.store.log_event(
                app=top.name,
                unit=top.app.unit,
                action="ignore",
                recommended=top.recommended_action.value,
                zone=zone.value,
                memory_bytes=top.app.memory_total,
                idle_hours=top.idle_hours,
            )
            return

        result = perform(outcome.action, top.app)
        self.store.record_accept(top.name)
        self.store.log_event(
            app=top.name,
            unit=top.app.unit,
            action=outcome.action.value,
            recommended=top.recommended_action.value,
            zone=zone.value,
            memory_bytes=top.app.memory_total,
            idle_hours=top.idle_hours,
            detail=result.message,
        )
        show_result(result.message, success=result.ok)
