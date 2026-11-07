import base64

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def open_shelf(page: Page, live_server):
    page.goto(live_server.url + "/")
    expect(page).to_have_url(live_server.url + "/shelf.html")
    expect(page.locator("#grid")).to_be_attached()


def cards(page: Page):
    return page.locator(".book-card")


def test_shelf_upload_shows_cover_card(page: Page, live_server, sample_epub):
    open_shelf(page, live_server)
    expect(page.get_by_role("button", name="＋ 上传 EPUB")).to_be_visible()
    expect(page.get_by_role("link", name="生词本")).to_have_attribute("href", "vocab.html")
    page.locator("#file-input").set_input_files(sample_epub)
    expect(cards(page)).to_have_count(1)
    card = cards(page).first
    expect(card).to_contain_text("The Sample Book")
    expect(card.locator(".card-title")).to_have_text("The Sample Book")
    expect(card.locator(".card-sub")).to_have_text("Ada Writer · 未开始")
    expect(card).not_to_contain_text("音频")  # how much is prepared is the book page's business
    book_id = live_server.api("GET", "/api/books")[0]["id"]
    expect(card.locator("a.cover")).to_have_attribute("href", f"book.html?id={book_id}")
    # the card survives a reload (it really is on the server)
    page.reload()
    expect(cards(page)).to_have_count(1)


def test_shelf_duplicate_upload_toast(page: Page, live_server, sample_epub):
    open_shelf(page, live_server)
    page.locator("#file-input").set_input_files(sample_epub)
    expect(cards(page)).to_have_count(1)
    page.locator("#file-input").set_input_files(sample_epub)
    expect(page.locator(".toast", has_text="这本书已经在书架上了")).to_be_visible()
    expect(cards(page)).to_have_count(1)


def test_shelf_bad_file_toast_chinese(page: Page, live_server, tmp_path):
    bad = tmp_path / "broken.epub"
    bad.write_bytes(b"this is not a zip file")
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    open_shelf(page, live_server)
    page.locator("#file-input").set_input_files(bad)
    expect(page.locator(".toast", has_text="不是有效的 EPUB")).to_be_visible()
    page.locator("#file-input").set_input_files(txt)
    expect(page.locator(".toast", has_text="只支持 EPUB 文件")).to_be_visible()
    expect(cards(page)).to_have_count(0)


def test_shelf_drop_files_uploads_all(page: Page, live_server, make_book):
    a, b = make_book("Alpha Story"), make_book("Beta Story")
    open_shelf(page, live_server)
    files = [{"name": p.name, "b64": base64.b64encode(p.read_bytes()).decode()} for p in (a, b)]
    page.evaluate("""(files) => {
        const dt = new DataTransfer();
        for (const f of files) {
            const bin = Uint8Array.from(atob(f.b64), c => c.charCodeAt(0));
            dt.items.add(new File([bin], f.name, {type: 'application/epub+zip'}));
        }
        window.__dt = dt;
        document.body.dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true}));
    }""", files)
    expect(page.locator("#drop-overlay")).to_be_visible()
    expect(page.locator("#drop-overlay")).to_contain_text("松开即上传")
    page.evaluate("document.body.dispatchEvent(new DragEvent('drop', {dataTransfer: window.__dt, bubbles: true, cancelable: true}))")
    expect(page.locator("#drop-overlay")).to_be_hidden()
    expect(cards(page)).to_have_count(2)
    expect(cards(page).nth(0)).to_contain_text("Alpha Story")
    expect(cards(page).nth(1)).to_contain_text("Beta Story")


def test_shelf_drag_reorder_persists(page: Page, live_server, make_book):
    live_server.upload(make_book("First Book"))
    live_server.upload(make_book("Second Book"))
    open_shelf(page, live_server)
    expect(cards(page)).to_have_count(2)
    expect(cards(page).nth(0)).to_contain_text("First Book")
    with page.expect_response(lambda r: r.url.endswith("/api/books/order") and r.request.method == "PUT") as resp:
        cards(page).nth(1).drag_to(cards(page).nth(0), target_position={"x": 10, "y": 40})
    assert resp.value.ok
    expect(cards(page).nth(0)).to_contain_text("Second Book")
    page.reload()
    expect(cards(page)).to_have_count(2)
    expect(cards(page).nth(0)).to_contain_text("Second Book")
    expect(cards(page).nth(1)).to_contain_text("First Book")


def test_shelf_delete_offers_audio_only_or_the_whole_book(page: Page, live_server, sample_epub):
    """删除 asks: only the audio (text, 中文, progress stay) or the whole book. 生词本 always stays."""
    bid = live_server.upload(sample_epub)["id"]
    open_shelf(page, live_server)
    card = cards(page).first
    expect(card).to_be_visible()
    expect(page.locator("#storage")).to_contain_text("占用")

    # no audio yet: only "delete the book" is offered; 取消 keeps it
    card.hover()
    card.get_by_role("button", name="删除").click()
    dlg = page.get_by_role("dialog", name="删除《The Sample Book》")
    expect(dlg).to_be_visible()
    expect(dlg).to_contain_text("生词本里的收藏都会保留")
    expect(dlg.get_by_role("button", name="只删音频")).to_have_count(0)
    dlg.get_by_role("button", name="取消").click()
    expect(dlg).to_be_hidden()
    expect(cards(page)).to_have_count(1)

    # with audio: 只删音频 keeps the book, back to 未准备
    live_server.api("POST", f"/api/books/{bid}/convert", {"mode": "one", "chapter": 0})
    live_server.wait_chapter(bid, 0, "done")
    vocab = live_server.api("POST", "/api/vocab", {"book_id": bid, "chapter": 0, "para": 0,
                                                     "start_char": 0, "end_char": 3})
    page.mouse.move(0, 0)
    card.click(button="right")  # right-click also reveals the delete button
    card.get_by_role("button", name="删除").click()
    expect(dlg).to_contain_text("音频")
    dlg.get_by_role("button", name="只删音频").click()
    expect(page.locator(".toasts")).to_contain_text("已删除音频")
    expect(cards(page)).to_have_count(1)
    assert live_server.api("GET", f"/api/books/{bid}")["chapters"][0]["status"] == "none"

    # the whole book: gone, but its 生词 stay
    card.hover()
    card.get_by_role("button", name="删除").click()
    dlg.get_by_role("button", name="删除整本书").click()
    expect(cards(page)).to_have_count(0)
    page.reload()
    expect(cards(page)).to_have_count(0)
    assert live_server.api("GET", "/api/books") == []
    assert [v["id"] for v in live_server.api("GET", "/api/vocab")] == [vocab["id"]]


def read_to(live_server, book_id: str, chapter: int, ratio: float):
    live_server.api("PUT", f"/api/books/{book_id}/progress",
                    {"chapter": chapter, "para": 0, "word": 0, "time": 1.0, "ratio": ratio})


def test_continue_reading_is_the_last_book_read(page: Page, live_server, make_book):
    a = live_server.upload(make_book("Alpha", "Ann"))["id"]
    b = live_server.upload(make_book("Beta", "Bob"))["id"]
    open_shelf(page, live_server)
    expect(cards(page)).to_have_count(2)
    expect(page.locator("#continue")).to_be_hidden()  # nothing read yet

    read_to(live_server, a, 1, 0.5)
    read_to(live_server, b, 2, 0.25)
    page.reload()
    hero = page.locator("#continue")
    expect(hero).to_be_visible()
    expect(hero).to_contain_text("继续阅读")
    expect(hero).to_contain_text("Beta")
    expect(hero.locator(".cont-sub")).to_contain_text("Ch 2 · ")  # "Chapter 2" says nothing: first line instead
    expect(hero.locator(".cont-sub")).not_to_contain_text("Chapter 2")
    expect(hero.get_by_role("link", name="继续")).to_have_attribute("href", f"reader.html?book={b}&ch=2")
    # the card under the cover: title, author · how far
    beta = page.locator(f'.book-card[data-id="{b}"]')
    expect(beta.locator(".card-sub")).to_contain_text("Bob · ")
    expect(beta.locator(".card-sub")).to_contain_text("%")

    read_to(live_server, a, 1, 0.6)
    read_to(live_server, a, 3, 0.001)  # a sliver of a chapter: 刚开始, not "0%"
    page.reload()
    expect(page.locator(f'.book-card[data-id="{a}"] .card-sub')).to_have_text("Ann · 12%")
    expect(hero).to_contain_text("Alpha")
    expect(hero.get_by_role("link", name="继续")).to_have_attribute("href", f"reader.html?book={a}&ch=3")


def test_only_one_accent_button_when_there_is_something_to_continue(page: Page, live_server, sample_epub):
    bid = live_server.upload(sample_epub)["id"]
    read_to(live_server, bid, 1, 0.1)
    open_shelf(page, live_server)
    expect(page.locator("#continue")).to_be_visible()
    expect(page.get_by_role("button", name="＋ 上传 EPUB")).not_to_have_class("btn primary")
    assert page.locator(".btn.primary:visible").count() == 1  # 继续 is the one


def test_a_sliver_read_says_just_started(page: Page, live_server, sample_epub):
    bid = live_server.upload(sample_epub)["id"]
    read_to(live_server, bid, 1, 0.001)
    open_shelf(page, live_server)
    expect(cards(page).first.locator(".card-sub")).to_have_text("Ada Writer · 刚开始")
