"""The app's icon: one SVG, used as every page's tab icon (favicon) and shown on the 书架."""

import re

from fastapi.testclient import TestClient

from kokoro_reader.app import STATIC_DIR, create_app
from tests.test_api import BASE, CountingSynth

PAGES = ["shelf.html", "book.html", "reader.html", "vocab.html"]


def test_every_page_has_the_svg_tab_icon_and_the_shelf_shows_it(tmp_path):
    app = create_app(tmp_path, CountingSynth(), start_worker=False)
    with TestClient(app, base_url=BASE) as client:
        r = client.get("/icon.svg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert r.text.lstrip().startswith("<svg") and "viewBox" in r.text
        for page in PAGES:
            html = client.get(f"/{page}").text
            assert re.search(r'<link rel="icon" href="icon\.svg[^"]*" type="image/svg\+xml">', html), page
        assert re.search(r'<img class="logo" src="icon\.svg', client.get("/shelf.html").text)
        for page in PAGES:  # the product is called Echobook (回声书); kokoro is the TTS engine
            html = client.get(f"/{page}").text
            assert re.search(r"<title>[^<]*Echobook</title>", html) and "Kokoro 有声书" not in html, page
        assert "Echobook" in client.get("/shelf.html").text.split('class="brand"')[1][:200]
    assert (STATIC_DIR / "icon.svg").stat().st_size < 4000  # hand-drawn, not an embedded bitmap
