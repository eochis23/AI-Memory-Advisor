from __future__ import annotations

import argparse
import dataclasses
import getpass
import logging
import shutil
import sys
import time
from pathlib import Path

from aimemory import __version__
from aimemory.apps import format_bytes, list_apps
from aimemory.audio import playing_cgroups
from aimemory.config import (
    DEFAULT_CONFIG_PATH,
    GEMINI_KEY_PATH,
    Config,
    ensure_user_config,
    save_gemini_api_key,
)
from aimemory.facts import GeminiClient, refresh_facts
from aimemory.focus import FocusTracker, extension_available
from aimemory.notify import show_recommendation, show_result, summary_line
from aimemory.pressure import PressureMonitor, PressureSample, Zone
from aimemory.score import Candidate, score_apps
from aimemory.store import Store


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_doctor(config: Config, config_path: Path) -> int:
    ok = True
    print(f"AIMEMORY doctor v{__version__}")
    state = "exists" if config_path.is_file() else "missing — defaults"
    print(f"config: {config_path} ({state})")

    if Path("/proc/pressure/memory").is_file():
        print("[ok] memory PSI available")
    else:
        print("[fail] /proc/pressure/memory missing")
        ok = False

    if Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        print("[ok] cgroup v2")
    else:
        print("[fail] cgroup v2 not detected")
        ok = False

    apps = list_apps(config)
    top = apps[0].name if apps else "none"
    print(f"[ok] app inventory: {len(apps)} units (top: {top})")

    if shutil.which("notify-send"):
        print("[ok] notify-send")
    else:
        print("[fail] notify-send missing")
        ok = False

    if shutil.which("pactl"):
        print("[ok] pactl (apps playing audio are never suggested)")
    else:
        print("[warn] pactl missing — audio playback is not detected")

    if extension_available():
        print("[ok] Focused Window D-Bus extension")
    else:
        print(
            "[warn] Focused Window D-Bus extension unavailable "
            "(https://extensions.gnome.org/extension/5592/focused-window-d-bus/)"
        )

    client = GeminiClient(config)
    if not client.configured:
        print("[warn] no Gemini API key — run `aimemory set-key` (app facts disabled)")
    else:
        try:
            name = client.check()
            print(f"[ok] Gemini key works ({name})")
        except Exception as e:
            print(f"[fail] Gemini check failed for {config.gemini_model}: {e}")
            ok = False

    sample = PressureMonitor(config).sample()
    psi = sample.psi_some.avg10 if sample.psi_some else None
    print(
        f"[info] zone={sample.zone.value} "
        f"MemAvailable={format_bytes(sample.mem_available)} psi_some.avg10={psi}"
    )
    return 0 if ok else 1


def _rank(config: Config, store: Store, sample: PressureSample) -> list[Candidate]:
    focus = FocusTracker()
    snap = focus.poll()
    if snap.available:
        print(f"focus: {snap.wm_class} pid={snap.pid} ({snap.title})")
    else:
        print("focus: (extension unavailable)")
    apps = list_apps(config)
    client = GeminiClient(config)
    if client.configured:
        try:
            requested, added = refresh_facts(client, store, apps, focus=focus)
            if requested:
                print(f"gemini: looked up {added}/{requested} apps (daily priority refresh)")
        except Exception as e:
            print(f"gemini lookup failed: {e}", file=sys.stderr)
    playing = playing_cgroups()
    if playing:
        print("playing audio (skipped): " + ", ".join(sorted(p.name for p in playing)))
    return score_apps(
        apps, focus, store, config, playing=playing, swap_free=sample.swap_free
    )


def cmd_status(config: Config) -> int:
    monitor = PressureMonitor(config)
    monitor.sample()
    time.sleep(1.0)  # second sample gives a real swap-in rate
    sample = monitor.sample()
    print(f"zone: {sample.zone.value}")
    print(
        f"MemAvailable: {format_bytes(sample.mem_available)} / "
        f"{format_bytes(sample.mem_total)}"
    )
    print(f"swap free: {format_bytes(sample.swap_free)} / {format_bytes(sample.swap_total)}")
    if sample.psi_some:
        print(f"PSI some: avg10={sample.psi_some.avg10} avg60={sample.psi_some.avg60}")
    print(f"swap-in rate: {sample.swap_in_rate:.1f} pages/s")

    store = Store(config.db_path)
    cands = _rank(config, store, sample)
    store.close()
    print("top candidates:")
    for c in cands[:8]:
        tags = []
        if c.facts:
            tags.append(c.facts.kind)
            tags.append(f"p{c.facts.priority}")
            if c.facts.background_use:
                tags.append("background")
            if c.facts.unsaved_work_risk:
                tags.append("unsaved-risk")
        print(
            f"  {c.display_name:28} {format_bytes(c.app.memory_total):>10}  "
            f"idle={c.idle_hours:.2f}h  -> {c.recommended_action.value:7}  "
            f"[{', '.join(tags) or 'no facts'}]"
        )
    return 0


def cmd_demo(config: Config) -> int:
    """Show a real notification for the top candidate without acting on it."""
    sample = dataclasses.replace(PressureMonitor(config).sample(), zone=Zone.YELLOW)
    store = Store(config.db_path)
    cands = _rank(config, store, sample)
    store.close()
    if not cands:
        print("no candidate apps found", file=sys.stderr)
        return 1
    top = cands[0]
    print("notification text:")
    for line in [summary_line(top), *top.notes]:
        print(f"  {line}")

    pending = show_recommendation(top, config)
    if pending is None:
        return 1
    print(
        f"notification shown — click it, press its button, or dismiss it "
        f"(auto-withdrawn after {config.notify_expire_ms // 1000}s)"
    )
    try:
        while (outcome := pending.poll()) is None:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pending.cancel()
        return 130

    if outcome.action is None:
        message = f"Demo: ignored — {top.display_name} left alone"
    else:
        message = f"Demo (dry run): would {outcome.action.value} {top.display_name}"
    print(f"you chose: {outcome.choice.value} -> {message}")
    show_result(message)
    return 0


def cmd_set_key() -> int:
    key = getpass.getpass("Gemini API key (input hidden): ").strip()
    if not key:
        print("no key entered", file=sys.stderr)
        return 1
    path = save_gemini_api_key(key)
    print(f"saved to {path} (mode 600)")
    return 0


def cmd_usage(config: Config) -> int:
    """Show the rolling usage snapshots that feed Gemini."""
    store = Store(config.db_path)
    # Take two quick samples so the CLI alone can seed/update stats.
    from aimemory.sampler import UsageRecorder

    focus = FocusTracker()
    focus.poll()
    apps = list_apps(config)
    rec = UsageRecorder(store)
    playing = playing_cgroups()
    rec.record(apps, focus, playing)
    time.sleep(1.0)
    focus.poll()
    apps = list_apps(config)
    rec.record(apps, focus, playing, now=time.time())

    names = store.all_usage_apps() or [a.name for a in apps]
    if not names:
        print("no usage samples yet — keep the daemon running")
        store.close()
        return 0
    print(f"{'app':40} {'focus':>7} {'audio':>7} {'unfoc':>7} {'cpu%':>6} {'peak':>10} {'age_h':>6}")
    for name in names:
        r = store.usage_rollup(name)
        cpu = r.get("cpu_busy_ratio_unfocused")
        cpu_s = f"{100 * cpu:.0f}%" if isinstance(cpu, float) else "-"
        peak = r.get("memory_peak_bytes")
        age = r.get("process_age_hours")
        print(
            f"{name:40} {r.get('focus_minutes', 0):>6.1f}m "
            f"{r.get('audio_minutes', 0):>6.1f}m "
            f"{r.get('unfocused_minutes', 0):>6.1f}m "
            f"{cpu_s:>6} "
            f"{format_bytes(int(peak)) if peak else '-':>10} "
            f"{age if age is not None else '-':>6}"
        )
    store.close()
    return 0


def cmd_facts(config: Config, *, clear: bool) -> int:
    store = Store(config.db_path)
    if clear:
        store.clear_facts()
        print("cleared cached app facts; they will be looked up again")
        store.close()
        return 0
    facts = store.all_facts()
    if not facts:
        print("no cached app facts")
    for app, f in facts.items():
        age = store.fact_age_s(app)
        age_h = f"{age / 3600:.1f}h" if age is not None else "?"
        print(
            f"  {app:40} {f.display_name:24} {f.kind:9} "
            f"priority={f.priority:2} background={f.background_use!s:5} "
            f"unsaved_risk={f.unsaved_work_risk} age={age_h}"
        )
    store.close()
    return 0


def cmd_run(config: Config) -> int:
    from aimemory.daemon import Daemon

    Daemon(config).run()
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aimemory", description="AI Memory Manager")
    parser.add_argument("--config", type=Path, default=None, help="config.toml path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check prerequisites")
    sub.add_parser("status", help="show pressure and top candidates")
    sub.add_parser("run", help="run the daemon")
    sub.add_parser("demo", help="show a real notification without acting (dry run)")
    sub.add_parser("init-config", help="write default config.toml")
    sub.add_parser("set-key", help=f"store the Gemini API key in {GEMINI_KEY_PATH}")
    sub.add_parser("usage", help="show rolling local usage snapshots (Gemini input)")
    facts_p = sub.add_parser("facts", help="show cached per-app facts from Gemini")
    facts_p.add_argument("--clear", action="store_true", help="forget all cached facts")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "init-config":
        path = ensure_user_config()
        print(f"wrote {path}")
        sys.exit(0)
    if args.cmd == "set-key":
        sys.exit(cmd_set_key())

    try:
        config = Config.load(args.config)
    except (OSError, ValueError) as e:
        parser.error(str(e))
    if args.cmd == "doctor":
        sys.exit(cmd_doctor(config, args.config or DEFAULT_CONFIG_PATH))
    if args.cmd == "status":
        sys.exit(cmd_status(config))
    if args.cmd == "run":
        sys.exit(cmd_run(config))
    if args.cmd == "demo":
        sys.exit(cmd_demo(config))
    if args.cmd == "usage":
        sys.exit(cmd_usage(config))
    if args.cmd == "facts":
        sys.exit(cmd_facts(config, clear=args.clear))
    parser.error(f"unknown command {args.cmd}")


if __name__ == "__main__":
    main()
