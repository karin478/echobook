"""阅读页改版: while reading there is only the text and one slim dock row. Shortcuts live behind
`?` (and the ⋯ menu), speed is a chip that opens presets, 划词 翻译/讲解 sits in ⋯, the Chinese
line is quiet text under the sentence (no box), and the default typeface is Charter."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_drill import ready
from tests.e2e.test_reader import audio, book, convert, open_reader, reader_epub  # noqa: F401
from tests.e2e.test_typography import P, css

pytestmark = pytest.mark.e2e


def mid_y(page: Page, sel: str) -> float:
    b = page.locator(sel).bounding_box()
    return b["y"] + b["height"] / 2


def test_dock_is_one_row_and_no_hint_line(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    expect(page.locator(".kbd-hint")).to_have_count(0)
    play = mid_y(page, "#play")
    for sel in ("#mode-stop", "#zh-toggle", "#speed-btn", "#more-btn", "#zh-pos"):
        assert abs(mid_y(page, sel) - play) < 4, sel
    assert page.locator(".r-top #mode-stop").count() == 0  # 连续/单句循环 moved down into the dock
    assert page.locator("#dock").bounding_box()["height"] <= 72


def test_question_mark_opens_the_shortcut_sheet(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    sheet = page.get_by_role("dialog", name="快捷键")
    expect(sheet).to_be_hidden()
    page.keyboard.press("Shift+Slash")
    expect(sheet).to_be_visible()
    for key in ("播放/暂停", "单句循环", "中文", "收藏本句", "语速"):
        expect(sheet).to_contain_text(key)
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    page.get_by_role("button", name="更多").click()
    page.get_by_role("button", name="快捷键").click()
    expect(sheet).to_be_visible()
    page.keyboard.press("Shift+Slash")  # ? again closes it
    expect(sheet).to_be_hidden()


def test_speed_chip_opens_presets_and_the_fine_slider(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    chip, pop = page.locator("#speed-btn"), page.locator("#speed-pop")
    expect(chip).to_have_text("1.00×")
    expect(pop).to_be_hidden()
    chip.click()
    expect(pop).to_be_visible()
    pop.get_by_role("button", name="0.85×").click()
    assert audio(page, "playbackRate") == pytest.approx(0.85)
    expect(chip).to_have_text("0.85×")
    expect(pop.get_by_role("button", name="0.85×")).to_have_attribute("aria-pressed", "true")
    pop.locator("#rate").fill("1.3")
    assert audio(page, "playbackRate") == pytest.approx(1.3)
    expect(chip).to_have_text("1.30×")
    page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    page.keyboard.press("BracketRight")  # [ ] still work with the popover closed
    expect(chip).to_have_text("1.35×")


def test_gloss_mode_lives_in_the_more_menu(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    more = page.locator("#more-pop")
    expect(more).to_be_hidden()
    page.get_by_role("button", name="更多").click()
    expect(more).to_be_visible()
    more.get_by_role("button", name="讲解", exact=True).click()
    expect(more.get_by_role("button", name="讲解", exact=True)).to_have_attribute("aria-pressed", "true")
    page.mouse.click(5, 300)  # a click elsewhere closes it
    expect(more).to_be_hidden()


def test_chinese_line_is_quiet_text_not_a_box(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    ready(page)
    expect(page.locator("#zh-text")).to_be_visible(timeout=15000)
    assert css(page, "#zh-text", "borderLeftWidth") == "0px"
    assert css(page, "#zh-text", "backgroundColor") == "rgba(0, 0, 0, 0)"
    zh = float(css(page, "#zh-text", "fontSize").rstrip("px"))
    en = float(css(page, P, "fontSize").rstrip("px"))
    assert zh < en * 0.85
    expect(page.locator("#zh-text #fav")).to_be_visible()  # ☆ stays at the end of the line


def test_default_typeface_is_charter(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0, playable=False)
    expect(page.locator(P)).to_be_visible()
    assert css(page, P, "fontFamily").startswith("Charter")
