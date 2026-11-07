"""Sentence splitting with spaCy (model loaded lazily and cached)."""

from __future__ import annotations

import threading

_nlp = None
_lock = threading.Lock()


def _get_nlp():
    global _nlp
    with _lock:
        if _nlp is None:
            import spacy
            _nlp = spacy.load("en_core_web_sm", exclude=["ner", "lemmatizer"])
        return _nlp


def split(text: str) -> list[tuple[int, int]]:
    """Character spans (start, end) of each sentence in `text`."""
    if not text.strip():
        return []
    doc = _get_nlp()(text)
    return [(s.start_char, s.end_char) for s in doc.sents]
