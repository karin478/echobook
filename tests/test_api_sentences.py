import threading
import json
import time

import pytest
from fastapi.testclient import TestClient

from kokoro_reader.app import create_app
from kokoro_reader.translate import FakeTranslator
from tests.test_api import BASE, upload
from tests.epub_factory import make_epub
from kokoro_reader.tts import FakeSynth

PARAS = [
    "He knocked twice. Nobody answered from the house.",
    "She waited on the step.",
]


class FailingTranslator(FakeTranslator):
    def translate(self, paras, title, glossary=None):
        raise RuntimeError("network down")


def _client(tmp_path, factory):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    app = create_app(home, FakeSynth(), start_worker=False, translator_factory=factory)
    return app, TestClient(app, base_url=BASE)


def _book(client, tmp_path):
    docs = [("c1.xhtml", "<h1>One</h1>" + "".join(f"<p>{p}</p>" for p in PARAS))]
    epub = make_epub(tmp_path / "s.epub", title="Quiet", author="A", docs=docs, toc=[("One", "c1.xhtml")])
    return upload(client, epub).json()["book"]["id"]


def _wait(client, url, status, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(url).json()
        if d["status"] == status:
            return d
        time.sleep(0.05)
    raise AssertionError(f"never reached {status}: {d}")


@pytest.fixture
def env(tmp_path):
    app, client = _client(tmp_path, FakeTranslator)
    with client:
        bid = _book(client, tmp_path)
        yield app, client, bid, tmp_path


def test_sentences_available_before_translation(env):
    app, client, bid, _ = env
    d = client.get(f"/api/books/{bid}/chapters/0/sentences").json()
    assert d["status"] == "none" and d["progress"] == 0
    shown = client.get(f"/api/books/{bid}/chapters/0").json()["paragraphs"]
    p0 = d["paragraphs"][0]
    assert [shown[0][a:b] for a, b in p0["spans"]] == ["He knocked twice.", "Nobody answered from the house."]
    assert p0["segs"] is None


def test_translate_then_aligned_segments(env):
    app, client, bid, _ = env
    r = client.post(f"/api/books/{bid}/chapters/0/translate")
    assert r.status_code == 202
    d = _wait(client, f"/api/books/{bid}/chapters/0/sentences", "done")
    assert d["progress"] == 1
    p0, p1 = d["paragraphs"]
    assert p0["segs"] == [{"from": 0, "to": 0, "zh": "【译】He knocked twice."},
                          {"from": 1, "to": 1, "zh": "【译】Nobody answered from the house."}]
    assert len(p1["spans"]) == 1 and p1["segs"][0]["zh"] == "【译】She waited on the step."
    # done is sticky: a second POST doesn't start another job
    assert client.post(f"/api/books/{bid}/chapters/0/translate").json()["status"] == "done"


def test_mismatched_sentences_fall_back_to_whole_paragraph(env):
    app, client, bid, _ = env
    path = app.state.library.translation_path(bid, 0)
    path.parent.mkdir(exist_ok=True)
    stored = {"model": "x", "paragraphs": [
        {"sents": ["Different.", "Sentences."], "zh": "整段译文。",
         "segs": [{"from": 0, "to": 0, "zh": "整段"}, {"from": 1, "to": 1, "zh": "译文。"}]},
        {"sents": ["She waited on the step."], "zh": "她等着。", "segs": [{"from": 0, "to": 0, "zh": "她等着。"}]},
    ]}
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    d = client.get(f"/api/books/{bid}/chapters/0/sentences").json()
    assert d["status"] == "done"
    shown = client.get(f"/api/books/{bid}/chapters/0").json()["paragraphs"]
    p0 = d["paragraphs"][0]
    assert p0["spans"] == [[0, len(shown[0])]]
    assert p0["segs"] == [{"from": 0, "to": 0, "zh": "整段译文。"}]
    assert d["paragraphs"][1]["segs"] == [{"from": 0, "to": 0, "zh": "她等着。"}]


def test_translation_failure_reports_error_and_can_retry(tmp_path):
    app, client = _client(tmp_path, FailingTranslator)
    with client:
        bid = _book(client, tmp_path)
        client.post(f"/api/books/{bid}/chapters/0/translate")
        d = _wait(client, f"/api/books/{bid}/chapters/0/sentences", "error")
        assert "network down" in d["error"]
        assert d["paragraphs"][0]["segs"] is None
        r = client.post(f"/api/books/{bid}/chapters/0/translate")
        assert r.json()["status"] == "queued"


def test_translator_factory_failure_is_an_error_not_500(tmp_path):
    def no_key():
        raise RuntimeError("没有找到 OPENROUTER_API_KEY")

    app, client = _client(tmp_path, no_key)
    with client:
        bid = _book(client, tmp_path)
        assert client.post(f"/api/books/{bid}/chapters/0/translate").status_code == 202
        d = _wait(client, f"/api/books/{bid}/chapters/0/sentences", "error")
        assert "OPENROUTER_API_KEY" in d["error"]


def test_unknown_chapter_is_404(env):
    app, client, bid, _ = env
    assert client.get(f"/api/books/{bid}/chapters/9/sentences").status_code == 404
    assert client.post(f"/api/books/{bid}/chapters/9/translate").status_code == 404
    assert client.get(f"/api/books/{'0' * 32}/chapters/0/sentences").status_code == 404


# ---------------------------------------------------------------- 整本书一键准备 (audio + Chinese)
def _book_n(client, tmp_path, n=3):
    docs = [(f"c{i}.xhtml", f"<h1>Chapter {i}</h1><p>{PARAS[0]}</p><p>{PARAS[1]}</p>") for i in range(n)]
    toc = [(f"Chapter {i}", f"c{i}.xhtml") for i in range(n)]
    epub = make_epub(tmp_path / "p.epub", title="Prep", author="A", docs=docs, toc=toc)
    return upload(client, epub).json()["book"]["id"]


def test_prepare_queues_audio_and_translates_every_story_chapter(tmp_path):
    app, client = _client(tmp_path, FakeTranslator)
    with client:
        bid = _book_n(client, tmp_path)
        store = app.state.store
        for i in range(3):
            store.set_chapter_matter(bid, i, i == 2)  # chapter 2 is front/back matter
        r = client.post(f"/api/books/{bid}/prepare")
        assert r.status_code == 202
        assert r.json() == {"audio": 2, "zh": 2}
        q = client.get("/api/queue").json()
        assert sorted(i for b, i in q["pending"] if b == bid) == [0, 1]
        for i in (0, 1):
            _wait(client, f"/api/books/{bid}/chapters/{i}/sentences", "done")
        assert client.get(f"/api/books/{bid}/chapters/2/sentences").json()["status"] == "none"
        info = client.get(f"/api/books/{bid}").json()
        assert info["translated"] == 2
        zh = {c["idx"]: c["zh"] for c in info["chapters"]}
        assert zh[0] == "done" and zh[1] == "done" and zh[2] == "none"
        # nothing left to do: a second prepare queues nothing new
        assert client.post(f"/api/books/{bid}/prepare").json() == {"audio": 0, "zh": 0}


def test_prepare_unknown_book_404(env):
    app, client, bid, _ = env
    assert client.post(f"/api/books/{'0' * 32}/prepare").status_code == 404


def test_book_list_reports_translated_count(env):
    app, client, bid, _ = env
    assert client.get("/api/books").json()[0]["translated"] == 0


class GlossaryCounting(FakeTranslator):
    glossary_calls = 0
    seen = []

    def glossary(self, rows):
        type(self).glossary_calls += 1
        return "Mara => Mara\nHollow => 空谷"

    def translate(self, paras, title, glossary=None):
        type(self).seen.append(glossary)
        return super().translate(paras, title, glossary)


def _two_chapter_book(client, tmp_path):
    para = "Mara walked to the Hollow. Mara stopped at the Hollow gate. Then Mara and the Hollow were quiet."
    docs = [(f"c{i}.xhtml", f"<h1>Ch {i}</h1><p>{para}</p><p>Plain words here.</p>") for i in (1, 2)]
    epub = make_epub(tmp_path / "g.epub", title="Glo", author="A", docs=docs,
                     toc=[("Ch 1", "c1.xhtml"), ("Ch 2", "c2.xhtml")])
    bid = upload(client, epub).json()["book"]["id"]
    for c in client.get(f"/api/books/{bid}").json()["chapters"]:
        client.app.state.store.set_chapter_matter(bid, c["idx"], False)
    return bid


def test_book_glossary_built_once_saved_and_used(tmp_path):
    GlossaryCounting.glossary_calls, GlossaryCounting.seen = 0, []
    app, client = _client(tmp_path, GlossaryCounting)
    with client:
        bid = _two_chapter_book(client, tmp_path)
        assert client.post(f"/api/books/{bid}/prepare").json()["zh"] == 2
        for idx in (0, 1):
            _wait(client, f"/api/books/{bid}/chapters/{idx}/sentences", "done")
        assert GlossaryCounting.glossary_calls == 1
        saved = json.loads(app.state.library.glossary_path(bid).read_text(encoding="utf-8"))
        assert saved["entries"] == [{"source": "Mara", "zh": "Mara"}, {"source": "Hollow", "zh": "空谷"}]
        used = [g for g in GlossaryCounting.seen if g]
        assert used and all({"source": "Hollow", "zh": "空谷"} in g for g in used)
        assert client.get(f"/api/books/{bid}").json()["glossary"] == 2


# ---- one trigger: preparing a chapter = its audio + its Chinese ----
def test_preparing_one_chapter_queues_audio_and_translates_it(tmp_path):
    app, client = _client(tmp_path, FakeTranslator)
    with client:
        bid = _book_n(client, tmp_path)
        client.post(f"/api/books/{bid}/convert", json={"mode": "one", "chapter": 1})
        assert [bid, 1] in client.get("/api/queue").json()["pending"]
        _wait(client, f"/api/books/{bid}/chapters/1/sentences", "done")
        assert client.get(f"/api/books/{bid}/chapters/0/sentences").json()["status"] == "none"


def test_opening_a_chapter_prepares_it_and_the_next_one(tmp_path):
    app, client = _client(tmp_path, FakeTranslator)
    with client:
        bid = _book_n(client, tmp_path)
        client.post(f"/api/books/{bid}/convert", json={"mode": "open", "chapter": 0})
        for i in (0, 1):
            _wait(client, f"/api/books/{bid}/chapters/{i}/sentences", "done")
        assert client.get(f"/api/books/{bid}/chapters/2/sentences").json()["status"] == "none"


def test_new_voice_reconvert_does_not_retranslate(tmp_path):
    app, client = _client(tmp_path, FakeTranslator)
    with client:
        bid = _book_n(client, tmp_path)
        client.post(f"/api/books/{bid}/convert", json={"mode": "reconvert", "chapter": 0})
        assert client.get(f"/api/books/{bid}/chapters/0/sentences").json()["status"] == "none"


class GatedGlossary(FakeTranslator):
    """glossary() blocks until released, so other chapters provably ask while it is building."""

    def __init__(self, fail=False):
        self.fail, self.calls, self.seen = fail, 0, []
        self.entered, self.release = threading.Event(), threading.Event()

    def glossary(self, rows):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        if self.fail:
            raise RuntimeError("glossary down")
        return "Mara => Mara\nHollow => 空谷"

    def translate(self, paras, title, glossary=None):
        self.seen.append(glossary)
        return super().translate(paras, title, glossary)


def _all_running(client, bid, idxs, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if all(client.get(f"/api/books/{bid}/chapters/{i}/sentences").json()["status"] == "running"
               for i in idxs):
            return
        time.sleep(0.02)
    raise AssertionError("chapters never all running")


def _three_chapter_book(client, tmp_path):
    para = "Mara walked to the Hollow. Mara stopped at the Hollow gate. Then Mara and the Hollow were quiet."
    docs = [(f"c{i}.xhtml", f"<h1>Ch {i}</h1><p>{para}</p>") for i in (1, 2, 3)]
    epub = make_epub(tmp_path / "g3.epub", title="Glo3", author="A", docs=docs,
                     toc=[(f"Ch {i}", f"c{i}.xhtml") for i in (1, 2, 3)])
    bid = upload(client, epub).json()["book"]["id"]
    for c in client.get(f"/api/books/{bid}").json()["chapters"]:
        client.app.state.store.set_chapter_matter(bid, c["idx"], False)
    return bid


def test_chapters_started_together_share_one_glossary_build(tmp_path):
    t = GatedGlossary()
    app, client = _client(tmp_path, lambda: t)
    with client:
        bid = _three_chapter_book(client, tmp_path)
        for i in range(3):  # three 准备 clicks in a row
            client.post(f"/api/books/{bid}/convert", json={"mode": "one", "chapter": i})
        assert t.entered.wait(10)
        _all_running(client, bid, range(3))  # the other two are waiting on the same build
        t.release.set()
        for i in range(3):
            _wait(client, f"/api/books/{bid}/chapters/{i}/sentences", "done")
        assert t.calls == 1
        assert len(t.seen) == 3
        assert all(g and {"source": "Hollow", "zh": "空谷"} in g for g in t.seen)


def test_glossary_failure_fails_the_chapters_waiting_on_it_and_retries_later(tmp_path):
    t = GatedGlossary(fail=True)
    app, client = _client(tmp_path, lambda: t)
    with client:
        bid = _three_chapter_book(client, tmp_path)
        for i in (0, 1):
            client.post(f"/api/books/{bid}/convert", json={"mode": "one", "chapter": i})
        assert t.entered.wait(10)
        _all_running(client, bid, (0, 1))
        t.release.set()
        for i in (0, 1):
            d = _wait(client, f"/api/books/{bid}/chapters/{i}/sentences", "error")
            assert "术语表" in d["error"]
        assert t.calls == 1        # the waiting chapter shared the failed build
        assert t.seen == []        # never translated without the glossary
        assert not app.state.library.glossary_path(bid).exists()
        client.post(f"/api/books/{bid}/chapters/0/translate")  # 重试 builds again
        _wait(client, f"/api/books/{bid}/chapters/0/sentences", "error")
        assert t.calls == 2


def test_opening_a_chapter_translates_it_before_the_next_one(tmp_path):
    gate, order = threading.Event(), []

    class Gated(FakeTranslator):
        def translate(self, paras, title, glossary=None):
            order.append(paras[0])
            gate.wait(10)
            return super().translate(paras, title, glossary)

    home = tmp_path / "home"
    home.mkdir()
    app = create_app(home, FakeSynth(), start_worker=False, translator_factory=Gated, translate_workers=1)
    client = TestClient(app, base_url=BASE)
    with client:
        docs = [(f"c{i}.xhtml", f"<h1>Chapter {i}</h1><p>Text of chapter {i}.</p>") for i in range(5)]
        epub = make_epub(tmp_path / "o.epub", title="Order", author="A", docs=docs,
                         toc=[(f"Chapter {i}", f"c{i}.xhtml") for i in range(5)])
        bid = upload(client, epub).json()["book"]["id"]
        for i in range(5):
            app.state.store.set_chapter_matter(bid, i, False)
        client.post(f"/api/books/{bid}/prepare")  # 0 runs, 1-4 wait
        end = time.time() + 10
        while not order and time.time() < end:
            time.sleep(0.01)
        client.post(f"/api/books/{bid}/convert", json={"mode": "open", "chapter": 3})
        gate.set()
        for i in range(5):
            _wait(client, f"/api/books/{bid}/chapters/{i}/sentences", "done")
        assert [o.split()[-1] for o in order] == ["0.", "3.", "4.", "1.", "2."]
