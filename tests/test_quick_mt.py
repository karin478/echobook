"""划词「翻译」: free machine translation first (Google's dict-chrome-ex endpoint, then MyMemory) —
fast and no tokens; the model is only the last resort. A failing channel cools down for a while."""

import pytest
from fastapi.testclient import TestClient

from kokoro_reader import quick_mt as mt
from kokoro_reader import translate as tr
from kokoro_reader.app import create_app
from tests.test_api import BASE, CountingSynth, build_epub, upload
from tests.test_lookup import CountingFake, body


def test_google_reads_both_reply_shapes(monkeypatch):
    seen = []

    def fake_get(url, timeout):
        seen.append(url)
        return replies.pop(0)

    replies = [["结果"], [["原来", "en"]]]
    monkeypatch.setattr(mt, "_get_json", fake_get)
    assert mt.google("turned out") == "结果"
    assert mt.google("turned out") == "原来"
    assert seen[0].startswith("https://clients5.google.com/translate_a/t?")
    assert "client=dict-chrome-ex" in seen[0] and "q=turned+out" in seen[0] and "tl=zh-CN" in seen[0]


def test_mymemory_refuses_a_spent_quota(monkeypatch):
    monkeypatch.setattr(mt, "_get_json", lambda url, timeout: {
        "responseStatus": 200, "quotaFinished": False, "responseData": {"translatedText": "原来"}})
    assert mt.mymemory("turned out") == "原来"
    monkeypatch.setattr(mt, "_get_json", lambda url, timeout: {
        "responseStatus": 429, "quotaFinished": True, "responseDetails": "quota", "responseData": {}})
    with pytest.raises(mt.MTError):
        mt.mymemory("turned out")


def test_chain_falls_through_and_cools_a_failing_channel():
    now = [1000.0]
    calls = []

    def google(text):
        calls.append("google")
        raise ValueError("HTML sorry page")  # what a 429 looks like to json.loads

    def mymemory(text):
        calls.append("mymemory")
        return "原来"

    chain = mt.QuickMT([("google", google), ("mymemory", mymemory)], clock=lambda: now[0])
    assert chain.translate(" turned out ") == ("原来", "mymemory")
    assert chain.translate("turned out") == ("原来", "mymemory")
    assert calls == ["google", "mymemory", "mymemory"]  # google is cooling down
    now[0] += mt.COOLDOWN + 1
    chain.translate("turned out")
    assert calls[-2:] == ["google", "mymemory"]  # tried again after the cooldown


def test_chain_raises_when_every_channel_fails():
    def bad(text):
        raise OSError("offline")

    chain = mt.QuickMT([("google", bad), ("mymemory", bad)])
    with pytest.raises(mt.MTError, match="offline"):
        chain.translate("x")


def test_no_machine_translation_under_the_fake_translator():
    assert mt.make_mt() is None  # tests (conftest sets the fake) never reach the network


class StubMT:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def translate(self, text):
        self.calls.append(text)
        if self.fail:
            raise mt.MTError("google: 429; mymemory: quota")
        return "很普通的句子", "google"


@pytest.mark.parametrize("fail", [False, True])
def test_lookup_quick_uses_machine_translation_and_falls_back_to_the_model(tmp_path, fail):
    home = tmp_path / "home"
    home.mkdir()
    fake, stub = CountingFake(), StubMT(fail)
    app = create_app(home, CountingSynth(), start_worker=False, translator_factory=lambda: fake,
                     mt_factory=lambda: stub)
    with TestClient(app, base_url=BASE) as client:
        bid = upload(client, build_epub(tmp_path / "b.epub")).json()["book"]["id"]
        sent = "This is an ordinary sentence of the story that keeps going."
        start = sent.index("ordinary sentence")
        r = client.post("/api/lookup", json=body(bid, start, start + len("ordinary sentence"), "quick")).json()
        assert stub.calls == ["ordinary sentence"]
        if fail:
            assert r["result"] == "【翻译】ordinary sentence" and r["source"] == "model"
            assert [g[0] for g in fake.glosses] == ["quick"]
        else:
            assert r["result"] == "很普通的句子" and r["source"] == "google"
            assert fake.glosses == []  # no tokens spent
        ex = client.post("/api/lookup", json=body(bid, start, start + len("ordinary sentence"), "explain")).json()
        assert ex["source"] == "model" and stub.calls == ["ordinary sentence"]  # 讲解 is always the model
