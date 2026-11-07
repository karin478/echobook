"""划词「翻译」via free machine translation: fast (tens of ms) and no tokens.

Google's `translate.googleapis.com` gtx endpoint often answers 429 to shared home/student
broadband IPs; the `clients5` dict-chrome-ex endpoint (the one Google's dictionary extension
uses) sits in a different rate-limit bucket and answers. Neither is an official API, so MyMemory
(public, keyless, daily quota) backs it up, and the caller falls back to the LLM when both fail.
Free MT translates the fragment on its own — context is what 讲解 is for.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

UA = "kokoro-reader"
TIMEOUT = 4.0
COOLDOWN = 600.0  # seconds a failing channel is skipped


class MTError(Exception):
    pass


def _get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def google(text: str, timeout: float = TIMEOUT) -> str:
    q = urllib.parse.urlencode({"client": "dict-chrome-ex", "sl": "en", "tl": "zh-CN", "q": text})
    data = _get_json("https://clients5.google.com/translate_a/t?" + q, timeout)
    first = data[0]  # ["译文"], or [["译文", "en"]] when the source language is detected
    out = first[0] if isinstance(first, list) else first
    if not isinstance(out, str) or not out.strip():
        raise MTError("google: empty reply")
    return out.strip()


def mymemory(text: str, timeout: float = TIMEOUT) -> str:
    q = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
    data = _get_json("https://api.mymemory.translated.net/get?" + q, timeout)
    if data.get("responseStatus") != 200 or data.get("quotaFinished"):
        raise MTError(f"mymemory: {data.get('responseDetails') or data.get('responseStatus')}")
    out = (data.get("responseData") or {}).get("translatedText") or ""
    if not out.strip():
        raise MTError("mymemory: empty reply")
    return out.strip()


Channel = tuple[str, Callable[[str], str]]


class QuickMT:
    """Try each channel in order; one that fails is skipped for COOLDOWN seconds."""

    def __init__(self, channels: list[Channel] | None = None, clock: Callable[[], float] = time.monotonic):
        self.channels = channels or [("google", google), ("mymemory", mymemory)]
        self.clock = clock
        self.down_until: dict[str, float] = {}
        self.lock = threading.Lock()

    def translate(self, text: str) -> tuple[str, str]:
        text = text.strip()
        errors = []
        for name, fn in self.channels:
            with self.lock:
                if self.down_until.get(name, 0.0) > self.clock():
                    errors.append(f"{name}: cooling down")
                    continue
            try:
                return fn(text), name
            except (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError, IndexError,
                    TypeError, MTError) as e:
                errors.append(f"{name}: {e}")
                with self.lock:
                    self.down_until[name] = self.clock() + COOLDOWN
        raise MTError("; ".join(errors))


def make_mt() -> QuickMT | None:
    """None under the fake translator (tests, E2E): 翻译 then goes to the fake model."""
    if os.environ.get("KOKORO_READER_FAKE_TRANSLATE") == "1":
        return None
    return QuickMT()
