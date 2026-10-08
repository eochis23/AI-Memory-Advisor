# AIMEMORY

Proactive memory-pressure advisor for **GNOME Wayland**. It watches PSI / `MemAvailable`, ranks idle high-memory apps, and shows a notification with **Reclaim memory** and **Close app** buttons.

Heuristics decide *whether* to alert and *which* app to suggest. The daemon continuously samples local usage; **Gemini Flash-Lite** reads that snapshot about once a day and returns app facts plus a **priority (1–10)** used in ranking. Without an API key everything still works — just without Gemini facts/priorities.

## Prerequisites

1. **Focused Window D-Bus** GNOME extension (strongly recommended for real idle/focus times)  
   https://extensions.gnome.org/extension/5592/focused-window-d-bus/

2. **Gemini API key** (optional) from https://aistudio.google.com/apikey

3. `notify-send` (libnotify), `gdbus`, `pactl` (PipeWire/PulseAudio), cgroup v2, Python 3.12+, [`uv`](https://github.com/astral-sh/uv)

## Install

```bash
cd /home/eochis/AIMEMORY
uv sync
uv tool install -e .
aimemory init-config
aimemory set-key       # ~/.config/aimemory/gemini_api_key (mode 600)
aimemory doctor
```

`GEMINI_API_KEY` in the environment overrides the key file.

After pulling code changes, reinstall and restart so the startup service picks them up:

```bash
uv tool install -e . --force
systemctl --user restart aimemory.service
```

### Start on login (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/aimemory.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aimemory.service
systemctl --user status aimemory.service
journalctl --user -u aimemory.service -f
```

The unit is wanted by `graphical-session.target`, so it starts with your GNOME session and stops when you log out. It runs `~/.local/bin/aimemory run`.

## Commands

| Command | Purpose |
| --- | --- |
| `aimemory doctor` | Check PSI, cgroup, notify-send, pactl, focus extension, Gemini key |
| `aimemory status` | Zone, swap, ranked candidates (refreshes stale Gemini facts) |
| `aimemory usage` | Rolling local usage snapshots that feed Gemini |
| `aimemory facts [--clear]` | Show (or forget) cached Gemini facts/priorities |
| `aimemory demo` | Real notification for the top app; dry run, never acts |
| `aimemory run` | Foreground daemon |
| `aimemory set-key` | Store the Gemini API key |
| `aimemory init-config` | Write `~/.config/aimemory/config.toml` |

## Behavior

| Zone | When |
| --- | --- |
| Green | Plenty of free memory, low PSI — samples usage, no alerts |
| Yellow | MemAvailable ≲ 25% or PSI `avg10 ≥ 1` |
| Red | MemAvailable ≲ 15%, PSI `avg10 ≥ 5`, or heavy swap-in |

**Notification**

- Buttons: **Reclaim memory** and **Close app** (recommended action first)
- Dismiss / timeout → ignore (that app’s ranking weight drops)
- Extra lines when relevant: memory growth, swap nearly full, background work, unsaved-work risk

While a notification is open the daemon keeps sampling; it is withdrawn after `notify_expire_ms`. Each suggested app gets a `cooldown_minutes` cooldown, with at least `notify_gap_minutes` between notifications (unless pressure jumps into red).

**Candidates**

Only desktop-launched units under your user slice: `app-*`, `flatpak-*`, `snap.*` (`.scope` / `.service`). Session units like `session-N.scope` and `init.scope` are never candidates.

Ranking uses roughly:

`memory × idle_hours × leak_factor × feedback × (priority / 5) × background_penalty`

Also:

- Apps currently playing audio are skipped
- Gemini `background_use` halves the score and adds a close warning
- If free swap is less than half the app’s memory, recommend **close** instead of **reclaim**
- Without the focus extension, idle times are approximate

**Actions**

- **Reclaim** — write to the app cgroup’s `memory.reclaim` (anonymous pages need swap/zram)
- **Close** — `systemctl --user stop <unit>`, else `SIGTERM`

Both re-read the cgroup at click time so a stale notification never signals a recycled PID.

## Usage sampling → Gemini priority

Every poll (~2s), even in green, the daemon records into SQLite (`usage_stats`):

- Focus minutes (needs the GNOME extension)
- Audio playback minutes
- Unfocused time and CPU-busy ratio while unfocused
- Memory now / peak / average, swap
- Process age
- Accept / dismiss counts (7-day and lifetime)

About once per day (and immediately for brand-new apps), Gemini Flash-Lite receives that snapshot and returns:

- `display_name`, `kind`
- `background_use`, `unsaved_work_risk`
- `priority` 1–10 (higher = better free-memory candidate)

Priority is cached 24h. Typical cost is one small call per day for your running apps (often free-tier / fractions of a cent).

Inspect with `aimemory usage` and `aimemory facts`.

## Privacy

Sent to Gemini: app ids derived from unit names, plus the measured usage fields above. **Not** sent: window titles, URLs, document contents, or keystrokes.

## Config and data

| Path | Purpose |
| --- | --- |
| `~/.config/aimemory/config.toml` | Thresholds, cooldowns, model id (`aimemory init-config`) |
| `~/.config/aimemory/gemini_api_key` | API key from `aimemory set-key` (mode 600) |
| `~/.local/share/aimemory/history.db` | Events, feedback, `app_facts`, `usage_stats` |
| `~/.config/systemd/user/aimemory.service` | User service (copy from `systemd/`) |

## Project layout

```
src/aimemory/
  daemon.py      # main loop
  pressure.py    # PSI / MemAvailable zones
  apps.py        # cgroup app inventory
  score.py       # ranking + recommended action
  notify.py      # actionable notifications
  actions.py     # reclaim / close
  sampler.py     # local usage accumulation
  facts.py       # Gemini client + priority prompt
  focus.py       # Focused Window D-Bus
  audio.py       # PipeWire playing detection
  store.py       # SQLite
  config.py      # config + key paths
systemd/aimemory.service
```
