"""Per-app knowledge from Gemini: static facts plus a usage-based priority score."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from aimemory.apps import format_bytes
from aimemory.config import Config, gemini_api_key

if TYPE_CHECKING:
    from aimemory.apps import AppMemory
    from aimemory.focus import FocusTracker
    from aimemory.store import Store

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_BATCH = 20
FACTS_TTL_S = 24 * 3600
DEFAULT_PRIORITY = 5

KINDS = ["browser", "chat", "editor", "office", "media", "terminal", "files", "game", "other"]


@dataclass(frozen=True)
class AppFacts:
    display_name: str
    kind: str
    background_use: bool
    unsaved_work_risk: bool
    priority: int = DEFAULT_PRIORITY  # 1–10: higher = freer to reclaim/close


@dataclass(frozen=True)
class AppUsage:
    """Local usage snapshot sent with a Gemini lookup."""

    name: str
    memory_bytes: int
    idle_hours: float | None
    accepts: int
    dismisses: int
    rollup: dict[str, float | int | None]


_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "display_name": {"type": "STRING"},
            "kind": {"type": "STRING", "enum": KINDS},
            "background_use": {"type": "BOOLEAN"},
            "unsaved_work_risk": {"type": "BOOLEAN"},
            "priority": {"type": "INTEGER"},
        },
        "required": [
            "id",
            "display_name",
            "kind",
            "background_use",
            "unsaved_work_risk",
            "priority",
        ],
    },
}


def build_prompt(usages: list[AppUsage]) -> str:
    payload = []
    for u in usages:
        r = u.rollup
        peak = r.get("memory_peak_bytes")
        avg = r.get("memory_avg_bytes")
        swap = r.get("swap_bytes")
        payload.append(
            {
                "id": u.name,
                "memory_now": format_bytes(u.memory_bytes),
                "memory_peak_24h": format_bytes(int(peak)) if peak else None,
                "memory_avg_24h": format_bytes(int(avg)) if avg else None,
                "swap_now": format_bytes(int(swap)) if swap else None,
                "idle_hours_now": (
                    None if u.idle_hours is None else round(u.idle_hours, 2)
                ),
                "focus_minutes_24h": r.get("focus_minutes"),
                "audio_minutes_24h": r.get("audio_minutes"),
                "unfocused_minutes_24h": r.get("unfocused_minutes"),
                "cpu_busy_ratio_while_unfocused": r.get("cpu_busy_ratio_unfocused"),
                "process_age_hours": r.get("process_age_hours"),
                "stats_window_hours": r.get("window_hours"),
                "suggestions_last_7d": r.get("suggestions_7d"),
                "accepted_last_7d": r.get("accepts_7d"),
                "dismissed_last_7d": r.get("dismisses_7d"),
                "lifetime_accepted": u.accepts,
                "lifetime_dismissed": u.dismisses,
            }
        )
    return (
        "You help a Linux memory advisor rank idle apps to free RAM.\n"
        "Each id is a desktop app (systemd unit name; may have prefixes like "
        "'gnome-' or reverse-DNS). The fields are MEASURED on THIS machine "
        "(rolling ~24h window when stats_window_hours > 0).\n"
        "Field guide:\n"
        "- focus_minutes_24h: time the window was focused\n"
        "- audio_minutes_24h: time it was actively playing audio\n"
        "- cpu_busy_ratio_while_unfocused: 0–1 CPU activity while not focused "
        "(high means real background work)\n"
        "- idle_hours_now: hours since last focus (null if unknown)\n"
        "- dismissed_last_7d: user ignored suggestions for this app\n"
        "For each id return:\n"
        "- display_name: the product's usual name\n"
        "- kind: closest category\n"
        "- background_use: true if it commonly does useful work unfocused\n"
        "- unsaved_work_risk: true if quitting can lose unsaved work\n"
        "- priority: integer 1–10 for how good a free-memory candidate it is "
        "using BOTH typical app knowledge AND these measurements. "
        "10 = safe/high-value to reclaim or close; "
        "1 = leave alone.\n"
        "Raise priority when: long idle, large memory, little focus time, "
        "low cpu_busy_ratio while unfocused, no recent audio.\n"
        "Lower priority when: recent focus, playing audio, high unfocused CPU, "
        "chat/calls/media patterns, unsaved-work risk, frequent dismissals.\n"
        "If stats_window_hours is low, weigh typical app knowledge more.\n"
        "Return one object per id with the id copied exactly.\n"
        f"apps: {json.dumps(payload)}"
    )


def clamp_priority(value: object) -> int:
    try:
        return max(1, min(10, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


class GeminiClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.key = gemini_api_key()

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.key or ""}

    def check(self) -> str:
        """Verify key and model (free metadata call); returns the model's display name."""
        r = httpx.get(
            f"{API_BASE}/models/{self.config.gemini_model}",
            headers=self._headers(),
            timeout=self.config.gemini_timeout_s,
        )
        r.raise_for_status()
        return str(r.json().get("displayName") or self.config.gemini_model)

    def classify(self, usages: list[AppUsage]) -> dict[str, AppFacts]:
        """Look up facts+priority for up to MAX_BATCH apps; missing ids are omitted."""
        usages = usages[:MAX_BATCH]
        r = httpx.post(
            f"{API_BASE}/models/{self.config.gemini_model}:generateContent",
            headers=self._headers(),
            json={
                "contents": [{"role": "user", "parts": [{"text": build_prompt(usages)}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 2048,
                    "responseMimeType": "application/json",
                    "responseSchema": _SCHEMA,
                },
            },
            timeout=self.config.gemini_timeout_s,
        )
        r.raise_for_status()
        data = r.json()
        parts = data["candidates"][0]["content"]["parts"]
        items = json.loads("".join(p.get("text", "") for p in parts))
        usage = data.get("usageMetadata", {})
        log.info(
            "gemini classified %d apps (%s in / %s out tokens)",
            len(items) if isinstance(items, list) else 0,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
        )
        wanted = {u.name for u in usages}
        out: dict[str, AppFacts] = {}
        for item in items if isinstance(items, list) else []:
            app_id = item.get("id") if isinstance(item, dict) else None
            if app_id not in wanted:
                continue
            kind = item.get("kind")
            out[app_id] = AppFacts(
                display_name=str(item.get("display_name") or app_id)[:60],
                kind=kind if kind in KINDS else "other",
                background_use=bool(item.get("background_use")),
                unsaved_work_risk=bool(item.get("unsaved_work_risk")),
                priority=clamp_priority(item.get("priority")),
            )
        return out


def usage_for(
    apps: list[AppMemory],
    store: Store,
    focus: FocusTracker | None = None,
) -> list[AppUsage]:
    out: list[AppUsage] = []
    for app in apps:
        idle_h: float | None = None
        if focus is not None:
            idle_s = focus.idle_seconds_for(app.name, app.pids)
            if idle_s is not None:
                idle_h = idle_s / 3600.0
            elif focus.tracked_seconds > 0:
                idle_h = focus.tracked_seconds / 3600.0
        accepts, dismisses = store.feedback_counts(app.name)
        out.append(
            AppUsage(
                name=app.name,
                memory_bytes=app.memory_total,
                idle_hours=idle_h,
                accepts=accepts,
                dismisses=dismisses,
                rollup=store.usage_rollup(app.name),
            )
        )
    return out


def refresh_facts(
    client: GeminiClient,
    store: Store,
    apps: list[AppMemory],
    *,
    focus: FocusTracker | None = None,
    ttl_s: float = FACTS_TTL_S,
) -> tuple[int, int]:
    """Classify apps that are missing or older than ttl_s; returns (requested, added)."""
    if not client.configured:
        return 0, 0
    stale_names = set(store.stale_fact_apps([a.name for a in apps], ttl_s=ttl_s))
    stale_apps = [a for a in apps if a.name in stale_names]
    stale_apps.sort(key=lambda a: a.memory_total, reverse=True)
    batch = stale_apps[:MAX_BATCH]
    if not batch:
        return 0, 0
    facts = client.classify(usage_for(batch, store, focus))
    store.put_facts(facts)
    return len(batch), len(facts)
