"""The single source of paths, constants and user settings."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from .files import write_text

APP_NAME = "Verba"
VERSION = "0.2.0"  # The one place of the version: the settings dialog, app.log and the exe (build.ps1) read it.

# Where the bundled files (the UI) live. In the exe this is the runtime extraction folder.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
# The data folder (sessions, settings, API key, log). The exe keeps it next to itself and leaves
# nothing on the PC, so the whole thing can be carried on a USB drive. From source it is the
# repository root. The VERBA_DATA environment variable overrides both (tests point it at a temp folder).
if os.environ.get("VERBA_DATA"):
    DATA_DIR = Path(os.environ["VERBA_DATA"])
elif getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent / f"{APP_NAME} Data"
else:
    DATA_DIR = BUNDLE
DATA_DIR.mkdir(parents=True, exist_ok=True)

UI_DIR = BUNDLE / "ui"
SESSIONS_DIR = DATA_DIR / "sessions"
SETTINGS_PATH = DATA_DIR / "settings.json"
USAGE_PATH = DATA_DIR / "usage.json"
ENV_PATH = DATA_DIR / ".env"
LOG_PATH = DATA_DIR / "app.log"
WEBVIEW_DIR = DATA_DIR / "webview"

PORT = 47831  # Fixed, so that it also detects a second instance.
SAMPLE_RATE = 16000  # 16 kHz is enough for speech recognition (Gemini downsamples to 16 kHz anyway).

# Target length (seconds) of a piece when a finished recording is transcribed as a whole (a recording
# not transcribed live, and every re-transcription). Long, to spend few requests: an hour is six.
# The two tracks of such a piece, sent as Ogg Vorbis, stay well under the 20 MB a request may carry.
BATCH_SECONDS = 600

SPEAKER_ME = "自分"
SPEAKER_OTHER = "相手"

# Models for transcription and summaries. The first is the default. A request goes to the chosen
# model; when that fails in a way another model may cure (out of quota, overloaded, retired, no
# answer), the others are tried in this order (gemini.py).
# A pinned version starts returning 404 once it is retired (seen with 2.5 Pro and 2.5 Flash-Lite
# in 2026-11), so apart from the default only the "latest" aliases, which Google keeps updating, are listed.
# careful: whether the model is trusted with work where a mistake does harm (the proper-noun fix).
# The lite model returned wrong replacements (seen with synthetic text).
MODELS = [
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "note": "既定（高精度・安定）", "careful": True},
    {"id": "gemini-flash-latest", "name": "Gemini Flash 最新版", "note": "自動で更新される", "careful": True},
    {"id": "gemini-flash-lite-latest", "name": "Gemini Flash-Lite 最新版", "note": "高速・軽量", "careful": False},
]
MODEL_IDS = [m["id"] for m in MODELS]

# Colour themes: a ground and a metal, chosen together, for the screen and the icon alike. Their
# colours are in ui/style.css (themes.py reads them from there). The first is the default.
THEMES = [
    {"id": "jet-gold", "name": "漆黒×金"},
    {"id": "jet-rose", "name": "漆黒×ローズゴールド"},
    {"id": "navy-gold", "name": "濃紺×金"},
    {"id": "green-gold", "name": "深緑×金"},
    {"id": "bordeaux-rose", "name": "ボルドー×ローズゴールド"},
    {"id": "violet-champagne", "name": "紫紺×シャンパンゴールド"},
]
THEME_IDS = [t["id"] for t in THEMES]

DEFAULT_SETTINGS = {
    "model": MODELS[0]["id"],
    "theme": THEMES[0]["id"],
    # Target length of a segment; the cut is made at a pause in speech after it.
    # Long, because the free tier allows few requests per day.
    "segment_seconds": 180,
    # Whether a recording is transcribed while it goes on (in segments of segment_seconds). If not,
    # nothing is sent until it ends, and the whole is then cut into long pieces (BATCH_SECONDS).
    "live": True,
    "mic": None,  # None = the Windows default device
    "speaker": None,
    "echo_removal": False,  # The other side is heard through speakers: remove its echo in the mic (echo.py)
    "echo_window": 3.0,  # How far (seconds) around an utterance of mine to look for the other side's utterances
    "echo_threshold": 0.8,  # How much of the text must match to count as echo (0.5 to 1)
}


def _checked(key: str, value):
    """Return the setting value in a usable form, or raise ValueError or TypeError if it cannot be used."""
    if (key == "model" and value not in MODEL_IDS) or (key == "theme" and value not in THEME_IDS):
        raise ValueError(key)
    if key in ("echo_removal", "live"):
        return bool(value)
    if key == "segment_seconds":
        return max(20, min(180, int(value)))
    if key == "echo_window":
        return max(0.0, min(10.0, float(value)))
    if key == "echo_threshold":
        return max(0.5, min(1.0, float(value)))
    return value


def load_api_key() -> str:
    """Read GEMINI_API_KEY from .env."""
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == "GEMINI_API_KEY":
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def save_api_key(key: str) -> None:
    """Replace GEMINI_API_KEY in .env (other lines are kept)."""
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = [line for line in lines if line.strip().partition("=")[0].strip() != "GEMINI_API_KEY"]
    lines.append(f"GEMINI_API_KEY={key}")
    write_text(ENV_PATH, "\n".join(lines) + "\n")


class Settings:
    """The settings the user can change from the UI, persisted in settings.json."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self._merge(saved)

    def get(self, key: str):
        """Return the value of one setting."""
        with self._lock:
            return self._data[key]

    def all(self) -> dict:
        """Return a copy of every setting."""
        with self._lock:
            return dict(self._data)

    def update(self, changes: dict) -> dict:
        """Take in the usable changes, save, and return every setting."""
        with self._lock:
            self._merge(changes)
            write_text(SETTINGS_PATH, json.dumps(self._data, ensure_ascii=False, indent=2))
            return dict(self._data)

    def _merge(self, changes: dict) -> None:
        """Take in only known keys with usable values."""
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                continue
            try:
                self._data[key] = _checked(key, value)
            except (TypeError, ValueError):
                pass
