"""生词本 → Anki in the browser, against a stand-in AnkiConnect (never the real Anki)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_vocab import seeded  # noqa: F401

pytestmark = pytest.mark.e2e


class FakeAnkiConnect:
    def __init__(self):
        self.calls, self.down = [], False
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(req)
                body = json.dumps(fake.answer(req["action"], req.get("params", {}))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, action, params):
        if action == "deckNames":
            return {"result": ["Default", "用来导入"], "error": None}
        if action == "modelNames":
            return {"result": ["Basic", "问答题"], "error": None}
        if action == "modelFieldNames":
            return {"result": ["正面", "背面"], "error": None}
        if action == "storeMediaFile":
            return {"result": params["filename"], "error": None}
        if action == "addNote":
            return {"result": 5000 + len(self.notes()), "error": None}
        return {"result": None, "error": f"unexpected {action}"}

    def notes(self):
        return [c["params"]["note"] for c in self.calls if c["action"] == "addNote"]


@pytest.fixture
def fake_anki():
    f = FakeAnkiConnect()
    yield f
    f.server.shutdown()


@pytest.fixture
def live_server_env(fake_anki):
    return {"KOKORO_ANKI_URL": fake_anki.url}


def test_pick_deck_once_then_push_one_and_the_rest(page: Page, live_server, seeded, fake_anki):
    book, ids = seeded
    page.goto(f"{live_server.url}/vocab.html")
    bar = page.locator("#anki-bar")
    # first time: choose an existing deck and note type
    expect(bar.get_by_label("牌组")).to_be_visible()
    bar.get_by_label("牌组").select_option("用来导入")
    bar.get_by_label("卡片类型").select_option("问答题")
    bar.get_by_role("button", name="保存").click()
    expect(bar).to_contain_text("用来导入 · 问答题")

    items = page.locator(".vocab-item")
    oldest = items.nth(2)
    oldest.get_by_role("button", name="推到 Anki").click()
    expect(oldest.locator(".anki-done")).to_have_text("已在 Anki")  # a status, not a dead button
    expect(oldest.get_by_role("button", name="推到 Anki")).to_have_count(0)
    (note,) = fake_anki.notes()
    assert note["deckName"] == "用来导入" and note["fields"]["正面"].startswith("<b>quiet</b>")

    bar.get_by_role("button", name="全部推到 Anki（2 条）").click()
    expect(page.locator(".vocab-item .anki-done")).to_have_count(3)
    assert len(fake_anki.notes()) == 3
    expect(bar.get_by_role("button", name="全部推到 Anki（0 条）")).to_be_disabled()

    # remembered across reloads, on both sides
    page.reload()
    expect(bar).to_contain_text("用来导入 · 问答题")
    expect(page.locator(".vocab-item .anki-done")).to_have_count(3)
    assert all(r.get("anki_note_id") for r in live_server.api("GET", "/api/vocab"))


@pytest.mark.parametrize("live_server_env", [{"KOKORO_ANKI_URL": "http://127.0.0.1:9"}])
def test_anki_closed_says_so_and_the_list_still_works(page: Page, live_server, seeded):
    page.goto(f"{live_server.url}/vocab.html")
    expect(page.locator("#anki-bar")).to_contain_text("连不上 Anki")
    expect(page.locator(".vocab-item")).to_have_count(3)
    expect(page.locator(".vocab-item").first.get_by_role("button", name="推到 Anki")).to_be_disabled()
