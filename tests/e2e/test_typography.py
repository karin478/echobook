"""阅读设置 (Aa): font size, typeface, line spacing and column width of the text only — unlike
browser zoom, the top bar and the bottom dock keep their size. Remembered across reloads."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_reader import book, convert, open_reader, reader_epub  # noqa: F401

pytestmark = pytest.mark.e2e

P = "#text p[data-p='0']"


def css(page: Page, sel: str, prop: str) -> str:
    return page.evaluate(f"getComputedStyle(document.querySelector({sel!r})).{prop}")


def test_reading_settings_change_the_text_not_the_bars_and_are_remembered(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0, playable=False)
    expect(page.locator(P)).to_be_visible()
    assert css(page, P, "fontSize") == "20px"
    assert css(page, ".reader-main", "maxWidth") == "700px"
    bar_font = css(page, ".r-top", "fontSize")

    page.get_by_role("button", name="阅读设置").click()
    panel = page.get_by_role("dialog", name="阅读设置")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="字号加大").click()
    panel.get_by_role("button", name="字号加大").click()
    expect(panel.locator(".typo-size")).to_have_text("22")
    panel.get_by_role("button", name="宽", exact=True).click()
    panel.get_by_role("button", name="无衬线").click()
    panel.get_by_role("button", name="宽松").click()

    assert css(page, P, "fontSize") == "22px"
    assert css(page, ".reader-main", "maxWidth") == "900px"
    assert "Georgia" not in css(page, P, "fontFamily")
    assert css(page, P, "lineHeight") == f"{22 * 2.0:g}px"
    assert css(page, ".r-top", "fontSize") == bar_font  # bars are not zoomed

    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    page.reload()
    expect(page.locator(P)).to_be_visible()
    assert css(page, P, "fontSize") == "22px"
    assert css(page, ".reader-main", "maxWidth") == "900px"

    page.get_by_role("button", name="阅读设置").click()
    expect(panel.get_by_role("button", name="宽", exact=True)).to_have_attribute("aria-pressed", "true")
    panel.get_by_role("button", name="恢复默认").click()
    assert css(page, P, "fontSize") == "20px"
    assert css(page, ".reader-main", "maxWidth") == "700px"
    assert "Georgia" in css(page, P, "fontFamily")


EDGES_JS = """() => {
  const box = (s) => document.querySelector(s).getBoundingClientRect();
  return {back: box('.r-top .back').left, last: box('.r-top .in > :last-child').right,
          textL: box('#text').left, textR: box('#text').right};
}"""


@pytest.mark.parametrize("width", [None, "很宽", "窄"])
def test_top_bar_lines_up_with_the_text_column(page: Page, live_server, book, width):
    """The bar's back link and last button sit on the text's left and right edges, at any width."""
    convert(live_server, book, 0)
    page.set_viewport_size({"width": 1400, "height": 900})
    open_reader(page, live_server, book, 0, playable=False)
    expect(page.locator(P)).to_be_visible()
    if width:
        page.get_by_role("button", name="阅读设置").click()
        page.get_by_role("dialog", name="阅读设置").get_by_role("button", name=width, exact=True).click()
        page.keyboard.press("Escape")
    e = page.evaluate(EDGES_JS)
    assert abs(e["back"] - e["textL"]) <= 1 and abs(e["last"] - e["textR"]) <= 1, e


def test_top_bar_icon_buttons_match(page: Page, live_server, book):
    """阅读设置 and 浅色/深色 are drawn icons of one size and style, not font glyphs."""
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0, playable=False)
    expect(page.locator(P)).to_be_visible()
    a, t = page.get_by_role("button", name="阅读设置"), page.get_by_role("button", name="切换浅色 / 深色")
    for b in (a, t):
        expect(b.locator("svg")).to_have_count(1)
    ba, bt = a.bounding_box(), t.bounding_box()
    assert (ba["width"], ba["height"]) == (bt["width"], bt["height"])
    assert css(page, ".r-top .theme-btn", "borderTopWidth") == css(page, ".r-top .typo-btn", "borderTopWidth")
