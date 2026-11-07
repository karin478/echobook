"""逐句 is the only reading mode: one sentence unit highlighted, its Chinese under it (↓ toggles,
sticky). Playback is 连续 (follows the audio sentence by sentence) or 一句一停 (stops at each unit end)."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_reader import SELECT_JS, book, chapter, convert, open_reader, reader_epub  # noqa: F401

pytestmark = pytest.mark.e2e

UNIT_TEXT = "[...CSS.highlights.get('drill') || []].map(r => r.toString()).join('|')"
ZH_PARA = "document.getElementById('zh-text').previousElementSibling.dataset.p"


def ready(page: Page):
    expect(page.locator("#zh-card")).to_be_visible()
    expect(page.locator("#zh-text")).to_be_visible(timeout=15000)  # Chinese is on by default


def stop_mode(page: Page):
    page.get_by_role("button", name="一句一停").click()
    expect(page.get_by_role("button", name="一句一停")).to_have_attribute("aria-pressed", "true")


def test_sentence_mode_is_default_with_chinese_and_sticky_toggle(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    expect(page.get_by_role("button", name="连续")).to_have_attribute("aria-pressed", "true")
    assert page.evaluate(UNIT_TEXT) == "The knock came twice, and there was no answer from the house."
    expect(page.locator("#zh-text")).to_have_text("【译】The knock came twice, and there was no answer from the house.")
    assert page.evaluate(ZH_PARA) == "0"  # sits right under the current paragraph
    page.keyboard.press("ArrowDown")
    expect(page.locator("#zh-text")).to_be_hidden()
    page.keyboard.press("ArrowRight")
    assert page.evaluate(UNIT_TEXT) == "Mara waited on the step."
    expect(page.locator("#zh-pos")).to_contain_text("2 /")
    expect(page.locator("#zh-text")).to_be_hidden()  # off stays off on the next sentence
    page.keyboard.press("ArrowDown")
    expect(page.locator("#zh-text")).to_have_text("【译】Mara waited on the step.")
    page.keyboard.press("ArrowLeft")
    assert page.evaluate(UNIT_TEXT).startswith("The knock came twice")
    page.locator("#zh-toggle").click()
    expect(page.locator("#zh-text")).to_be_hidden()
    expect(page.locator("#zh-toggle")).to_have_attribute("aria-pressed", "false")


def test_continuous_follows_the_audio_and_arrows_keep_playing(page: Page, live_server, book):
    convert(live_server, book, 0)
    toks = chapter(live_server, book, 0)["tokens"]
    end0 = [t for t in toks if t["p"] == 0 and t["a"] is not None][-1]["b"]
    open_reader(page, live_server, book, 0)
    ready(page)
    for _ in range(20):
        page.keyboard.press("BracketRight")  # 2× so the test is quick
    page.keyboard.press("Space")
    # plays straight through the end of sentence 1 into sentence 2; highlight + Chinese follow
    page.wait_for_function(f"document.getElementById('au').currentTime > {end0 + 0.3}", timeout=10000)
    assert page.evaluate("document.getElementById('au').paused") is False
    expect(page.locator("#zh-pos")).to_contain_text("2 /")
    assert page.evaluate(UNIT_TEXT) == "Mara waited on the step."
    expect(page.locator("#zh-text")).to_have_text("【译】Mara waited on the step.")
    page.keyboard.press("ArrowRight")
    assert page.evaluate(UNIT_TEXT) == "She called again, louder this time."
    page.wait_for_timeout(300)
    assert page.evaluate("document.getElementById('au').paused") is False


def test_stop_mode_plays_only_the_current_unit(page: Page, live_server, book):
    convert(live_server, book, 0)
    toks = chapter(live_server, book, 0)["tokens"]
    timed0 = [t for t in toks if t["p"] == 0 and t["a"] is not None]
    end = timed0[-1]["b"]
    open_reader(page, live_server, book, 0)
    for _ in range(20):
        page.keyboard.press("BracketRight")
    ready(page)
    stop_mode(page)
    page.keyboard.press("Space")
    page.wait_for_function("!document.getElementById('au').paused")
    page.wait_for_function("document.getElementById('au').paused", timeout=10000)
    t = page.evaluate("document.getElementById('au').currentTime")
    assert end - 0.2 <= t <= end + 0.35
    # ↑ replays from the unit start (R still does too)
    page.keyboard.press("ArrowUp")
    page.wait_for_function("!document.getElementById('au').paused")
    assert page.evaluate("document.getElementById('au').currentTime") < 1.0
    assert page.evaluate("scrollY") == 0  # ↑ is taken: it doesn't scroll the page
    page.wait_for_function("document.getElementById('au').paused", timeout=10000)
    page.keyboard.press("KeyR")
    page.wait_for_function("!document.getElementById('au').paused")
    assert page.evaluate("document.getElementById('au').currentTime") < 1.0
    # → plays the next sentence and stops at its end
    page.keyboard.press("ArrowRight")
    assert page.evaluate(UNIT_TEXT) == "Mara waited on the step."
    page.wait_for_function("!document.getElementById('au').paused")
    page.wait_for_function("document.getElementById('au').paused", timeout=10000)
    assert page.evaluate(UNIT_TEXT) == "Mara waited on the step."
    # S switches back to 连续
    page.keyboard.press("KeyS")
    expect(page.get_by_role("button", name="连续")).to_have_attribute("aria-pressed", "true")


def test_click_selects_unit(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.locator('#text p[data-p="1"] w', has_text="louder").first.evaluate(
        "el => { el.click(); document.getElementById('au').pause(); }")
    assert page.evaluate(UNIT_TEXT) == "She called again, louder this time."


def test_works_without_audio_and_remembers_choices(page: Page, live_server, book):
    open_reader(page, live_server, book, 1, playable=False)
    ready(page)
    assert page.evaluate(UNIT_TEXT) == "Morning came slowly to the harbour town."
    page.keyboard.press("ArrowRight")
    expect(page.locator("#zh-text")).to_have_text("【译】The boats were already out.")
    page.keyboard.press("ArrowDown")
    expect(page.locator("#zh-text")).to_be_hidden()
    stop_mode(page)
    page.reload()
    expect(page.locator("#zh-pos")).to_contain_text(" / ", timeout=15000)
    expect(page.locator("#zh-text")).to_be_hidden()  # Chinese off is remembered
    expect(page.get_by_role("button", name="一句一停")).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("ArrowDown")
    expect(page.locator("#zh-text")).to_be_visible()


def test_favorite_sentence_keeps_english_and_chinese(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    # the star sits at the end of the sentence's Chinese line, not in the bottom bar
    fav = page.locator("#zh-text #fav")
    expect(fav).to_be_visible()
    expect(page.locator("#dock #fav")).to_have_count(0)
    expect(page.locator("#zh-text .zh-body")).to_have_text("【译】The knock came twice, and there was no answer from the house.")
    expect(fav).to_have_attribute("aria-pressed", "false")
    fav.click()
    expect(fav).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("ArrowRight")
    expect(fav).to_have_attribute("aria-pressed", "false")  # next sentence is not saved
    page.keyboard.press("KeyF")
    expect(fav).to_have_attribute("aria-pressed", "true")
    page.wait_for_timeout(300)
    saved = {v["text"]: v for v in live_server.api("GET", "/api/vocab")}
    first = saved["The knock came twice, and there was no answer from the house."]
    assert first["kind"] == "sentence"
    assert first["zh"] == "【译】The knock came twice, and there was no answer from the house."
    assert saved["Mara waited on the step."]["zh"] == "【译】Mara waited on the step."
    # F again un-saves; the mark survives a reload
    page.keyboard.press("KeyF")
    expect(fav).to_have_attribute("aria-pressed", "false")
    page.keyboard.press("ArrowLeft")
    page.reload()
    ready(page)
    expect(fav).to_have_attribute("aria-pressed", "true")
    # the 生词本 shows the Chinese under the sentence
    page.goto(f"{live_server.url}/vocab.html")
    item = page.locator(".vocab-item").first
    expect(item.locator(".zh")).to_have_text("【译】The knock came twice, and there was no answer from the house.")


def test_selection_star_saves_the_sentence_chinese_too(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.evaluate(SELECT_JS, [1, "louder", 1, "louder", False])
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toasts")).to_contain_text("已收藏")
    rec = live_server.api("GET", "/api/vocab")[0]
    assert rec["kind"] == "word" and "louder" in rec["text"]
    assert rec["zh"] == "【译】She called again, louder this time."


def test_stop_mode_follows_a_seek_and_stops_at_that_sentence(page: Page, live_server, book):
    convert(live_server, book, 0)
    toks = chapter(live_server, book, 0)["tokens"]
    p1 = [t for t in toks if t["p"] == 1 and t["a"] is not None]
    p1 = p1[:[t["w"] for t in p1].index("step") + 1]  # "Mara waited on the step."
    open_reader(page, live_server, book, 0)
    ready(page)
    stop_mode(page)
    page.keyboard.press("Space")
    # drag the slider while sentence 1 is still playing: seek in the same JS turn that sees it
    # playing, so a slow machine cannot reach sentence 1's end (and stop there) first
    page.wait_for_function(f"""(() => {{ const au = document.getElementById('au');
        if (au.paused) return false; au.currentTime = {p1[0]['a'] + 0.05}; return true; }})()""")
    page.wait_for_function("document.getElementById('au').paused", timeout=10000)
    t = page.evaluate("document.getElementById('au').currentTime")
    # the bug stopped at sentence 1's end (before p1 starts); a loaded machine may pause a bit late
    assert p1[-1]["b"] - 0.2 <= t <= p1[-1]["b"] + 0.6  # played that sentence to its end
    assert page.evaluate(UNIT_TEXT) == "Mara waited on the step."


def test_stop_mode_also_stops_when_playback_is_started_from_outside(page: Page, live_server, book):
    convert(live_server, book, 0)
    toks = chapter(live_server, book, 0)["tokens"]
    end0 = [t for t in toks if t["p"] == 0 and t["a"] is not None][-1]["b"]
    open_reader(page, live_server, book, 0)
    ready(page)
    stop_mode(page)
    page.evaluate("document.getElementById('au').play()")  # e.g. a media key / headphones
    page.wait_for_function("!document.getElementById('au').paused")
    page.wait_for_function("document.getElementById('au').paused", timeout=10000)
    t = page.evaluate("document.getElementById('au').currentTime")
    assert end0 - 0.2 <= t <= end0 + 0.35


def test_switching_chapters_drops_the_old_sentences_at_once(page: Page, live_server, book):
    convert(live_server, book, 0, 1)
    open_reader(page, live_server, book, 0)
    ready(page)
    held = []
    page.route("**/chapters/1/sentences", lambda r: held.append(r))
    page.get_by_role("link", name="下一章 →").click()
    expect(page.locator("h1")).to_have_text("Chapter 2")
    page.wait_for_timeout(200)
    assert page.evaluate(UNIT_TEXT) == ""          # nothing of chapter 1 painted on chapter 2
    expect(page.locator("#zh-text")).to_be_hidden()
    for r in held:
        r.continue_()


def test_right_arrow_on_the_last_sentence_opens_the_next_chapter(page: Page, live_server, book):
    convert(live_server, book, 0, 1)
    open_reader(page, live_server, book, 0)
    ready(page)
    n = int(page.locator("#zh-pos").inner_text().split("/")[1])
    for _ in range(n - 1):
        page.keyboard.press("ArrowRight")
    expect(page.locator("#zh-pos")).to_contain_text(f"{n} / {n}")
    page.keyboard.press("ArrowRight")
    expect(page.locator("h1")).to_have_text("Chapter 2")


def test_arrows_still_move_sentences_after_using_the_speed_slider(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    page.locator("#rate").focus()
    page.locator("#rate").dispatch_event("change")
    page.keyboard.press("ArrowRight")
    assert page.evaluate(UNIT_TEXT) == "Mara waited on the step."


def test_audio_that_fails_to_load_says_so_and_can_retry(page: Page, live_server, book):
    convert(live_server, book, 0)
    page.route("**/audio.mp3*", lambda r: r.abort())
    page.goto(f"{live_server.url}/reader.html?book={book}&ch=0")
    banner = page.locator("#banner")
    expect(banner).to_contain_text("音频加载失败", timeout=10000)
    page.unroute("**/audio.mp3*")
    banner.get_by_role("button", name="重试").click()
    page.wait_for_function("document.getElementById('au').readyState >= 1", timeout=10000)
    expect(banner).to_be_hidden()
