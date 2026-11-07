import asyncio
import html
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Protocol, TypedDict

import numpy as np

from .audio import ffmpeg_path, trim_silence
from .config import DEFAULT_VOICE, EDGE_KEEP, PARA_GAP, SAMPLE_RATE

Token = TypedDict("Token", {"p": int, "w": str, "s": str, "a": float | None, "b": float | None})

PREVIEW_TEXT = ("It is a truth universally acknowledged, that a single man in possession "
                "of a good fortune, must be in want of a wife.")

_FEMALE_US = "heart alloy aoede bella jessica kore nicole nova river sarah sky".split()
_MALE_US = "adam echo eric fenrir liam michael onyx puck santa".split()
_FEMALE_UK = "alice emma isabella lily".split()
_MALE_UK = "daniel fable george lewis".split()


# Microsoft Edge "Natural" voices via edge-tts (online, free, per-word boundaries)
_EDGE = (
    ("美式", "女", "en-US-", "AvaMultilingual EmmaMultilingual Aria Jenny Michelle"),
    ("美式", "男", "en-US-", "AndrewMultilingual BrianMultilingual Christopher Guy Eric Roger Steffan"),
    ("英式", "女", "en-GB-", "Sonia Libby Maisie"),
    ("英式", "男", "en-GB-", "Ryan Thomas"),
)
EDGE_VOICE_IDS: frozenset[str] = frozenset(
    f"{region}{n}Neural" for _, _, region, names in _EDGE for n in names.split())


def _edge_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("edge_tts") is not None


def edge_enabled() -> bool:
    """Microsoft voices are opt-in: KOKORO_READER_EDGE=1 in the process environment, else in the
    user's ~/.config/kokoro-reader/.env (KOKORO_TRANSLATE_ENV), and edge-tts installed.

    Off by default because edge-tts uses an unofficial Microsoft service that receives the chapter text.
    """
    val = os.environ.get("KOKORO_READER_EDGE")
    if val is None:
        from pathlib import Path

        from .translate import ENV_FILE_HINT, _read_env_file
        path = Path(os.environ.get("KOKORO_TRANSLATE_ENV") or ENV_FILE_HINT).expanduser()
        val = _read_env_file(path).get("KOKORO_READER_EDGE", "")
    return val.strip().lower() in ("1", "true", "yes", "on") and _edge_installed()


def voices() -> list[dict]:
    """The voices offered right now: Kokoro always, Microsoft only when switched on."""
    on = edge_enabled()
    return [v for v in VOICES if v["engine"] == "kokoro" or on]


def engine_of(voice: str) -> str:
    """"edge" for Microsoft (edge-tts) voices, "kokoro" for the rest."""
    return "edge" if voice in EDGE_VOICE_IDS else "kokoro"


def _voices() -> list[dict]:
    out = []
    for prefix, names, accent, gender in (
        ("af", _FEMALE_US, "美式", "女"), ("am", _MALE_US, "美式", "男"),
        ("bf", _FEMALE_UK, "英式", "女"), ("bm", _MALE_UK, "英式", "男"),
    ):
        for n in names:
            out.append({"id": f"{prefix}_{n}", "label": f"{n.capitalize()}（{accent}{gender}声）",
                        "accent": accent, "gender": gender, "engine": "kokoro"})
    for accent, gender, region, names in _EDGE:
        for n in names.split():
            out.append({"id": f"{region}{n}Neural", "label": f"{n.removesuffix('Multilingual')}（{accent}{gender}声·微软）",
                        "accent": accent, "gender": gender, "engine": "edge"})
    for v in out:
        v["default"] = v["id"] == DEFAULT_VOICE
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


def edge_tokens(pi: int, text: str, words: list[tuple[str, float, float]]) -> list[Token]:
    """Tokens for one paragraph from edge-tts word boundaries (times relative to its audio).

    The tokens rebuild `text` exactly: each boundary word found in the text (in order) becomes a
    timed token; everything between words (punctuation) becomes untimed tokens; whitespace goes
    into the preceding token's `s`. Boundary words not found in the text are skipped.
    """
    toks: list[Token] = []

    def plain(chunk: str) -> None:
        for m in re.finditer(r"(\S*)(\s*)", chunk):
            w, sp = m.groups()
            if w:
                toks.append({"p": pi, "w": w, "s": sp, "a": None, "b": None})
            elif sp:
                if toks:
                    toks[-1]["s"] += sp
                else:  # leading whitespace: keep it so the text still rebuilds
                    toks.append({"p": pi, "w": "", "s": sp, "a": None, "b": None})

    pos = 0
    for raw, a, b in words:
        w = html.unescape(raw)
        i = text.find(w, pos) if w else -1
        if i < 0:
            continue
        plain(text[pos:i])
        toks.append({"p": pi, "w": w, "s": "", "a": round(a, 3), "b": round(b, 3)})
        pos = i + len(w)
    plain(text[pos:])
    return toks


def _decode_mp3(data: bytes) -> np.ndarray:
    out = subprocess.run([ffmpeg_path(), "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1",
                          "-ar", str(SAMPLE_RATE), "pipe:1"], input=data, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def _edge_fetch(text: str, voice: str) -> tuple[np.ndarray, list[tuple[str, float, float]]]:
    """One edge-tts request: (24 kHz float samples, [(word, start_s, end_s)])."""
    import edge_tts

    async def run():
        buf, words = bytearray(), []
        async for ch in edge_tts.Communicate(text, voice, boundary="WordBoundary").stream():
            if ch["type"] == "audio":
                buf += ch["data"]
            elif ch["type"] == "WordBoundary":
                words.append((ch["text"], ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7))
        return bytes(buf), words

    data, words = asyncio.run(run())
    return _decode_mp3(data), words


class EdgeSynth:
    """Microsoft Natural voices via edge-tts: one request per paragraph, several at once, with retries.

    edge-tts uses the Edge browser's read-aloud service (not an official API): requests now and then
    fail ("No audio was received", 403 after Microsoft changes something), so each paragraph is retried.
    """
    CONCURRENCY = 8
    ATTEMPTS = 4

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch or _edge_fetch

    def _fetch_retry(self, text: str, voice: str):
        for attempt in range(self.ATTEMPTS):
            try:
                return self._fetch(text, voice)
            except Exception:
                if attempt == self.ATTEMPTS - 1:
                    raise
                time.sleep(2 ** attempt)

    def synth_chapter(self, paragraphs: list[str], voice: str,
                      on_progress: ProgressFn = None) -> tuple[np.ndarray, list[Token]]:
        todo = [i for i, p in enumerate(paragraphs) if re.search(r"\w", p)]
        got: dict[int, tuple] = {}
        done = len(paragraphs) - len(todo)
        with ThreadPoolExecutor(self.CONCURRENCY) as pool:
            futs = {pool.submit(self._fetch_retry, paragraphs[i], voice): i for i in todo}
            for f in as_completed(futs):
                got[futs[f]] = f.result()
                done += 1
                _report(on_progress, done, len(paragraphs))
        if not todo:
            _report(on_progress, len(paragraphs), max(len(paragraphs), 1))
        chunks: list[np.ndarray] = []
        toks: list[Token] = []
        t = 0.0
        for pi in todo:
            raw, words = got[pi]
            audio, cut = trim_silence(np.asarray(raw, dtype=np.float32), EDGE_KEEP)
            span = len(audio) / SAMPLE_RATE
            for tok in edge_tokens(pi, paragraphs[pi], words):
                if tok["a"] is not None:
                    tok["a"] = round(t + min(max(tok["a"] - cut, 0.0), span), 3)
                    tok["b"] = round(t + min(max(tok["b"] - cut, 0.0), span), 3)
                toks.append(tok)
            chunks.append(audio)
            chunks.append(np.zeros(int(PARA_GAP * SAMPLE_RATE), dtype=np.float32))
            t += span + PARA_GAP
        if not chunks:
            return np.zeros(0, dtype=np.float32), []
        return np.concatenate(chunks), toks


class RoutingSynth:
    """Picks the engine by voice id: Microsoft (edge-tts) voices to EdgeSynth, the rest to Kokoro."""

    def __init__(self, kokoro: Synthesizer | None = None, edge: Synthesizer | None = None) -> None:
        self.kokoro = kokoro if kokoro is not None else KokoroSynth()
        self.edge = edge if edge is not None else EdgeSynth()

    def synth_chapter(self, paragraphs: list[str], voice: str,
                      on_progress: ProgressFn = None) -> tuple[np.ndarray, list[Token]]:
        if engine_of(voice) == "edge":
            if not edge_enabled():
                raise RuntimeError("微软音色没有开启：设 KOKORO_READER_EDGE=1 并安装 edge-tts")
            return self.edge.synth_chapter(paragraphs, voice, on_progress)
        return self.kokoro.synth_chapter(paragraphs, voice, on_progress)


def synth_preview(synth: Synthesizer, voice: str) -> np.ndarray:
    samples, _ = synth.synth_chapter([PREVIEW_TEXT], voice)
    return samples
