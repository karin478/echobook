import time

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def seeded(live_server, sample_epub):
    book = live_server.upload(sample_epub)["id"]
    live_server.api("POST", f"/api/books/{book}/convert", {"mode": "one", "chapter": 1})
    live_server.wait_chapter(book, 1, "done", timeout=30)
    paras = live_server.api("GET", f"/api/books/{book}/chapters/1")["paragraphs"]
    ids = []
    for para, word in [(0, "quiet"), (1, "clocks"), (1, "Winston Smith")]:
        text = paras[para]
        s = text.index(word)
        rec = live_server.api("POST", "/api/vocab", {
            "book_id": book, "chapter": 1, "para": para, "start_char": s, "end_char": s + len(word)})
        ids.append(rec["id"])
    return book, ids


def test_vocab_page_lists_and_deletes(page: Page, live_server, seeded):
    book, ids = seeded
    page.goto(f"{live_server.url}/vocab.html")
    expect(page.get_by_role("link", name="← 书架")).to_have_attribute("href", "shelf.html")
    expect(page.locator("#count")).to_contain_text("3")
    expect(page.locator("#anki-bar")).to_be_visible()
    items = page.locator(".vocab-item")
    expect(items).to_have_count(3)
    # newest first
    expect(items.nth(0).locator(".word")).to_have_text("Winston Smith")
    expect(items.nth(2).locator(".word")).to_have_text("quiet")
    first = items.nth(2)
    expect(first.locator("mark")).to_have_text("quiet")
    expect(first.locator(".sentence")).to_contain_text("The first chapter opens on a quiet street.")
    expect(first.locator(".src")).to_contain_text("The Sample Book")
    # story numbering, same as the book page (the Copyright page at idx 0 is not counted)
    expect(first.locator(".src")).to_have_text("The Sample Book · Ch 1 · Chapter 1")
    created = next(r for r in live_server.api("GET", "/api/vocab") if r["id"] == ids[0])["created_at"]
    expect(first.locator(".when")).to_have_text(time.strftime("%Y-%m-%d %H:%M", time.localtime(created)))
    expect(first.get_by_role("button", name="播放句子")).to_be_visible()

    page.once("dialog", lambda d: d.dismiss())
    first.get_by_role("button", name="删除").click()
    expect(items).to_have_count(3)

    page.once("dialog", lambda d: d.accept())
    first.get_by_role("button", name="删除").click()
    expect(items).to_have_count(2)
    expect(page.locator("#count")).to_contain_text("2")
    assert [r["id"] for r in live_server.api("GET", "/api/vocab")] == [ids[2], ids[1]]

    for _ in range(2):
        page.once("dialog", lambda d: d.accept())
        items.first.get_by_role("button", name="删除").click()
    expect(page.locator(".empty-note")).to_have_text("还没有收藏。在阅读页划选文字，在弹出的翻译/讲解里点 ⭐ 收藏，或按 F / 点中文那行末尾的 ☆ 收藏整句（连中文一起）。")


def test_vocab_page_plays_clip(page: Page, live_server, seeded):
    _, ids = seeded
    page.goto(f"{live_server.url}/vocab.html")
    target = page.locator(".vocab-item").nth(2)
    with page.expect_response(lambda r: r.url.endswith(f"/api/vocab/{ids[0]}/clip.mp3")) as info:
        target.get_by_role("button", name="播放句子").click()
    assert info.value.status in (200, 206)
    src = page.evaluate("document.querySelector('audio').getAttribute('src')")
    assert src == f"/api/vocab/{ids[0]}/clip.mp3"
    # a record without a clip shows no play button
    page.route("**/api/vocab", lambda route: route.fulfill(
        json=[{**r, "sentence_clip": None} for r in live_server.api("GET", "/api/vocab")]))
    page.reload()
    expect(page.locator(".vocab-item")).to_have_count(3)
    expect(page.get_by_role("button", name="播放句子")).to_have_count(0)


def test_vocab_page_falls_back_for_records_without_story_number(page: Page, live_server):
    from kokoro_reader.store import Store
    Store(live_server.home / "kokoro.db").add_vocab({
        "text": "old", "sentence": "An old <record>.", "book_id": None, "book_title": "Legacy",
        "chapter": 3, "para": 0, "sentence_clip": None, "status": "new"})
    page.goto(f"{live_server.url}/vocab.html")
    expect(page.locator(".vocab-item .src")).to_have_text("Legacy · Ch 4")


def test_vocab_page_escapes_chapter_title(page: Page, live_server):
    from kokoro_reader.store import Store
    Store(live_server.home / "kokoro.db").add_vocab({
        "text": "x", "sentence": "x", "book_id": None, "book_title": "B", "chapter": 2,
        "chapter_title": "<b>Bold</b> & co", "story_number": 1,
        "para": 0, "sentence_clip": None, "status": "new"})
    page.goto(f"{live_server.url}/vocab.html")
    src = page.locator(".vocab-item .src")
    expect(src).to_have_text("B · Ch 1 · <b>Bold</b> & co")
    expect(src.locator("b")).to_have_count(0)


def test_vocab_page_labels_matter_chapter_by_title(page: Page, live_server):
    from kokoro_reader.store import Store
    Store(live_server.home / "kokoro.db").add_vocab({
        "text": "x", "sentence": "x", "book_id": None, "book_title": "B", "chapter": 0,
        "chapter_title": "Copyright", "story_number": None,
        "para": 0, "sentence_clip": None, "status": "new"})
    page.goto(f"{live_server.url}/vocab.html")
    expect(page.locator(".vocab-item .src")).to_have_text("B · 「Copyright」")


def test_a_saved_sentence_can_be_deleted_from_the_vocab_page(page: Page, live_server, seeded):
    book, ids = seeded
    para = live_server.api("GET", f"/api/books/{book}/chapters/1")["paragraphs"][0]
    live_server.api("POST", "/api/vocab", {"book_id": book, "chapter": 1, "para": 0, "start_char": 0,
                                           "end_char": len(para), "whole": True, "zh": "第一章。"})
    page.goto(f"{live_server.url}/vocab.html")
    items = page.locator(".vocab-item")
    expect(items).to_have_count(4)
    msgs = []
    page.once("dialog", lambda d: (msgs.append(d.message), d.accept()))
    items.first.get_by_role("button", name="删除").click()
    expect(items).to_have_count(3)
    assert "The first chapter" in msgs[0]


def save_with(live_server, book, word, para=0, **kw):
    paras = live_server.api("GET", f"/api/books/{book}/chapters/1")["paragraphs"]
    s = paras[para].index(word)
    return live_server.api("POST", "/api/vocab", {"book_id": book, "chapter": 1, "para": para,
                                                  "start_char": s, "end_char": s + len(word), **kw})


def test_explanations_are_folded_and_the_list_is_quiet(page: Page, live_server, seeded):
    """讲解 folds under its tag (a 翻译 is one line, so it stays open); the Chinese line has no bar;
    推到 Anki and 删除 wait for the pointer, and a pushed word just says so."""
    book, _ = seeded
    save_with(live_server, book, "street", zh="安静的街道", gloss="这里的意思\n▸ street\n  街道", gloss_mode="explain")
    save_with(live_server, book, "opens", gloss="【翻译】打开", gloss_mode="quick")
    page.goto(f"{live_server.url}/vocab.html")
    explained, translated = page.locator(".vocab-item").nth(1), page.locator(".vocab-item").nth(0)
    toggle = explained.get_by_role("button", name="讲解")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(explained.locator(".g-fold")).to_be_hidden()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(explained.locator(".g-fold")).to_be_visible()
    expect(explained.locator(".g-head")).to_have_text("street")
    expect(translated.locator(".gloss")).to_contain_text("打开")  # open, no toggle
    expect(translated.get_by_role("button", name="翻译")).to_have_count(0)

    assert explained.locator(".zh").evaluate("e => getComputedStyle(e).borderLeftWidth") == "0px"
    delete = translated.get_by_role("button", name="删除")  # a row nobody clicked (focus keeps them shown)
    page.mouse.move(5, 5)
    expect(delete).to_have_css("opacity", "0")
    translated.hover()
    expect(delete).to_have_css("opacity", "1")
