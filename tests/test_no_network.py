"""The test suite must never reach OpenRouter (it costs money and sends book text out)."""

import urllib.request

import pytest

from kokoro_reader.translate import FakeTranslator, make_translator


def test_default_translator_in_tests_is_fake():
    assert isinstance(make_translator(), FakeTranslator)


def test_real_http_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="network"):
        urllib.request.urlopen("https://openrouter.ai/api/v1/chat/completions")
