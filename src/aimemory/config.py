from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "aimemory" / "config.toml"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "aimemory"
GEMINI_KEY_PATH = DEFAULT_CONFIG_PATH.parent / "gemini_api_key"


@dataclass
class Config:
    gemini_model: str = "gemini-flash-lite-latest"
    gemini_timeout_s: float = 20.0
    poll_interval_s: float = 2.0
    focus_poll_interval_s: float = 3.0
    yellow_mem_available_pct: float = 25.0
    red_mem_available_pct: float = 15.0
    yellow_psi_avg10: float = 1.0
    red_psi_avg10: float = 5.0
    swap_in_red_pages_per_s: float = 200.0
    close_idle_hours: float = 2.0
    cooldown_minutes: float = 30.0
    notify_gap_minutes: float = 5.0
    notify_expire_ms: int = 120_000
    denylist: list[str] = field(
        default_factory=lambda: [
            "aimemory",
            "gnome-shell",
            "org.gnome.Shell",
            "pipewire",
            "pipewire-pulse",
            "wireplumber",
            "xdg-desktop-portal",
            "xdg-document-portal",
            "xdg-permission-store",
            "dbus-broker",
            "systemd",
        ]
    )
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.db"

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        cfg = cls()
        config_path = path or DEFAULT_CONFIG_PATH
        if path is not None and not config_path.is_file():
            raise FileNotFoundError(f"config file not found: {config_path}")
        if config_path.is_file():
            with config_path.open("rb") as f:
                raw = tomllib.load(f)
            known = {f.name for f in fields(cls)}
            for key, value in raw.items():
                if key not in known:
                    continue
                if key == "data_dir":
                    setattr(cfg, key, Path(value).expanduser())
                else:
                    setattr(cfg, key, value)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg


def default_config_toml() -> str:
    return """\
# AIMEMORY configuration
# Gemini API key: run `aimemory set-key` or set GEMINI_API_KEY.
gemini_model = "gemini-flash-lite-latest"
gemini_timeout_s = 20.0
poll_interval_s = 2.0
focus_poll_interval_s = 3.0
yellow_mem_available_pct = 25.0
red_mem_available_pct = 15.0
yellow_psi_avg10 = 1.0
red_psi_avg10 = 5.0
swap_in_red_pages_per_s = 200.0
close_idle_hours = 2.0
cooldown_minutes = 30.0      # per app, after it was suggested
notify_gap_minutes = 5.0     # between any two notifications (bypassed on entering red)
notify_expire_ms = 120000

denylist = [
  "aimemory",
  "gnome-shell",
  "org.gnome.Shell",
  "pipewire",
  "pipewire-pulse",
  "wireplumber",
  "xdg-desktop-portal",
  "xdg-document-portal",
  "xdg-permission-store",
  "dbus-broker",
  "systemd",
]
"""


def ensure_user_config() -> Path:
    path = DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(default_config_toml(), encoding="utf-8")
    return path


def gemini_api_key() -> str | None:
    """GEMINI_API_KEY from the environment, else the key file written by set-key."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        key = GEMINI_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def save_gemini_api_key(key: str) -> Path:
    GEMINI_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(GEMINI_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
    os.chmod(GEMINI_KEY_PATH, 0o600)
    return GEMINI_KEY_PATH
