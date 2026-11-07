import ctypes
import json
import sys
import threading
import time

import numpy as np
import pytest

from kokoro_reader.library import Library
from kokoro_reader.queue import ConversionQueue, lower_worker_priority
from kokoro_reader.store import Store
from kokoro_reader.tts import FakeSynth
from tests.epub_factory import make_epub

LONG = "This is an ordinary sentence of the story that keeps going. " * 8
N = 8


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = Store(home / "kokoro.db")
    lib = Library(home, store)
    docs = [(f"c{i}.xhtml", f"<h1>Chapter {i}</h1><p>{LONG}</p><p>Second para.</p>") for i in range(N)]
    toc = [(f"Chapter {i}", f"c{i}.xhtml") for i in range(N)]
    epub = make_epub(tmp_path / "b.epub", title="Book", author="A", docs=docs, toc=toc)
    bid, _ = lib.import_epub(epub)
    assert len(store.list_chapters(bid)) == N
    for i in range(N):
        store.set_chapter_matter(bid, i, False)
    return store, lib, bid


def make_q(env, synth=None):
    store, lib, bid = env
    return ConversionQueue(store, lib, synth or FakeSynth(), autostart=False), store, lib, bid


def status(store, bid, idx):
    return store.list_chapters(bid)[idx]["status"]


def drain(q):
    done = []
    while True:
        cur_before = q.snapshot()["pending"][:1]
        if not q.run_once():
            return done
        done.append(cur_before[0][1])


def test_fifo_normal(env):
    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [1, 2, 3], "normal")
    assert status(store, bid, 1) == "queued"
    assert drain(q) == [1, 2, 3]
    assert all(status(store, bid, i) == "done" for i in (1, 2, 3))


def test_high_jumps_queue(env):
    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [1, 2, 3, 4, 5], "normal")
    q.enqueue(bid, [4], "high")
    assert q.snapshot()["pending"][0] == [bid, 4]
    assert drain(q) == [4, 1, 2, 3, 5]


def test_high_keeps_given_order_and_normal_keeps_position(env):
    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [1, 2, 3], "normal")
    q.enqueue(bid, [5, 3], "high")
    assert [p[1] for p in q.snapshot()["pending"]] == [5, 3, 1, 2]
    q.enqueue(bid, [2, 6], "normal")
    assert [p[1] for p in q.snapshot()["pending"]] == [5, 3, 1, 2, 6]


def test_enqueue_open_queues_current_and_next_skipping_matter(env):
    q, store, lib, bid = make_q(env)
    store.set_chapter_matter(bid, 3, True)
    q.enqueue(bid, [7], "normal")
    q.enqueue_open(bid, 2)
    assert [p[1] for p in q.snapshot()["pending"]] == [2, 4, 7]


def test_enqueue_open_skips_done(env):
    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [2], "normal")
    q.run_once()
    q.enqueue_open(bid, 2)
    assert [p[1] for p in q.snapshot()["pending"]] == [3]


def test_batch_next5_skips_matter_and_done(env):
    q, store, lib, bid = make_q(env)
    store.set_chapter_matter(bid, 0, True)
    store.set_chapter_matter(bid, 3, True)
    store.set_chapter_status(bid, 1, "done", duration=1.0, voice="af_heart")
    q.enqueue_batch(bid, "next5")
    assert [p[1] for p in q.snapshot()["pending"]] == [2, 4, 5, 6, 7]


def test_batch_all(env):
    q, store, lib, bid = make_q(env)
    store.set_chapter_matter(bid, 0, True)
    q.enqueue_batch(bid, "all")
    assert [p[1] for p in q.snapshot()["pending"]] == [1, 2, 3, 4, 5, 6, 7]


def test_done_same_voice_ignored_but_reconvert_works(env):
    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [1], "normal")
    q.run_once()
    assert store.list_chapters(bid)[1]["voice"] == "af_heart"
    q.enqueue(bid, [1], "normal")
    assert q.snapshot()["pending"] == []
    store.set_book_voice(bid, "bf_emma")
    q.reconvert(bid, 1)
    assert q.snapshot()["pending"] == [[bid, 1]]
    assert q.run_once() is True
    ch = store.list_chapters(bid)[1]
    assert ch["status"] == "done" and ch["voice"] == "bf_emma"


def test_done_different_voice_still_ignored_by_enqueue(env):
    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [1], "normal")
    q.run_once()
    store.set_book_voice(bid, "bf_emma")
    q.enqueue(bid, [1], "high")
    q.enqueue_open(bid, 1)
    assert [p[1] for p in q.snapshot()["pending"]] == [2]
    assert store.list_chapters(bid)[1]["voice"] == "af_heart"


class ToneSynth(FakeSynth):
    def synth_chapter(self, paragraphs, voice, on_progress=None):
        s, t = super().synth_chapter(paragraphs, voice, on_progress)
        return s + 0.1, t  # non-empty samples


def test_writes_audio_and_tokens(env):
    q, store, lib, bid = make_q(env, ToneSynth())
    q.enqueue(bid, [0], "normal")
    q.run_once()
    assert lib.chapter_audio_path(bid, 0).exists()
    toks = json.loads(lib.chapter_tokens_path(bid, 0).read_text())
    assert toks and toks[0]["w"]
    ch = store.list_chapters(bid)[0]
    assert ch["status"] == "done" and ch["duration"] > 0 and ch["voice"] == "af_heart"
    assert not list(lib.chapter_audio_path(bid, 0).parent.glob("*.tmp*"))


def test_reconvert_overwrites_existing_files(env):
    class Varying(FakeSynth):
        level = 0.1

        def synth_chapter(self, paragraphs, voice, on_progress=None):
            s, t = super().synth_chapter(paragraphs, voice, on_progress)
            Varying.level += 0.4
            return s + Varying.level, t

    q, store, lib, bid = make_q(env, Varying())
    q.enqueue(bid, [0], "normal")
    q.run_once()
    old = lib.chapter_audio_path(bid, 0).read_bytes()
    lib.chapter_tokens_path(bid, 0).write_text("[]")
    q.reconvert(bid, 0)
    q.run_once()
    assert lib.chapter_tokens_path(bid, 0).read_text() != "[]"
    assert lib.chapter_audio_path(bid, 0).read_bytes() != old
    assert status(store, bid, 0) == "done"


def test_empty_chapter_converts_to_zero(env, monkeypatch):
    q, store, lib, bid = make_q(env)
    monkeypatch.setattr(lib, "paragraphs", lambda b, i: [])
    q.enqueue(bid, [2], "normal")
    q.run_once()
    ch = store.list_chapters(bid)[2]
    assert ch["status"] == "done" and ch["duration"] == 0
    assert not lib.chapter_audio_path(bid, 2).exists()
    assert json.loads(lib.chapter_tokens_path(bid, 2).read_text()) == []


def test_synth_error_marks_error_and_continues(env):
    class Flaky(FakeSynth):
        calls = 0

        def synth_chapter(self, paragraphs, voice, on_progress=None):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("boom")
            return super().synth_chapter(paragraphs, voice, on_progress)

    q, store, lib, bid = make_q(env, Flaky())
    q.enqueue(bid, [0, 1], "normal")
    assert q.run_once() is True
    assert q.run_once() is True
    assert status(store, bid, 0) == "error"
    assert status(store, bid, 1) == "done"
    assert q.snapshot()["current"] is None


def test_recover_stuck_converting(env):
    store, lib, bid = env
    store.set_chapter_status(bid, 0, "converting")
    store.set_chapter_status(bid, 1, "queued")
    ConversionQueue(store, lib, FakeSynth(), autostart=False)
    assert status(store, bid, 0) == "none" and status(store, bid, 1) == "none"


def test_run_once_empty_returns_false(env):
    q, *_ = make_q(env)
    assert q.run_once() is False


def test_snapshot_shape_and_progress(env):
    seen = []

    class Spy(FakeSynth):
        def synth_chapter(self, paragraphs, voice, on_progress=None):
            seen.append(q.snapshot())
            return super().synth_chapter(paragraphs, voice, on_progress)

    q, store, lib, bid = make_q(env, Spy())
    assert q.snapshot() == {"current": None, "pending": []}
    q.enqueue(bid, [1, 2], "normal")
    q.run_once()
    assert seen[0]["current"] == {"book_id": bid, "idx": 1, "progress": 0.0}
    assert seen[0]["pending"] == [[bid, 2]]
    assert q.snapshot()["current"] is None


def test_progress_exposed_during_synth(env):
    got = []

    class Spy(FakeSynth):
        def synth_chapter(self, paragraphs, voice, on_progress=None):
            def cb(p):
                on_progress(p)
                got.append(q.snapshot()["current"]["progress"])
            return super().synth_chapter(paragraphs, voice, cb)

    q, store, lib, bid = make_q(env, Spy())
    q.enqueue(bid, [1], "normal")
    q.run_once()
    assert got and got[-1] == 1.0 and got == sorted(got)


def test_converting_chapter_not_requeued(env):
    q, store, lib, bid = make_q(env)

    class Spy(FakeSynth):
        def synth_chapter(self, paragraphs, voice, on_progress=None):
            q.enqueue(bid, [1], "high")
            q.reconvert(bid, 1)
            assert q.snapshot()["pending"] == []
            return super().synth_chapter(paragraphs, voice, on_progress)

    q.synth = Spy()
    q.enqueue(bid, [1], "normal")
    q.run_once()
    assert status(store, bid, 1) == "done"


def test_worker_thread_processes(env):
    store, lib, bid = env
    q = ConversionQueue(store, lib, FakeSynth(), autostart=True)
    try:
        q.enqueue(bid, [1], "normal")
        deadline = time.time() + 3
        while time.time() < deadline and status(store, bid, 1) != "done":
            time.sleep(0.02)
        assert status(store, bid, 1) == "done"
    finally:
        q.stop()
    assert q._thread is None or not q._thread.is_alive()


def test_stop_is_prompt_when_idle(env):
    store, lib, bid = env
    q = ConversionQueue(store, lib, FakeSynth(), autostart=True)
    time.sleep(0.1)
    t0 = time.time()
    q.stop()
    assert time.time() - t0 < 1.0


def test_enqueue_status_read_is_atomic_with_worker(env):
    """Worker finishing a chapter between the status read and the lock must not re-queue it."""
    import threading

    q, store, lib, bid = make_q(env)
    q.enqueue(bid, [1], "normal")
    orig = store.list_chapters
    threads = []
    calls = {"n": 0}

    def spy(book_id):
        res = orig(book_id)
        calls["n"] += 1
        if calls["n"] == 1:
            t = threading.Thread(target=q.run_once)
            t.start()
            t.join(0.5)  # finishes only if the queue lock is free
            threads.append(t)
        return res

    store.list_chapters = spy
    q.enqueue(bid, [1], "high")
    for t in threads:
        t.join(5)
    store.list_chapters = orig
    assert status(store, bid, 1) == "done"
    assert q.snapshot()["pending"] == []


QOS_CLASS_UTILITY = 0x11
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="QoS classes are macOS-only")


def _qos_of(thread_ident: int) -> int:
    """Read the real QoS class of a live thread via pthread_get_qos_class_np."""
    lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
    fn = lib.pthread_get_qos_class_np
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.c_int
    qos, rel = ctypes.c_uint(0), ctypes.c_int(0)
    assert fn(ctypes.c_void_p(thread_ident), ctypes.byref(qos), ctypes.byref(rel)) == 0
    return qos.value


@darwin_only
def test_lower_worker_priority_sets_utility_qos_on_calling_thread():
    seen = {}

    def body():
        seen["before"] = _qos_of(threading.get_ident())
        lower_worker_priority()
        seen["after"] = _qos_of(threading.get_ident())

    t = threading.Thread(target=body)
    t.start()
    t.join(5)
    assert seen["after"] == QOS_CLASS_UTILITY
    assert seen["before"] != QOS_CLASS_UTILITY
    # thread-scoped: the main (API) thread keeps its QoS
    assert _qos_of(threading.get_ident()) != QOS_CLASS_UTILITY


@darwin_only
def test_worker_thread_runs_at_utility_qos(env):
    store, lib, bid = env
    q = ConversionQueue(store, lib, FakeSynth(), autostart=True)
    try:
        q.enqueue(bid, [1], "high")
        deadline = time.time() + 10
        while status(store, bid, 1) != "done" and time.time() < deadline:
            time.sleep(0.02)
        assert status(store, bid, 1) == "done"
        assert _qos_of(q._thread.ident) == QOS_CLASS_UTILITY
    finally:
        q.stop()


def test_lower_worker_priority_falls_back_to_nice_off_macos(monkeypatch):
    import os
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "nice", lambda n: calls.append(n))
    lower_worker_priority()
    assert calls == [10]


def test_lower_worker_priority_logs_failure(monkeypatch, caplog):
    import os

    def boom(n):
        raise OSError("nope")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "nice", boom)
    with caplog.at_level("WARNING", logger="kokoro_reader.queue"):
        lower_worker_priority()  # must not raise
    assert "could not lower worker priority" in caplog.text


def test_worker_survives_broken_torch(env, monkeypatch):
    import sys
    import types
    bad = types.ModuleType("torch")

    def boom(n):
        raise OSError("broken torch")

    bad.set_num_threads = boom
    monkeypatch.setitem(sys.modules, "torch", bad)
    store, lib, bid = env
    q = ConversionQueue(store, lib, FakeSynth(), autostart=True)
    try:
        q.enqueue(bid, [1], "normal")
        deadline = time.time() + 3
        while time.time() < deadline and status(store, bid, 1) != "done":
            time.sleep(0.02)
        assert status(store, bid, 1) == "done"
    finally:
        q.stop()


def test_book_deleted_mid_conversion_writes_nothing(env):
    store, lib, bid = env

    class Deleter(ToneSynth):
        def synth_chapter(self, paragraphs, voice, on_progress=None):
            lib.delete_book(bid)
            return super().synth_chapter(paragraphs, voice, on_progress)

    q = ConversionQueue(store, lib, Deleter(), autostart=False)
    q.enqueue(bid, [1], "normal")
    q.run_once()
    assert not lib.book_dir(bid).exists()


def test_failure_cleans_tmp_files(env, monkeypatch):
    import kokoro_reader.queue as qmod
    q, store, lib, bid = make_q(env, ToneSynth())

    def bad_write(samples, dst):
        dst.write_bytes(b"partial")
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(qmod, "write_mp3", bad_write)
    q.enqueue(bid, [1], "normal")
    q.run_once()
    assert status(store, bid, 1) == "error"
    assert not list(lib.chapter_audio_path(bid, 1).parent.glob("*.tmp"))


def test_book_deleted_while_encoding_leaves_no_orphan_folder(env, monkeypatch):
    import kokoro_reader.queue as qmod
    q, store, lib, bid = make_q(env)
    real = qmod.write_mp3

    def delete_then_write(samples, dst):
        lib.delete_book(bid)  # deleted from the shelf while ffmpeg runs
        return real(samples, dst)

    monkeypatch.setattr(qmod, "write_mp3", delete_then_write)
    q.enqueue(bid, [0], "high")
    q.run_once()
    assert not lib.book_dir(bid).exists()


def test_delete_removes_the_db_row_before_the_files(env, monkeypatch):
    store, lib, bid = env
    seen = []
    import kokoro_reader.library as libmod
    real = libmod.shutil.rmtree
    monkeypatch.setattr(libmod.shutil, "rmtree",
                        lambda *a, **kw: (seen.append(store.get_book(bid)), real(*a, **kw)))
    lib.delete_book(bid)
    assert seen == [None]


class _ProgressSynth(FakeSynth):
    def __init__(self, on_first):
        self.on_first = on_first

    def synth_chapter(self, paragraphs, voice, on_progress=None):
        for k in range(3):
            if k == 1:
                self.on_first()
            if on_progress:
                on_progress(k / 3)
        return super().synth_chapter(paragraphs, voice, on_progress)


def test_stop_cancels_the_chapter_being_synthesised(env):
    store, lib, bid = env
    holder = {}
    synth = _ProgressSynth(lambda: setattr(holder["q"], "_stopping", True))
    q = ConversionQueue(store, lib, synth, autostart=False)
    holder["q"] = q
    q.enqueue(bid, [0], "high")
    q.run_once()
    assert not lib.chapter_audio_path(bid, 0).exists()
    # left converting: the restart's reset_stuck puts it back, it is not an error
    assert store.list_chapters(bid)[0]["status"] == "converting"


def test_deleting_the_book_cancels_its_synthesis(env):
    store, lib, bid = env
    calls = []
    synth = _ProgressSynth(lambda: (calls.append(1), lib.delete_book(bid)))
    q = ConversionQueue(store, lib, synth, autostart=False)
    q.enqueue(bid, [0], "high")
    q.run_once()
    assert calls == [1] and not lib.book_dir(bid).exists()


def test_each_conversion_is_timed_for_the_estimates(env):
    """预计时间 learns from real runs: chars, seconds of work and seconds of audio per chapter."""
    from kokoro_reader.estimate import SpeedLog
    store, lib, bid = env
    ticks = iter([100.0, 130.0])
    speed = SpeedLog(lib.home / "speed.json")
    q = ConversionQueue(store, lib, FakeSynth(), autostart=False, speed=speed, clock=lambda: next(ticks))
    q.enqueue(bid, [0], "normal")
    assert q.run_once()
    ch = store.list_chapters(bid)[0]
    (run,) = json.loads((lib.home / "speed.json").read_text())
    assert run == {"chars": ch["chars"], "seconds": 30.0, "audio": pytest.approx(ch["duration"])}
