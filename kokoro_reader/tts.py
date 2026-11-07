import os
import re
import threading
import time
from typing import Callable, Protocol, TypedDict

import numpy as np

from .audio import trim_silence
from .config import EDGE_KEEP, PARA_GAP, SAMPLE_RATE

Token = TypedDict("Token", {"p": int, "w": str, "s": str, "a": float | None, "b": float | None})

PREVIEW_TEXT = ("It is a truth universally acknowledged, that a single man in possession "
                "of a good fortune, must be in want of a wife.")

_FEMALE_US = "heart alloy aoede bella jessica kore nicole nova river sarah sky".split()
_MALE_US = "adam echo eric fenrir liam michael onyx puck santa".split()
_FEMALE_UK = "alice emma isabella lily".split()
_MALE_UK = "daniel fable george lewis".split()


def _voices() -> list[dict]:
    out = []
    for prefix, names, accent, gender in (
        ("af", _FEMALE_US, "美式", "女"), ("am", _MALE_US, "美式", "男"),
        ("bf", _FEMALE_UK, "英式", "女"), ("bm", _MALE_UK, "英式", "男"),
    ):
        for n in names:
            out.append({"id": f"{prefix}_{n}", "label": f"{n.capitalize()}（{accent}{gender}声）",
                        "accent": accent, "gender": gender})
    return out


VOICES: list[dict] = _voices()

ProgressFn = Callable[[float], None] | None


class Synthesizer(Protocol):
    def synth_chapter(self, paragraphs: list[str], voice: str,
                      on_progress: ProgressFn = None) -> tuple[np.ndarray, list[Token]]: ...


def _report(on_progress: ProgressFn, done: int, total: int) -> None:
    if on_progress is not None:
        on_progress(done / total)


class FakeSynth:
    """Deterministic stand-in: 0.3 s per word, trailing punctuation as zero-length tokens, zero audio.

    `KOKORO_READER_FAKE_DELAY` (seconds, default 0) sleeps once per paragraph so E2E tests
    can watch a conversion in progress.
    """
    WORD_SEC = 0.3
    _CORE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)

    def synth_chapter(self, paragraphs: list[str], voice: str,
                      on_progress: ProgressFn = None) -> tuple[np.ndarray, list[Token]]:
        delay = float(os.environ.get("KOKORO_READER_FAKE_DELAY") or 0)
        chunks: list[np.ndarray] = []
        toks: list[Token] = []
        t = 0.0
        for pi, para in enumerate(paragraphs):
            if delay > 0:
                time.sleep(delay)
            words = para.split()
            if any(re.search(r"\w", w) for w in words):
                n_words = 0
                for wi, raw in enumerate(words):
                    last = wi == len(words) - 1
                    lead, core, trail = self._CORE.match(raw).groups()
                    if not core:  # pure punctuation chunk, e.g. "•" or "—"
                        toks.append({"p": pi, "w": raw, "s": "" if last else " ", "a": None, "b": None})
                        continue
                    if lead:
                        toks.append({"p": pi, "w": lead, "s": "", "a": None, "b": None})
                    toks.append({"p": pi, "w": core, "s": "" if trail or last else " ",
                                 "a": round(t, 3), "b": round(t + self.WORD_SEC, 3)})
                    t += self.WORD_SEC
                    n_words += 1
                    for ci, ch in enumerate(trail):
                        toks.append({"p": pi, "w": ch, "s": " " if (ci == len(trail) - 1 and not last) else "",
                                     "a": round(t, 3), "b": round(t, 3)})
                chunks.append(np.zeros(int(round(n_words * self.WORD_SEC * SAMPLE_RATE)), dtype=np.float32))
                chunks.append(np.zeros(int(PARA_GAP * SAMPLE_RATE), dtype=np.float32))
                t += PARA_GAP
            _report(on_progress, pi + 1, len(paragraphs))
        if not chunks:
            return np.zeros(0, dtype=np.float32), []
        return np.concatenate(chunks), toks


def _mps_available() -> bool:
    try:
        import torch
        return bool(torch.backends.mps.is_available())
    except Exception:
        return False


def pick_device() -> str:
    """Torch device for Kokoro: KOKORO_READER_DEVICE if set, else Apple GPU (mps) when available, else cpu.

    Measured on Apple Silicon (one 17-minute chapter): mps ~40% faster than cpu, with the same
    tokens and per-word timestamps and the same Whisper round-trip error rate.
    """
    env = os.environ.get("KOKORO_READER_DEVICE")
    if env:
        return env
    return "mps" if _mps_available() else "cpu"


class KokoroSynth:
    """Real Kokoro; pipelines created lazily and cached per lang_code."""

    def __init__(self, device: str | None = None) -> None:
        self._device = device
        self._pipes: dict[str, object] = {}
        self._pipes_lock = threading.Lock()

    @property
    def device(self) -> str:
        # resolved lazily (first use is on the conversion worker), so startup never imports torch
        if self._device is None:
            self._device = pick_device()
        return self._device

    def _pipe(self, voice: str):
        lang = voice[0]
        if lang not in self._pipes:
            with self._pipes_lock:
                if lang not in self._pipes:
                    from kokoro import KPipeline
                    self._pipes[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M",
                                                device=self.device)
        return self._pipes[lang]

    def synth_chapter(self, paragraphs: list[str], voice: str,
                      on_progress: ProgressFn = None) -> tuple[np.ndarray, list[Token]]:
        pipe = self._pipe(voice) if any(re.search(r"\w", p) for p in paragraphs) else None
        chunks: list[np.ndarray] = []
        toks: list[Token] = []
        t = 0.0
        for pi, para in enumerate(paragraphs):
            produced = False
            if pipe is not None and re.search(r"\w", para):
                for res in pipe(para, voice=voice):
                    raw = res.audio.numpy() if hasattr(res.audio, "numpy") else np.asarray(res.audio)
                    audio, cut = trim_silence(raw, EDGE_KEEP)
                    span = len(audio) / SAMPLE_RATE
                    for tok in res.tokens or []:
                        # untimed tokens are kept so the rendered text stays complete
                        timed = tok.start_ts is not None and tok.end_ts is not None
                        toks.append({
                            "p": pi, "w": tok.text, "s": tok.whitespace,
                            "a": round(t + min(max(tok.start_ts - cut, 0.0), span), 3) if timed else None,
                            "b": round(t + min(max(tok.end_ts - cut, 0.0), span), 3) if timed else None,
                        })
                    chunks.append(audio.astype(np.float32))
                    t += len(audio) / SAMPLE_RATE
                    produced = True
            if produced:
                chunks.append(np.zeros(int(PARA_GAP * SAMPLE_RATE), dtype=np.float32))
                t += PARA_GAP
            _report(on_progress, pi + 1, len(paragraphs))
        if not chunks:
            return np.zeros(0, dtype=np.float32), []
        return np.concatenate(chunks), toks


def synth_preview(synth: Synthesizer, voice: str) -> np.ndarray:
    samples, _ = synth.synth_chapter([PREVIEW_TEXT], voice)
    return samples
