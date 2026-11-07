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


def open_voices(page: Page):
    page.locator("#voice").click()
    dlg = page.get_by_role("dialog", name="选音色")
    expect(dlg).to_be_visible()
    return dlg


def voice_opt(dlg, name: str):
    return dlg.get_by_role("button", name=re.compile(f"^{name}\\b"))


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
    expect(r.locator(".ttl a")).to_have_attribute("href", f"reader.html?book={book}&ch=2")
    expect(r.locator(".dur")).not_to_have_text("")
    expect(r.get_by_role("button", name="准备", exact=True)).to_have_count(0)
    # only that chapter was prepared
    expect(row(page, 1).locator(".tag")).to_have_text("未准备")
    expect(page.locator(".book-meta")).to_contain_text("音频 1/5")
    expect(page.locator(".book-meta")).to_contain_text("中文 1/5")


@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_FAKE_TRANSLATE": "1"}])
def test_prepare_whole_book_makes_audio_and_chinese(page: Page, live_server, book):
    open_book(page, live_server, book)
    page.get_by_role("button", name="准备整本书").click()
    expect(page.locator(".toast")).to_contain_text("5 章音频")
    for n in range(1, 6):
        expect(row(page, n).locator(".tag")).to_have_text("已准备", timeout=20000)
    expect(page.locator(".book-meta")).to_contain_text("中文 5/5")
    page.locator(".matter-toggle").click()
    expect(page.locator(".chapter-row.matter .tag")).to_have_text("未准备")


def test_book_voice_change_shows_reconvert(page: Page, live_server, book):
    live_server.api("POST", f"/api/books/{book}/convert", {"mode": "one", "chapter": 1})
    live_server.wait_chapter(book, 1, "done")
    open_book(page, live_server, book)
    expect(row(page, 1).locator(".tag")).to_have_text("已准备", timeout=15000)

    picker = page.locator("#voice")
    expect(picker).to_contain_text("Heart")
    r1 = row(page, 1)
    expect(r1.locator(".ttl a")).to_be_visible()
    expect(r1.get_by_role("button", name="用新音色重录")).to_have_count(0)

    dlg = open_voices(page)
    for group in ("美式女声", "美式男声", "英式女声", "英式男声"):
        expect(dlg.get_by_role("heading", name=group, exact=True)).to_have_count(1)
    expect(dlg.get_by_role("heading", name=re.compile("^微软"))).to_have_count(0)  # Microsoft voices are opt-in
    expect(voice_opt(dlg, "Heart")).to_have_attribute("aria-pressed", "true")  # the book's voice
    # a click plays that voice's 试听 at once; nothing changes until 确定
    with page.expect_response("**/api/voices/bm_george/preview.mp3") as resp:
        voice_opt(dlg, "George").click()
    assert resp.value.ok  # the audio element asks for a range: 206
    expect(voice_opt(dlg, "George")).to_have_attribute("aria-pressed", "true")
    expect(voice_opt(dlg, "Heart")).to_have_attribute("aria-pressed", "false")
    assert live_server.api("GET", f"/api/books/{book}")["voice"] == "af_heart"
    dlg.get_by_role("button", name="取消").click()
    expect(dlg).to_be_hidden()
    expect(picker).to_contain_text("Heart")
    assert live_server.api("GET", f"/api/books/{book}")["voice"] == "af_heart"

    dlg = open_voices(page)
    expect(voice_opt(dlg, "Heart")).to_have_attribute("aria-pressed", "true")  # 取消 dropped George
    voice_opt(dlg, "George").click()
    dlg.get_by_role("button", name="确定").click()
    expect(dlg).to_be_hidden()
    expect(picker).to_contain_text("George")
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



@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_EDGE": "1"}])
def test_book_lists_microsoft_voices_when_switched_on(page: Page, live_server, book):
    open_book(page, live_server, book)
    dlg = open_voices(page)
    for group in ("微软 美式女声", "微软 美式男声", "微软 英式女声", "微软 英式男声"):
        expect(dlg.get_by_role("heading", name=group, exact=True)).to_have_count(1)
    expect(voice_opt(dlg, "Ava")).to_have_attribute("data-voice", "en-US-AvaMultilingualNeural")


def test_rows_are_quiet_until_you_point_at_them(page: Page, live_server, book):
    """One status per row (audio + 中文 together, no pills); a row's buttons show on hover; the title
    is the way into the reader; the header has one accent button and no paragraph of instructions."""
    open_book(page, live_server, book)
    r = row(page, 1)
    expect(page.locator(".chapter-row .zh")).to_have_count(0)
    expect(page.locator(".book-head .hint")).to_have_count(0)
    assert page.locator(".book-head .btn.primary:visible").count() == 1
    tag_bg = r.locator(".tag").evaluate("e => getComputedStyle(e).backgroundColor")
    assert tag_bg == "rgba(0, 0, 0, 0)"
    prep = r.get_by_role("button", name="准备", exact=True)
    page.mouse.move(5, 5)
    expect(prep).to_have_css("opacity", "0")
    r.hover()
    expect(prep).to_have_css("opacity", "1")
    # an unprepared chapter opens too (text first, the reader prepares it)
    expect(r.locator(".ttl a")).to_have_attribute("href", f"reader.html?book={book}&ch=1")


def test_translation_progress_is_part_of_the_one_status(page: Page, live_server, book):
    def translating(route):
        data = route.fetch().json()
        for c in data["chapters"]:
            if c["idx"] == 1:
                c.update(status="done", duration=60.0, zh="running", zh_progress=0.4)
        route.fulfill(json=data)
    page.route(f"**/api/books/{book}", translating)
    open_book(page, live_server, book)
    expect(row(page, 1).locator(".tag")).to_have_text("翻译 40%")
    page.unroute(f"**/api/books/{book}")
