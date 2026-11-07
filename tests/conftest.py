import urllib.parse
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def _no_real_translation(monkeypatch, tmp_path_factory):
    """Every test gets the fake translator and no way to reach the network: the real one costs
    money and sends book text to OpenRouter. Tests of the real client patch `_chat`/`urlopen`."""
    monkeypatch.setenv("KOKORO_READER_FAKE_TRANSLATE", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("KOKORO_TRANSLATE_ENV", str(tmp_path_factory.getbasetemp() / "no-such.env"))
    # Microsoft voices are opt-in; a test that wants them sets KOKORO_READER_EDGE itself
    monkeypatch.delenv("KOKORO_READER_EDGE", raising=False)
    # and never the real Anki on this machine (AnkiConnect listens on localhost, which is allowed)
    monkeypatch.setenv("KOKORO_ANKI_URL", "http://127.0.0.1:9")

    real = urllib.request.urlopen

    def local_only(url, *a, **kw):
        full = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        host = urllib.parse.urlsplit(full).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):  # the e2e harness talks to its server
            raise AssertionError("tests must not use the network")
        return real(url, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", local_only)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_READER_HOME", str(tmp_path))
    return tmp_path
