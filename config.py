"""Persistent settings for ytcli, stored as JSON at ~/.config/ytcli/config.json.

Pure file helpers with **no Textual imports**, so they may be called safely from
@work(thread=True) worker threads without touching the UI event loop.

Schema (all keys optional; missing ones fall back to defaults)::

    {
      "search_results_count": 10,          # results fetched per ytsearch query
      "default_format": "best_video",      # one of the yt_downloader preset ids
      "download_dir": "~/Downloads/ytcli", # where downloads / exports land
      "startup": {                          # toggles applied at app boot
          "thumbnails": false,
          "visualizer": false,
          "autoplay_next": true
      },
      "toast_duration": 3.0,               # notify() timeout, seconds
      "search_history": ["some query", ...] # most-recent-last, capped
    }
"""
import json
import os
from typing import Any

CONFIG_HOME = os.path.join(os.path.expanduser("~"), ".config", "ytcli")
CONFIG_FILE = os.path.join(CONFIG_HOME, "config.json")

# How many search queries to remember / re-callable via Up in the search box.
MAX_HISTORY = 25

# File-name prefix used by queue export / import in the download directory.
QUEUE_EXPORT_PREFIX = "ytcli-queue-"

DEFAULT_CONFIG: dict[str, Any] = {
    "search_results_count": 10,
    "default_format": "best_video",
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "ytcli"),
    "startup": {
        "thumbnails": False,
        "visualizer": False,
        "autoplay_next": True,
    },
    "toast_duration": 3.0,
    "search_history": [],
}


def config_dir() -> str:
    """Directory holding config.json (created lazily on save)."""
    return CONFIG_HOME


def config_path() -> str:
    """Absolute path of the settings file."""
    return CONFIG_FILE


def default_config() -> dict[str, Any]:
    """Return a fresh deep copy of the default settings."""
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``extra`` over ``base`` (both plain dicts)."""
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return fallback


def _normalise(cfg: dict[str, Any]) -> dict[str, Any]:
    """Coerce every field to the right type, clamping to sane ranges."""
    merged = _merge(default_config(), cfg)

    try:
        count = int(merged.get("search_results_count", 10))
    except (TypeError, ValueError):
        count = 10
    merged["search_results_count"] = max(1, min(100, count))

    fmt = merged.get("default_format")
    if not isinstance(fmt, str) or not fmt:
        merged["default_format"] = "best_video"

    ddir = merged.get("download_dir")
    merged["download_dir"] = os.path.expanduser(str(ddir) if ddir else "")

    startup = merged.get("startup")
    if not isinstance(startup, dict):
        startup = {}
    merged["startup"] = {
        "thumbnails": _as_bool(startup.get("thumbnails"), False),
        "visualizer": _as_bool(startup.get("visualizer"), False),
        "autoplay_next": _as_bool(startup.get("autoplay_next"), True),
    }

    try:
        toast = float(merged.get("toast_duration", 3.0))
    except (TypeError, ValueError):
        toast = 3.0
    merged["toast_duration"] = max(0.5, min(30.0, toast))

    history = merged.get("search_history")
    if not isinstance(history, list):
        history = []
    merged["search_history"] = [
        str(h)[:300] for h in history if isinstance(h, str) and h.strip()
    ][-MAX_HISTORY:]

    return merged


def load_config() -> dict[str, Any]:
    """Load settings from disk, merged over defaults.

    Never raises: on a missing/corrupt file the defaults are returned so the app
    always boots with a usable configuration.
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return default_config()
        return _normalise(raw)
    except FileNotFoundError:
        return default_config()
    except (json.JSONDecodeError, OSError):
        return default_config()


def save_config(cfg: dict[str, Any]) -> None:
    """Write settings atomically (tmp file + rename) to disk.

    Raises OSError on failure so the caller (a worker thread) can surface it.
    """
    normalised = _normalise(cfg)
    os.makedirs(CONFIG_HOME, exist_ok=True)
    tmp_path = CONFIG_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(normalised, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, CONFIG_FILE)
