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
