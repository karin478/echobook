"""生词本 → Anki: one click turns a saved word/sentence into a note through AnkiConnect (Anki
desktop + the AnkiConnect add-on). The reader picks an existing deck and note type once; we never
create decks or note types, never sync, and never add a duplicate."""

import base64

import pytest
from fastapi.testclient import TestClient

from kokoro_reader import anki
from kokoro_reader.app import create_app
from tests.test_api import BASE, CountingSynth, build_epub, upload


class FakeAnki:
    def __init__(self, fail=None):
        self.calls, self.fail, self.next_id = [], fail, 1000

    def call(self, action, **params):
        self.calls.append((action, params))
        if self.fail == "down":
            raise anki.AnkiError("连不上 Anki")
        if action == "deckNames":
            return ["Default", "用来导入"]
        if action == "modelNames":
            return ["Basic", "问答题"]
        if action == "modelFieldNames":
            return {"Basic": ["Front", "Back"], "问答题": ["正面", "背面"]}[params["modelName"]]
        if action == "storeMediaFile":
            return params["filename"]
        if action == "addNote":
            if self.fail == "duplicate":
                raise anki.AnkiError("cannot create note because it is a duplicate")
            self.next_id += 1
            return self.next_id
        raise AssertionError(action)

    def actions(self):
        return [a for a, _ in self.calls]


@pytest.fixture
def actx(tmp_path):
    def make(fail=None):
        home = tmp_path / f"home-{fail}"
        home.mkdir()
        fake = FakeAnki(fail)
        app = create_app(home, CountingSynth(), start_worker=False, anki_factory=lambda: fake)
        client = TestClient(app, base_url=BASE)
        client.__enter__()
        bid = upload(client, build_epub(tmp_path / f"b-{fail}.epub")).json()["book"]["id"]
        for i in range(4):
            app.state.store.set_chapter_matter(bid, i, False)
        client.post(f"/api/books/{bid}/convert", json={"mode": "one", "chapter": 0})
        app.state.queue.run_once()
        return client, fake, bid
    return make


def save(client, bid, **kw):
    body = {"book_id": bid, "chapter": 0, "para": 0, "start_char": 0, "end_char": 4, **kw}
    return client.post("/api/vocab", json=body).json()


def test_options_and_config_pick_an_existing_deck_and_note_type(actx):
    client, fake, bid = actx()
    opts = client.get("/api/anki/options").json()
    assert opts == {"decks": ["Default", "用来导入"], "models": ["Basic", "问答题"], "config": None}
    assert client.put("/api/anki/config", json={"deck": "Nope", "model": "问答题"}).status_code == 400
    assert client.put("/api/anki/config", json={"deck": "用来导入", "model": "Nope"}).status_code == 400
    r = client.put("/api/anki/config", json={"deck": "用来导入", "model": "问答题"})
    assert r.status_code == 200
    cfg = {"deck": "用来导入", "model": "问答题", "front": "正面", "back": "背面"}
    assert r.json() == cfg
    assert client.get("/api/anki/options").json()["config"] == cfg
    assert "createDeck" not in fake.actions() and "sync" not in fake.actions()


def test_push_a_word_with_its_sentence_chinese_explanation_and_audio(actx):
    client, fake, bid = actx()
    client.put("/api/anki/config", json={"deck": "用来导入", "model": "问答题"})
    rec = save(client, bid, zh="这是<一句>话", gloss="这里的意思\n▸ This\n  这个\n  例：This is it. —— 就是它。",
               gloss_mode="explain")
    r = client.post(f"/api/vocab/{rec['id']}/anki")
    assert r.status_code == 200 and r.json() == {"note_id": 1001, "already": False}

    (_, media), = [c for c in fake.calls if c[0] == "storeMediaFile"]
    assert media["filename"] == f"echobook-{rec['id']}.mp3"
    assert base64.b64decode(media["data"]) == client.get(f"/api/vocab/{rec['id']}/clip.mp3").content
    (_, add), = [c for c in fake.calls if c[0] == "addNote"]
    note = add["note"]
    assert note["deckName"] == "用来导入" and note["modelName"] == "问答题"
    assert note["options"] == {"allowDuplicate": False, "duplicateScope": "deck"}
    assert "echobook" in note["tags"]
    front, back = note["fields"]["正面"], note["fields"]["背面"]
    assert front.startswith("<b>This</b><br>") and "<b>This</b> is an ordinary" in front
    assert front.endswith(f"[sound:echobook-{rec['id']}.mp3]")
    assert back.startswith("这是&lt;一句&gt;话<br><br>这里的意思<br>— <b>This</b><br>")
    assert "例：This is it. —— 就是它。" in back and "Book · " in back

    # pushed once: the record remembers it and a second push adds nothing
    assert client.get("/api/vocab").json()[0]["anki_note_id"] == 1001
    assert client.post(f"/api/vocab/{rec['id']}/anki").json() == {"note_id": 1001, "already": True}
    assert fake.actions().count("addNote") == 1


def test_push_a_whole_sentence(actx):
    client, fake, bid = actx()
    client.put("/api/anki/config", json={"deck": "Default", "model": "Basic"})
    rec = client.post("/api/vocab", json={"book_id": bid, "chapter": 0, "para": 0, "start_char": 0,
                                           "end_char": 59, "whole": True, "zh": "中文"}).json()
    client.post(f"/api/vocab/{rec['id']}/anki")
    (_, add), = [c for c in fake.calls if c[0] == "addNote"]
    assert add["note"]["fields"]["Front"].startswith(rec["sentence"])
    assert add["note"]["fields"]["Back"].startswith("中文<br><br>")


def test_push_errors_are_readable(actx):
    client, fake, bid = actx()
    rec = save(client, bid)
    r = client.post(f"/api/vocab/{rec['id']}/anki")
    assert r.status_code == 400 and "牌组" in r.json()["detail"]  # not configured yet
    assert client.post("/api/vocab/" + "0" * 32 + "/anki").status_code == 404

    client, fake, bid = actx("duplicate")
    client.put("/api/anki/config", json={"deck": "Default", "model": "Basic"})
    r = client.post(f"/api/vocab/{save(client, bid)['id']}/anki")
    assert r.status_code == 409 and "已有" in r.json()["detail"]

    client, fake, bid = actx("down")
    assert client.get("/api/anki/options").status_code == 503
    assert "连不上 Anki" in client.get("/api/anki/options").json()["detail"]


def test_real_client_speaks_ankiconnect_v6(monkeypatch):
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def fake_urlopen(req, timeout):
        import json
        sent.append((req.full_url, json.loads(req.data)))
        return Resp(b'{"result": ["Default"], "error": null}' if len(sent) == 1
                    else b'{"result": null, "error": "deck was not found"}')

    monkeypatch.setattr(anki.urllib.request, "urlopen", fake_urlopen)
    c = anki.AnkiConnect("http://127.0.0.1:8765")
    assert c.call("deckNames") == ["Default"]
    assert sent[0] == ("http://127.0.0.1:8765", {"action": "deckNames", "version": 6, "params": {}})
    with pytest.raises(anki.AnkiError, match="deck was not found"):
        c.call("addNote", note={})


def test_tests_never_reach_the_real_anki(monkeypatch):
    assert anki.default_url() != "http://127.0.0.1:8765"  # conftest points it at a dead port
