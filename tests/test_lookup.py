"""划词: the reader selects a fragment and gets either a quick Chinese rendering (翻译) or a short
explanation of what it means in this sentence (讲解); saving keeps that result with the word."""

import pytest
from fastapi.testclient import TestClient

from kokoro_reader import translate as tr
from kokoro_reader.app import create_app
from tests.test_api import BASE, CountingSynth, build_epub, upload

SENT = "This is an ordinary sentence of the story that keeps going."


class CountingFake(tr.FakeTranslator):
    def __init__(self):
        self.glosses = []

    def gloss(self, mode, text, sentence, context, title):
        self.glosses.append((mode, text, sentence, context, title))
        return super().gloss(mode, text, sentence, context, title)


@pytest.fixture
def lctx(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = CountingFake()
    app = create_app(home, CountingSynth(), start_worker=False, translator_factory=lambda: fake)
    with TestClient(app, base_url=BASE) as client:
        bid = upload(client, build_epub(tmp_path / "b.epub")).json()["book"]["id"]
        yield client, fake, bid


def body(bid, start, end, mode, **kw):
    return {"book_id": bid, "chapter": 0, "para": 0, "start_char": start, "end_char": end,
            "mode": mode, **kw}


def test_lookup_quick_and_explain_use_the_sentence_and_are_cached(lctx):
    client, fake, bid = lctx
    start = SENT.index("ordinary sentence")
    end = start + len("ordinary sentence")
    r = client.post("/api/lookup", json=body(bid, start, end, "quick"))
    assert r.status_code == 200
    assert r.json() == {"mode": "quick", "text": "ordinary sentence", "start_char": start, "end_char": end,
                        "result": "【翻译】ordinary sentence", "source": "model"}  # no machine translation under the fake
    mode, text, sentence, context, title = fake.glosses[0]
    assert (mode, text, sentence, title) == ("quick", "ordinary sentence", SENT, "Book")
    assert SENT in context
    ex = client.post("/api/lookup", json=body(bid, start, end, "explain")).json()
    assert ex["mode"] == "explain" and ex["result"] == f"【讲解】ordinary sentence｜{SENT}"
    # asking again costs nothing
    client.post("/api/lookup", json=body(bid, start, end, "quick"))
    assert len(fake.glosses) == 2


def test_lookup_rejects_bad_input(lctx):
    client, fake, bid = lctx
    assert client.post("/api/lookup", json=body(bid, 0, 4, "poem")).status_code == 422
    assert client.post("/api/lookup", json=body(bid, 5, 5, "quick")).status_code == 400
    assert client.post("/api/lookup", json=body("0" * 32, 0, 4, "quick")).status_code == 400
    assert client.post("/api/lookup", json={**body(bid, 0, 4, "quick"), "chapter": 99}).status_code == 404
    assert fake.glosses == []


def test_lookup_model_failure_is_a_readable_error(tmp_path):
    class Broken(tr.FakeTranslator):
        def gloss(self, *a):
            raise tr.TranslateError("OpenRouter request failed: HTTP 500")

    home = tmp_path / "home"
    home.mkdir()
    app = create_app(home, CountingSynth(), start_worker=False, translator_factory=Broken)
    with TestClient(app, base_url=BASE) as client:
        bid = upload(client, build_epub(tmp_path / "b.epub")).json()["book"]["id"]
        r = client.post("/api/lookup", json=body(bid, 0, 4, "quick"))
        assert r.status_code == 502
        assert "HTTP 500" in r.json()["detail"]


def test_saving_a_selection_keeps_its_gloss(lctx):
    client, fake, bid = lctx
    rec = client.post("/api/vocab", json={k: v for k, v in body(bid, 0, 4, None).items() if k != "mode"}
                      | {"gloss": "这", "gloss_mode": "quick"}).json()
    assert (rec["gloss"], rec["gloss_mode"]) == ("这", "quick")
    plain = client.post("/api/vocab", json={k: v for k, v in body(bid, 0, 4, None).items() if k != "mode"}).json()
    assert plain["gloss"] is None and plain["gloss_mode"] is None



def test_saving_one_unit_of_an_explanation(lctx):
    """讲解逐条收藏: the ☆ on one ▸ block sends that block and its head; the entry is that chunk."""
    client, fake, bid = lctx
    block = "▸ keeps going (keep doing)\n一直继续。"
    rec = client.post("/api/vocab", json={"book_id": bid, "chapter": 0, "para": 0, "start_char": 0,
                                           "end_char": len(SENT), "gloss": block, "gloss_mode": "explain",
                                           "unit": "keeps going (keep doing)"}).json()
    assert rec["text"] == "keeps going" and SENT[rec["start_char"]:rec["end_char"]] == "keeps going"
    assert rec["gloss"] == block

def test_real_client_prompts(monkeypatch):
    seen = []

    def fake_chat(self, messages, max_tokens):
        seen.append((messages, max_tokens))
        return "  惹他心烦 \n"

    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", fake_chat)
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    sent = "Nothing could get under his skin."
    assert t.gloss("quick", "get under his skin", sent, sent, "Whistler") == "惹他心烦"
    assert t.gloss("explain", "get under his skin", sent, sent, "Whistler") == "惹他心烦"
    (q, q_max), (e, e_max) = seen
    for msgs in (q, e):
        user = msgs[-1]["content"]
        assert "get under his skin" in user and sent in user and "Whistler" in user
    assert q[0]["content"] != e[0]["content"]  # different instructions per mode
    assert q_max < e_max
    with pytest.raises(ValueError):
        t.gloss("poem", "x", sent, sent, "")


def test_explain_prompt_breaks_the_fragment_into_units():
    """讲解 is a teacher's breakdown: one line of meaning, then one ▸ block per smallest unit
    (phrase / collocation / pattern) with an 例：, and an optional 注意： — the reader page
    lays the reply out by these markers, so they are part of the contract."""
    p = tr.GLOSS_SYSTEM["explain"]
    for marker in ("▸", "例：", "注意：", "最小的意义单位", "最多 5 个词", "示例"):
        assert marker in p
    assert tr.GLOSS_MAX_TOKENS["explain"] >= 1200  # room for several units


def test_lookup_grows_the_selection_and_says_to_what(lctx):
    """智能扩选 happens before the lookup: the reply carries the grown range, so the page can
    re-highlight it and ⭐ saves the whole word / phrase."""
    client, fake, bid = lctx
    start = SENT.index("rdinar")
    r = client.post("/api/lookup", json=body(bid, start, start + len("rdinar"), "quick")).json()
    assert r["text"] == "ordinary"
    assert (r["start_char"], r["end_char"]) == (SENT.index("ordinary"), SENT.index("ordinary") + len("ordinary"))
    assert fake.glosses[-1][1] == "ordinary"
