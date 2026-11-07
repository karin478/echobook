"""预计时间 / 大小: how long converting takes, and how much disk the audio will need.

Learned from this machine's own recent conversions (``<home>/speed.json``: chars, seconds of work,
seconds of audio per chapter), with defaults measured on Apple Silicon until there are some.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import MP3_BITRATE

CONV_CPS = 85.0    # characters converted per second of work (Kokoro on an Apple GPU)
AUDIO_CPS = 17.5   # characters read per second of audio (af_heart at normal speed)
KEEP = 30          # the last N chapters count: the machine (or its load) may change


def _audio_bytes_per_sec() -> int:
    return int(MP3_BITRATE.rstrip("k")) * 1000 // 8


class SpeedLog:
    def __init__(self, path: Path, keep: int = KEEP):
        self.path = Path(path)
        self.keep = keep
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            runs = json.loads(self.path.read_text())
            return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
        except (OSError, ValueError):
            return []

    def record(self, chars: int, seconds: float, audio_seconds: float) -> None:
        if chars <= 0 or seconds <= 0 or audio_seconds <= 0:
            return  # an empty or instant chapter says nothing about speed
        with self._lock:
            runs = (self._load() + [{"chars": chars, "seconds": seconds, "audio": audio_seconds}])[-self.keep:]
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(runs))
            tmp.replace(self.path)

    def rates(self) -> dict:
        runs = self._load()
        chars = sum(r.get("chars", 0) for r in runs)
        secs = sum(r.get("seconds", 0) for r in runs)
        audio = sum(r.get("audio", 0) for r in runs)
        ok = chars > 0 and secs > 0 and audio > 0
        return {
            "conv_cps": chars / secs if ok else CONV_CPS,
            "audio_cps": chars / audio if ok else AUDIO_CPS,
            "audio_bytes_per_sec": _audio_bytes_per_sec(),
            "measured": len(runs) if ok else 0,
        }


def eta_by_book(snapshot: dict, chars_of, conv_cps: float) -> dict[str, float]:
    """Seconds until each book's last queued chapter is done, walking the queue in order."""
    t = 0.0
    eta: dict[str, float] = {}
    cur = snapshot.get("current")
    if cur:
        t += chars_of(cur["book_id"], cur["idx"]) * (1 - float(cur.get("progress") or 0)) / conv_cps
        eta[cur["book_id"]] = t
    for book_id, idx in snapshot.get("pending", []):
        t += chars_of(book_id, idx) / conv_cps
        eta[book_id] = t
    return eta
