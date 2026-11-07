import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def book(live_server, sample_epub) -> str:
    return live_server.upload(sample_epub)["id"]


def open_book(page: Page, live_server, book_id: str):
    page.goto(f"{live_server.url}/book.html?id={book_id}")
    expect(page.locator("h1")).to_have_text("The Sample Book")


def row(page: Page, num: int):
    return page.locator(f'.chapter-row[data-num="{num}"]')


def test_book_page_lists_story_chapters_and_hides_matter(page: Page, live_server, book):
    open_book(page, live_server, book)
    expect(page.locator(".book-meta")).to_contain_text("Ada Writer")
    expect(page.locator(".book-meta")).to_contain_text("5 章")
    expect(page.locator(".chapter-row.story")).to_have_count(5)
    expect(row(page, 1)).to_contain_text("Ch 1")
    expect(row(page, 1)).to_contain_text("The first chapter opens on a quiet street.")
    expect(row(page, 1).locator(".tag")).to_have_text("未准备")
    expect(row(page, 1).get_by_role("button", name="准备", exact=True)).to_be_visible()

    toggle = page.locator(".matter-toggle")
    expect(toggle).to_contain_text("已隐藏 1 个非正文部分")
    matter = page.locator(".chapter-row.matter")
    expect(matter).to_have_count(1)
    expect(matter).to_be_hidden()
    toggle.click()
    expect(matter).to_be_visible()
    expect(matter).to_contain_text("Copyright")

    # marking it as story moves it into the numbered list (and persists)
    matter.get_by_role("button", name="设为正文").click()
    expect(page.locator(".chapter-row.story")).to_have_count(6)
    expect(page.locator(".matter-toggle")).to_be_hidden()
    assert next(c for c in live_server.api("GET", f"/api/books/{book}")["chapters"] if c["idx"] == 0)["is_matter"] is False

    # ...and a story chapter can be hidden again via its small menu
    first = page.locator(".chapter-row.story").first
    first.locator(".row-menu-btn").click()
    first.get_by_role("button", name="设为非正文").click()
    expect(page.locator(".chapter-row.story")).to_have_count(5)
    expect(page.locator(".matter-toggle")).to_contain_text("已隐藏 1 个非正文部分")


def test_book_prepare_one_makes_audio_and_chinese(page: Page, live_server, book):
    open_book(page, live_server, book)
    r = row(page, 2)
    r.get_by_role("button", name="准备", exact=True).click()  # one trigger: audio + Chinese
    expect(r.locator(".tag")).to_have_text("已准备", timeout=15000)
    expect(r.locator(".zh")).to_have_text("中文", timeout=15000)
    expect(r.get_by_role("link", name="▶ 读")).to_have_attribute("href", f"reader.html?book={book}&ch=2")
    expect(r.locator(".dur")).not_to_have_text("")
    expect(r.get_by_role("button", name="准备", exact=True)).to_have_count(0)
    # only that chapter was prepared
    expect(row(page, 1).locator(".tag")).to_have_text("未准备")
    expect(page.locator(".book-meta")).to_contain_text("音频 1/5")
    expect(page.locator(".book-meta")).to_contain_text("中文 1/5")


@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_FAKE_TRANSLATE": "1"}])
def test_prepare_whole_book_makes_audio_and_chinese(page: Page, live_server, book):
    open_book(page, live_server, book)
    expect(row(page, 1).locator(".zh")).to_have_text("")
    page.get_by_role("button", name="准备整本书").click()
    expect(page.locator(".toast")).to_contain_text("5 章音频")
    for n in range(1, 6):
        expect(row(page, n).locator(".tag")).to_have_text("已准备", timeout=20000)
        expect(row(page, n).locator(".zh")).to_have_text("中文", timeout=20000)
    expect(page.locator(".book-meta")).to_contain_text("中文 5/5")
    page.locator(".matter-toggle").click()
    expect(page.locator(".chapter-row.matter .tag")).to_have_text("未准备")
    expect(page.locator(".chapter-row.matter .zh")).to_have_text("")


def test_book_voice_change_shows_reconvert(page: Page, live_server, book):
    live_server.api("POST", f"/api/books/{book}/convert", {"mode": "one", "chapter": 1})
    live_server.wait_chapter(book, 1, "done")
    open_book(page, live_server, book)
    expect(row(page, 1).locator(".tag")).to_have_text("已准备", timeout=15000)

    select = page.locator("#voice")
    expect(select).to_have_value("af_heart")
    for group in ("美式女声", "美式男声", "英式女声", "英式男声"):
        expect(select.locator(f'optgroup[label="{group}"]')).to_have_count(1)
    expect(page.get_by_role("button", name="🔈 试听")).to_be_visible()

    r1 = row(page, 1)
    expect(r1.get_by_role("link", name="▶ 读")).to_be_visible()
    expect(r1.get_by_role("button", name="用新音色重录")).to_have_count(0)

    select.select_option("bm_george")
    expect(r1.get_by_role("button", name="用新音色重录")).to_be_visible()
    assert live_server.api("GET", f"/api/books/{book}")["voice"] == "bm_george"

    r1.get_by_role("button", name="用新音色重录").click()
    expect(r1.get_by_role("button", name="用新音色重录")).to_have_count(0, timeout=15000)
    expect(r1.locator(".tag")).to_have_text("已准备")
    assert live_server.wait_chapter(book, 1, "done")["voice"] == "bm_george"


def test_book_continue_reading_links_to_progress(page: Page, live_server, book):
    open_book(page, live_server, book)
    start = page.locator("#continue")
    expect(start).to_have_text("▶ 开始阅读")
    expect(start).to_have_attribute("href", f"reader.html?book={book}&ch=1")

    live_server.api("PUT", f"/api/books/{book}/progress",
                    {"chapter": 3, "para": 1, "word": 4, "time": 12.0, "ratio": 0.4})
    live_server.api("PUT", f"/api/books/{book}/progress",
                    {"chapter": 2, "para": 9, "word": 0, "time": 99.0, "ratio": 0.99})
    live_server.api("PUT", f"/api/books/{book}/progress",
                    {"chapter": 3, "para": 1, "word": 4, "time": 12.0, "ratio": 0.4})
    page.reload()
    expect(start).to_have_text("▶ 继续阅读 Ch 3")
    expect(start).to_have_attribute("href", f"reader.html?book={book}&ch=3")
    expect(page.locator(".book-meta")).to_contain_text("读到 Ch 3 的 40%")
    expect(row(page, 3).locator(".tag")).to_have_text("读到 40%")
    expect(row(page, 2).locator(".tag")).to_have_text("✓ 已读完")
    expect(row(page, 3)).to_have_class(re.compile(r"\bcurrent\b"))


def test_estimates_before_and_while_preparing(page: Page, live_server, book):
    """预计: how long the audio takes to make and how big it gets — before starting, and the time
    left while it runs. Learned from this machine's past conversions."""
    open_book(page, live_server, book)
    expect(page.locator(".book-meta")).to_contain_text(re.compile(r"占用 [\d.]+ (KB|MB)"))
    est = page.locator("#prep-est")
    expect(est).to_have_text(re.compile(r"^预计 (不到 1 分钟|约 \d+ 分钟) · 音频约 [\d.]+ (KB|MB)$"))
    expect(row(page, 1).get_by_role("button", name="准备")).to_have_attribute("title", re.compile("预计"))

    # while preparing, the progress shows the time left (the queue made to look busy and slow)
    def busy_book(route):
        data = route.fetch().json()
        for c in data["chapters"]:
            if c["idx"] == 2:
                c["status"] = "converting"
        route.fulfill(json=data)
    page.route(f"**/api/books/{book}", busy_book)
    page.route("**/api/queue", lambda r: r.fulfill(json={
        "current": {"book_id": book, "idx": 2, "progress": 0.5}, "pending": [], "eta": {book: 5400}}))
    expect(page.locator("#prep-eta")).to_have_text("音频还剩约 1.5 小时")
    page.unroute(f"**/api/books/{book}")
    page.unroute("**/api/queue")

    row(page, 1).get_by_role("button", name="准备").click()
    expect(page.locator(".toast")).to_contain_text("预计")
