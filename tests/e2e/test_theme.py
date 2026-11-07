"""浅色 / 深色: follows the system until the reader picks one; the pick is remembered on every page."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_reader import book, open_reader, reader_epub  # noqa: F401

pytestmark = pytest.mark.e2e

LIGHT_BG = "rgb(247, 243, 236)"
DARK_BG = "rgb(22, 21, 19)"
BG = "getComputedStyle(document.body).backgroundColor"


def toggle(page: Page):
    return page.get_by_role("button", name="切换浅色 / 深色")


def test_theme_follows_system_until_picked_and_is_remembered(page: Page, live_server, book):
    page.emulate_media(color_scheme="dark")
    page.goto(f"{live_server.url}/shelf.html")
    assert page.evaluate(BG) == DARK_BG
    toggle(page).click()
    assert page.evaluate(BG) == LIGHT_BG  # system is dark, the reader chose light
    page.reload()
    assert page.evaluate(BG) == LIGHT_BG
    for path in ("vocab.html", f"book.html?id={book}"):
        page.goto(f"{live_server.url}/{path}")
        expect(toggle(page)).to_be_visible()
        assert page.evaluate(BG) == LIGHT_BG
    open_reader(page, live_server, book, 0, playable=False)
    expect(toggle(page)).to_be_visible()
    assert page.evaluate(BG) == LIGHT_BG
    toggle(page).click()
    assert page.evaluate(BG) == DARK_BG
    page.emulate_media(color_scheme="light")
    page.reload()
    assert page.evaluate(BG) == DARK_BG  # an explicit pick beats the system
