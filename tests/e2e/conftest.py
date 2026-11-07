"""Playwright E2E fixtures: a real `python -m kokoro_reader` (FakeSynth) on a random port + system Chrome."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.epub_factory import make_epub

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"

STORY = ("It was a bright cold day in April, and the clocks were striking thirteen. "
         "Winston Smith slipped quickly through the glass doors of Victory Mansions. ") * 3
FIRST_LINES = [
    "The first chapter opens on a quiet street.",
    "Morning came slowly to the harbour town.",
    "Nobody expected the letter to arrive.",
    "The storm broke just after midnight.",
    "In the end, they all went home.",
]


# ---- browser: system Chrome, headless, never downloaded ----
@pytest.fixture(scope="session")
def browser_channel():
    return "chrome"


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, browser_channel):
    return {**browser_type_launch_args, "channel": browser_channel}


# ---- server ----
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, url: str, home: Path):
        self.url = url
        self.home = home

    def api(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def upload(self, path: Path) -> dict:
        boundary = "kokoro-e2e-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/epub+zip\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/api/books", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["book"]

    def wait_chapter(self, book_id: str, idx: int, status: str, timeout: float = 15) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            ch = next(c for c in self.api("GET", f"/api/books/{book_id}")["chapters"] if c["idx"] == idx)
            if ch["status"] == status:
                return ch
            time.sleep(0.1)
        raise AssertionError(f"chapter {idx} never reached {status!r}")


@pytest.fixture
def live_server_env() -> dict:
    """Extra env vars for the server process; override with
    `@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_FAKE_DELAY": "1"}])`."""
    return {}


@pytest.fixture
def live_server(tmp_path, live_server_env):
    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    env = {**os.environ, "KOKORO_READER_FAKE_TTS": "1", "KOKORO_READER_HOME": str(home),
           "KOKORO_READER_PORT": str(port),
           # the reader always loads sentences (逐句 is the only mode): never call a real model
           "KOKORO_READER_FAKE_TRANSLATE": "1", **live_server_env}
    log = open(tmp_path / "server.log", "wb")
    proc = subprocess.Popen([str(PYTHON), "-m", "kokoro_reader"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while True:
            if proc.poll() is not None:
                raise RuntimeError("server exited:\n" + (tmp_path / "server.log").read_text())
            try:
                with urllib.request.urlopen(url + "/api/books", timeout=1) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.time() > deadline:
                raise RuntimeError("server did not start:\n" + (tmp_path / "server.log").read_text())
            time.sleep(0.1)
        yield LiveServer(url, home)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


# ---- books ----
def build_book(path: Path, title: str, author: str = "Test Author", cover: bytes | None = None) -> Path:
    """6 TOC entries: a Copyright page (non-story) followed by 5 story chapters."""
    docs = [("copyright.xhtml", "<h1>Copyright</h1><p>All rights reserved.</p>")]
    toc = [("Copyright", "copyright.xhtml")]
    for i, first in enumerate(FIRST_LINES, start=1):
        docs.append((f"c{i}.xhtml", f"<h1>Chapter {i}</h1><p>{first}</p><p>{STORY}</p>"))
        toc.append((f"Chapter {i}", f"c{i}.xhtml"))
    return make_epub(path, title=title, author=author, docs=docs, toc=toc, cover=cover)


@pytest.fixture
def sample_epub(tmp_path) -> Path:
    return build_book(tmp_path / "sample.epub", "The Sample Book", "Ada Writer")


@pytest.fixture
def make_book(tmp_path):
    def _make(title: str, author: str = "Test Author") -> Path:
        return build_book(tmp_path / f"{title.replace(' ', '_')}.epub", title, author)
    return _make
