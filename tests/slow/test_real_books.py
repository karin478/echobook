"""Smoke tests on real EPUBs in the folder $KOKORO_READER_SMOKE_BOOKS (skipped when unset;
real Kokoro; `-m slow`)."""

import re
from functools import cache
import os
from pathlib import Path

import pytest

from kokoro_reader.epub_extract import extract_book
from kokoro_reader.tts import KokoroSynth

BOOKS_DIR = Path(os.environ.get("KOKORO_READER_SMOKE_BOOKS") or "/nonexistent")
BOOKS = sorted(BOOKS_DIR.glob("*.epub")) if BOOKS_DIR.is_dir() else []

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not BOOKS, reason=f"no EPUBs in {BOOKS_DIR}"),
]


@cache
def _story(path: Path):
    return [c for c in extract_book(path).chapters if not c.is_matter]


def _ids(p: Path) -> str:
    return p.stem.split(" (")[0]


@pytest.mark.parametrize("path", BOOKS, ids=_ids)
def test_story_chapters_look_like_story(path):
    story = _story(path)
    assert len(story) >= 5, [c.title for c in story]
    short = [(c.title, sum(len(p) for p in c.paragraphs)) for c in story if sum(len(p) for p in c.paragraphs) <= 300]
    assert not short, short
    for c in story[:3]:
        for p in c.paragraphs:
            low = p.lower()
            assert "copyright" not in low and "all rights reserved" not in low, (c.title, p[:120])


@pytest.mark.parametrize("path", BOOKS, ids=_ids)
def test_first_story_chapter_tokens_match_text(path):
    chapter = _story(path)[0]
    samples, toks = KokoroSynth().synth_chapter(chapter.paragraphs, "af_heart")
    assert len(samples) > 0
    strip = lambda s: re.sub(r"\s+", "", s)
    for pi, para in enumerate(chapter.paragraphs):
        joined = "".join(t["w"] + t["s"] for t in toks if t["p"] == pi)
        assert strip(joined) == strip(para), (chapter.title, pi, para[:120])
