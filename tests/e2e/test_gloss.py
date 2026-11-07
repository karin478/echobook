"""划词: selecting text pops up a 翻译 or an AI 讲解 at once — which one is a sticky switch next to
「中文」(T toggles), never a question per selection. ⭐ 收藏 keeps the result with the word."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_drill import ready
from tests.e2e.test_reader import SELECT_JS, book, convert, open_reader, reader_epub  # noqa: F401

pytestmark = pytest.mark.e2e

LOUDER = "She called again, louder this time."


def test_selection_shows_a_translation_and_saving_keeps_it(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    expect(page.get_by_role("button", name="翻译", exact=True)).to_have_attribute("aria-pressed", "true")
    page.evaluate(SELECT_JS, [0, "no", 0, "answer", False])
    expect(page.locator("#gloss-body")).to_have_text("【翻译】no answer")
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏：no answer")
    expect(page.locator("#gloss-pop")).to_be_hidden()
    rec = live_server.api("GET", "/api/vocab")[0]
    assert (rec["text"], rec["gloss"], rec["gloss_mode"]) == ("no answer", "【翻译】no answer", "quick")


def test_explain_mode_is_a_sticky_switch_and_the_vocab_page_shows_it(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.get_by_role("button", name="讲解", exact=True).click()
    expect(page.get_by_role("button", name="讲解", exact=True)).to_have_attribute("aria-pressed", "true")
    page.evaluate(SELECT_JS, [1, "louder", 1, "louder", False])
    expect(page.locator("#gloss-body")).to_have_text(f"【讲解】louder｜{LOUDER}")
    # T flips the mode and the open popup follows it
    page.keyboard.press("KeyT")
    expect(page.get_by_role("button", name="翻译", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#gloss-body")).to_have_text("【翻译】louder")
    page.keyboard.press("KeyT")
    expect(page.locator("#gloss-body")).to_have_text(f"【讲解】louder｜{LOUDER}")
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏")
    page.reload()
    ready(page)
    expect(page.get_by_role("button", name="讲解", exact=True)).to_have_attribute("aria-pressed", "true")
    page.goto(f"{live_server.url}/vocab.html")
    item = page.locator(".vocab-item").first
    expect(item.locator(".gloss")).to_contain_text(f"【讲解】louder｜{LOUDER}")
    expect(item.locator(".gloss .g-tag")).to_have_text("讲解")


def test_popup_stays_while_the_page_scrolls(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.evaluate(SELECT_JS, [0, "no", 0, "answer", False])
    expect(page.locator("#gloss-body")).to_have_text("【翻译】no answer")
    page.evaluate("window.dispatchEvent(new Event('scroll'))")  # e.g. 连续 following the audio
    expect(page.locator("#gloss-pop")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#gloss-pop")).to_be_hidden()


EXPLAINED = ("多年见过的神社里，没见过像它这样的。\n"
             "▸ over the years\n  多年来；常和完成时连用。\n  例：It changed over the years. —— 这些年它变了。\n"
             "▸ never ... anything quite like it\n  从没见过跟它一样的；quite 加强否定。\n"
             "注意：两个 I'd 都是过去完成时。")


def test_explain_reply_is_laid_out_unit_by_unit(page: Page, live_server, book):
    convert(live_server, book, 0)
    page.route("**/api/lookup", lambda route: route.fulfill(
        json={"mode": "explain", "text": "louder", "result": EXPLAINED}))
    open_reader(page, live_server, book, 0)
    ready(page)
    page.get_by_role("button", name="讲解", exact=True).click()
    page.evaluate(SELECT_JS, [1, "louder", 1, "louder", False])
    body = page.locator("#gloss-body")
    expect(body.locator(".g-mean")).to_have_text("多年见过的神社里，没见过像它这样的。")
    expect(body.locator(".g-unit .g-head")).to_have_text(["over the years", "never ... anything quite like it"])
    expect(body.locator(".g-unit").first.locator(".g-ex")).to_have_text("例：It changed over the years. —— 这些年它变了。")
    expect(body.locator(".g-note")).to_have_text("注意：两个 I'd 都是过去完成时。")
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏")
    page.goto(f"{live_server.url}/vocab.html")
    expect(page.locator(".vocab-item .gloss .g-unit .g-head")).to_have_text(
        ["over the years", "never ... anything quite like it"])


def test_a_machine_translation_says_where_it_came_from(page: Page, live_server, book):
    convert(live_server, book, 0)
    page.route("**/api/lookup", lambda route: route.fulfill(
        json={"mode": "quick", "text": "louder", "result": "更大声", "source": "google"}))
    open_reader(page, live_server, book, 0)
    ready(page)
    page.evaluate(SELECT_JS, [1, "louder", 1, "louder", False])
    expect(page.locator("#gloss-body")).to_contain_text("更大声")
    expect(page.locator("#gloss-body .g-src")).to_have_text("Google")
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏")
    assert live_server.api("GET", "/api/vocab")[0]["gloss"] == "更大声"  # the label is not saved


def test_a_partial_word_grows_to_the_whole_word_and_is_saved_whole(page: Page, live_server, book):
    """智能扩选: dragging over part of "louder" looks up — and highlights, and saves — "louder"."""
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.evaluate("""() => {
      const w = [...document.querySelectorAll('#text p[data-p="1"] w')].find(x => x.textContent === "louder");
      getSelection().setBaseAndExtent(w.firstChild, 1, w.firstChild, 4);   // "oud"
      w.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
    }""")
    expect(page.locator("#gloss-body")).to_have_text("【翻译】louder")
    assert page.evaluate("getSelection().toString()") == "louder"
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏：louder")
    assert live_server.api("GET", "/api/vocab")[0]["text"] == "louder"


UNITS = ("她又喊了一声，这次更大声。\n"
         "▸ called again (call)\n  又喊了一次。\n  例：He called again. —— 他又打来了。\n"
         "▸ this time\n  这一次；和上一次对比。\n  例：Try harder this time. —— 这次再努力点。\n"
         "注意：louder 是比较级。")


def test_each_explained_unit_can_be_saved_on_its_own(page: Page, live_server, book):
    """☆ on a ▸ block saves just that chunk (with its block) — the popup stays for the next one."""
    convert(live_server, book, 0)
    page.route("**/api/lookup", lambda route: route.fulfill(
        json={"mode": "explain", "text": LOUDER, "result": UNITS, "source": "model"}))
    open_reader(page, live_server, book, 0)
    ready(page)
    page.get_by_role("button", name="讲解", exact=True).click()
    page.evaluate(SELECT_JS, [1, "called", 1, "louder", False])
    body = page.locator("#gloss-body")
    expect(body.locator(".g-unit")).to_have_count(2)
    save = body.get_by_role("button", name="收藏「this time」")
    # the ☆ sits right after the chunk's words, not pushed to the far edge of the row
    gap = page.evaluate("""(b) => {
      const head = b.closest('.g-head'), t = head.firstChild, r = document.createRange();
      r.selectNodeContents(t);
      return b.getBoundingClientRect().left - r.getBoundingClientRect().right;
    }""", save.element_handle())
    assert 0 <= gap <= 12, gap
    save.click()
    expect(page.locator(".toasts")).to_contain_text("已收藏：this time")
    expect(page.locator("#gloss-pop")).to_be_visible()
    expect(save).to_have_attribute("aria-pressed", "true")
    expect(save).to_be_disabled()
    (rec,) = live_server.api("GET", "/api/vocab")
    assert rec["text"] == "this time" and rec["gloss_mode"] == "explain"
    assert rec["gloss"] == "▸ this time\n这一次；和上一次对比。\n例：Try harder this time. —— 这次再努力点。"
    assert rec["sentence"] == LOUDER

    body.get_by_role("button", name="收藏「called again (call)」").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏：called again")
    assert [r["text"] for r in live_server.api("GET", "/api/vocab")] == ["called again", "this time"]
    # the vocab page shows each as its own entry with only its block
    page.goto(f"{live_server.url}/vocab.html")
    expect(page.locator(".vocab-item .gloss .g-unit .g-head")).to_have_text(["called again (call)", "this time"])


def test_a_translation_has_no_unit_stars(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.evaluate(SELECT_JS, [1, "louder", 1, "louder", False])
    expect(page.locator("#gloss-body")).to_have_text("【翻译】louder")
    expect(page.locator("#gloss-body .g-save")).to_have_count(0)
