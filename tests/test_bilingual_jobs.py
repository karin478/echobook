"""TranslationJobs ordering: the chapter the reader is about to read jumps the translation queue."""

import threading
import time

from kokoro_reader.bilingual import TranslationJobs
from kokoro_reader.library import Library
from kokoro_reader.store import Store
from kokoro_reader.translate import FakeTranslator


def test_high_priority_chapter_jumps_the_translation_queue(tmp_path):
    gate, order = threading.Event(), []

    class Gated(FakeTranslator):
        def translate(self, paras, title, glossary=None):
            order.append(paras[0])
            gate.wait(5)
            return super().translate(paras, title, glossary)

    home = tmp_path / "home"
    home.mkdir()
    lib = Library(home, Store(home / "kokoro.db"))
    (home / "books" / "b").mkdir(parents=True)
    jobs = TranslationJobs(lib, Gated, max_chapters=1)
    try:
        jobs.start("b", 0, ["A."], "T")            # running, holds the only worker
        end = time.time() + 5
        while not order and time.time() < end:
            time.sleep(0.01)
        assert order
        for i, text in ((1, "B."), (2, "C."), (3, "D.")):
            jobs.start("b", i, [text], "T")         # 准备整本书 order
        jobs.start("b", 4, ["E."], "T", priority="high")  # the chapter just clicked
        jobs.start("b", 2, ["C."], "T", priority="high")  # already queued: moves up too
        gate.set()
        end = time.time() + 5
        while len(order) < 5 and time.time() < end:
            time.sleep(0.01)
        assert order == ["A.", "C.", "E.", "B.", "D."]
    finally:
        gate.set()
        jobs.shutdown()
