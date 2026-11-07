"""Background chapter translation jobs + the sentence/segment view served to the reader."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from . import glossary as gl
from .library import Library
from .translate import Translator, norm, sentence_spans, translate_chapter


class GlossaryError(RuntimeError):
    pass


class TranslationJobs:
    """Chapter translations on a small pool. Waiting chapters run in priority order: the one
    the reader just clicked or opened (`priority="high"`) goes ahead of a whole-book batch."""

    def __init__(self, library: Library, translator_factory: Callable[[], Translator],
                 max_chapters: int = 4):
        self.library = library
        self.factory = translator_factory
        self._pool = ThreadPoolExecutor(max_workers=max_chapters, thread_name_prefix="translate")
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, int], dict] = {}
        self._pending: list[tuple[str, int]] = []           # waiting jobs, front runs first
        self._runs: dict[tuple[str, int], Callable[[], None]] = {}
        self._translator: Translator | None = None
        self._glossary_builds: dict[str, Future] = {}

    def _get_translator(self) -> Translator:
        with self._lock:
            if self._translator is None:
                self._translator = self.factory()
            return self._translator

    def book_glossary(self, book_id: str, book_paragraphs: Callable[[], list[str]]) -> list[dict]:
        """The book's glossary, built once (one model call) from every story chapter's text.
        The first chapter to need it builds it; chapters that ask meanwhile wait for that same
        build and share its result, or its failure (GlossaryError: the chapter is not translated
        without it, so every chapter reads the same; nothing is saved, so 重试 builds again)."""
        saved = self.library.glossary(book_id)
        if saved is not None:
            return saved
        with self._lock:
            build = self._glossary_builds.get(book_id)
            leader = build is None
            if leader:
                build = self._glossary_builds[book_id] = Future()
        if not leader:
            return build.result()
        try:
            entries = self.library.glossary(book_id)  # finished just before we took the lead
            if entries is None:
                t = self._get_translator()
                entries = gl.build_glossary(book_paragraphs(), t)
                self.library.save_glossary(book_id, entries, getattr(t, "model", ""))
            build.set_result(entries)
            return entries
        except Exception as e:
            logging.getLogger(__name__).exception("glossary for %s failed", book_id)
            err = GlossaryError(f"术语表生成失败：{e or type(e).__name__}")
            build.set_exception(err)
            raise err from e
        finally:
            if not build.done():  # e.g. KeyboardInterrupt: never leave waiting chapters hanging
                build.set_exception(GlossaryError("术语表生成被中断"))
            with self._lock:
                self._glossary_builds.pop(book_id, None)

    def _status_locked(self, book_id: str, idx: int) -> dict:
        job = self._jobs.get((book_id, idx))
        if job and job["status"] in ("queued", "running"):
            return dict(job)
        if self.library.translation_path(book_id, idx).exists():
            return {"status": "done", "progress": 1.0, "error": None}
        if job:
            return dict(job)
        return {"status": "none", "progress": 0.0, "error": None}

    def status(self, book_id: str, idx: int) -> dict:
        with self._lock:
            return self._status_locked(book_id, idx)

    def start(self, book_id: str, idx: int, paragraphs: list[str], title: str,
              book_paragraphs: Callable[[], list[str]] | None = None,
              priority: str = "normal") -> dict:
        key = (book_id, idx)
        job = {"status": "queued", "progress": 0.0, "error": None}

        def progress(x: float) -> None:
            with self._lock:
                job["progress"] = round(x, 3)

        def run() -> None:
            with self._lock:
                job["status"] = "running"
            try:
                glossary = self.book_glossary(book_id, book_paragraphs) if book_paragraphs else None
                data = translate_chapter(paragraphs, self._get_translator(), title, progress,
                                         glossary=glossary)
                self.library.save_translation(book_id, idx, data)
                with self._lock:
                    job.update(status="done", progress=1.0)
            except Exception as e:  # surfaced to the reader as 翻译失败
                with self._lock:
                    job.update(status="error", error=str(e) or type(e).__name__)

        with self._lock:
            st = self._status_locked(book_id, idx)
            if st["status"] == "queued" and priority == "high" and key in self._pending:
                self._pending.remove(key)
                self._pending.insert(0, key)
            if st["status"] in ("queued", "running", "done"):
                return st
            self._jobs[key] = job
            self._runs[key] = run
            if priority == "high":
                self._pending.insert(0, key)
            else:
                self._pending.append(key)
        self._pool.submit(self._run_next)
        return dict(job)

    def _run_next(self) -> None:
        """One pool slot per started job; each slot runs whichever job is first in line now."""
        with self._lock:
            if not self._pending:
                return
            run = self._runs.pop(self._pending.pop(0))
        run()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def sentence_view(shown: list[str], stored: dict | None) -> list[dict]:
    """Per displayed paragraph: sentence `spans` and, when translated, aligned `segs`.

    A paragraph whose sentences no longer match what was translated (the display text changed)
    collapses to one whole-paragraph span with its full translation, so nothing mis-lines up.
    """
    entries = (stored or {}).get("paragraphs") or []
    out = []
    for i, text in enumerate(shown):
        spans = [list(s) for s in sentence_spans(text)]
        if stored is None:
            out.append({"spans": spans, "segs": None})
            continue
        e = entries[i] if i < len(entries) else None
        if e is None or not e.get("sents"):
            out.append({"spans": spans, "segs": [] if not spans else None})
            continue
        if [norm(text[a:b]) for a, b in spans] == [norm(s) for s in e["sents"]]:
            out.append({"spans": spans, "segs": e["segs"]})
            continue
        lead = len(text) - len(text.lstrip())
        whole = [[lead, len(text.rstrip())]] if text.strip() else []
        out.append({"spans": whole, "segs": [{"from": 0, "to": 0, "zh": e["zh"]}] if whole else []})
    return out
