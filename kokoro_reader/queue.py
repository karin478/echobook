"""Single-worker priority conversion queue: chapters -> mp3 + word tokens."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from typing import Literal

from .audio import write_mp3
from .library import Library
from .store import Store
from .tts import Synthesizer

log = logging.getLogger(__name__)

Item = tuple[str, int]


class Cancelled(Exception):
    """The service is stopping or the book was deleted: abandon this chapter, write nothing."""


QOS_CLASS_UTILITY = 0x11


def lower_worker_priority() -> None:
    """Lower the *calling* thread's scheduling priority (call it first thing in the worker).

    macOS: set the thread's QoS class to UTILITY via pthread_set_qos_class_self_np, so
    the scheduler favours interactive work (and the rest of the Mac) over conversion.
    This is thread-scoped: the API/playback threads keep their default QoS. Calling it
    before torch creates its thread pools lets those pools inherit the lowered QoS.
    (os.setpriority(PRIO_DARWIN_THREAD, ...) only honours PRIO_DARWIN_BG and is otherwise
    a silent no-op, and os.nice() is process-wide.)
    Elsewhere: os.nice(10). Failures are logged, never raised.
    """
    try:
        if sys.platform == "darwin":
            lib = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
            fn = lib.pthread_set_qos_class_self_np
            fn.argtypes = [ctypes.c_uint, ctypes.c_int]
            fn.restype = ctypes.c_int
            rc = fn(QOS_CLASS_UTILITY, 0)
            if rc != 0:
                raise OSError(rc, os.strerror(rc))
        else:
            os.nice(10)
    except Exception:
        log.warning("could not lower worker priority", exc_info=True)


class ConversionQueue:
    def __init__(self, store: Store, library: Library, synth: Synthesizer, autostart: bool = True):
        self.store = store
        self.library = library
        self.synth = synth
        self._cond = threading.Condition()
        self._pending: list[Item] = []
        self._current: dict | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.store.reset_stuck()  # recover tasks stuck by a previous crash/restart
        if autostart:
            self.start()

    # ---- enqueue API ----
    def _statuses(self, book_id: str) -> dict[int, dict]:
        return {c["idx"]: c for c in self.store.list_chapters(book_id)}

    def _add(self, book_id: str, idxs: list[int], priority: str, force: bool = False) -> None:
        with self._cond:
            # read statuses under the lock so a finishing worker can't race us
            chapters = self._statuses(book_id)
            wanted: list[Item] = []
            for idx in dict.fromkeys(idxs):
                ch = chapters.get(idx)
                if ch is None:
                    continue
                if self._current and (self._current["book_id"], self._current["idx"]) == (book_id, idx):
                    continue
                if ch["status"] == "done" and not force:
                    continue
                wanted.append((book_id, idx))
            if not wanted:
                return
            if priority == "high":
                ws = set(wanted)
                self._pending = wanted + [it for it in self._pending if it not in ws]
            else:
                self._pending += [it for it in wanted if it not in self._pending]
            for b, i in wanted:
                self.store.set_chapter_status(b, i, "queued")
            self._cond.notify_all()

    def enqueue(self, book_id: str, idxs: list[int], priority: Literal["high", "normal"]) -> None:
        self._add(book_id, list(idxs), priority)

    def enqueue_open(self, book_id: str, idx: int) -> list[int]:
        """Queue the opened chapter and the next story chapter; returns those chapters."""
        chapters = self.store.list_chapters(book_id)
        todo = []
        cur = next((c for c in chapters if c["idx"] == idx), None)
        if cur is not None:
            todo.append(idx)
        nxt = next((c for c in chapters if c["idx"] > idx and not c["is_matter"]), None)
        if nxt is not None:
            todo.append(nxt["idx"])
        self._add(book_id, todo, "high")
        return todo

    def enqueue_batch(self, book_id: str, mode: Literal["next5", "all"]) -> list[int]:
        chapters = self.store.list_chapters(book_id)
        first = next((c["idx"] for c in chapters if not c["is_matter"] and c["status"] != "done"), None)
        if first is None:
            return []
        todo = [c["idx"] for c in chapters
                if c["idx"] >= first and not c["is_matter"] and c["status"] != "done"]
        if mode == "next5":
            todo = todo[:5]
        self._add(book_id, todo, "normal")
        return todo

    def reconvert(self, book_id: str, idx: int) -> None:
        self._add(book_id, [idx], "high", force=True)

    def snapshot(self) -> dict:
        with self._cond:
            cur = dict(self._current) if self._current else None
            return {"current": cur, "pending": [[b, i] for b, i in self._pending]}

    # ---- processing ----
    def run_once(self) -> bool:
        with self._cond:
            if not self._pending:
                return False
            book_id, idx = self._pending.pop(0)
            self._current = {"book_id": book_id, "idx": idx, "progress": 0.0}
        try:
            self._process(book_id, idx)
        finally:
            with self._cond:
                self._current = None
        return True

    def _on_progress(self, p: float) -> None:
        with self._cond:
            cur = self._current
            if cur is not None:
                cur["progress"] = float(p)
            stopping = self._stopping
        if stopping or (cur is not None and self.store.get_book(cur["book_id"]) is None):
            raise Cancelled

    def _process(self, book_id: str, idx: int) -> None:
        try:
            book = self.store.get_book(book_id)
            if book is None:  # deleted while queued
                return
            voice = book["voice"]
            self.store.set_chapter_status(book_id, idx, "converting")
            paragraphs = self.library.paragraphs(book_id, idx)
            samples, tokens = self.synth.synth_chapter(paragraphs, voice, self._on_progress)
            if self.store.get_book(book_id) is None:  # deleted mid-conversion: write nothing
                return
            mp3 = self.library.chapter_audio_path(book_id, idx)
            js = self.library.chapter_tokens_path(book_id, idx)
            js.parent.mkdir(exist_ok=True)  # FileNotFoundError if the book folder is gone
            # temp file in the same dir + os.replace: readers never see a half-written file
            if len(samples) == 0:
                duration = 0.0
                mp3.unlink(missing_ok=True)
            else:
                tmp_mp3 = mp3.with_name(mp3.name + ".tmp")
                duration = write_mp3(samples, tmp_mp3)
                os.replace(tmp_mp3, mp3)
            tmp_js = js.with_name(js.name + ".tmp")
            tmp_js.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_js, js)
            self.store.set_chapter_status(book_id, idx, "done", duration=duration, voice=voice)
        except Cancelled:
            log.info("conversion cancelled: book=%s chapter=%s", book_id, idx)
            self._drop_tmp(book_id, idx)
        except Exception:
            if self.store.get_book(book_id) is None:  # deleted mid-write: nothing to report
                self._drop_tmp(book_id, idx)
                return
            log.exception("conversion failed: book=%s chapter=%s", book_id, idx)
            self._drop_tmp(book_id, idx)
            try:
                self.store.set_chapter_status(book_id, idx, "error")
            except Exception:
                log.exception("could not mark chapter as error")

    def _drop_tmp(self, book_id: str, idx: int) -> None:
        for p in (self.library.chapter_audio_path(book_id, idx),
                  self.library.chapter_tokens_path(book_id, idx)):
            try:
                p.with_name(p.name + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass

    # ---- worker thread ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="conversion-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        t = self._thread
        if t is not None:
            t.join()
        self._thread = None

    def _run(self) -> None:
        lower_worker_priority()
        try:
            import torch
            torch.set_num_threads(4)
        except ImportError:
            pass
        except Exception:
            log.exception("torch setup failed; continuing without it")
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
            self.run_once()
