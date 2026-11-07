"""Mapping between token timestamps and text positions.

`word` in an anchor is the 0-based index among a paragraph's TIMED tokens (a is not None).
"""

from __future__ import annotations

from .tts import Token


def _para_tokens(tokens: list[Token], para: int) -> list[Token]:
    return [t for t in tokens if t["p"] == para]


def display_paragraphs(paragraphs: list[str], tokens: list[Token] | None) -> list[str]:
    """Text the reader shows per paragraph; the frontend must render exactly this."""
    if not tokens:
        return list(paragraphs)
    by_para: dict[int, list[str]] = {}
    for t in tokens:
        by_para.setdefault(t["p"], []).append(t["w"] + t["s"])
    out = []
    for i, p in enumerate(paragraphs):
        parts = by_para.get(i)
        out.append("".join(parts).rstrip() if parts else p)
    return out


def token_char_spans(tokens: list[Token], para: int) -> list[tuple[int, int, int]]:
    """(chapter-wide token index, start_char, end_char) for each token of `para`, relative to its display text."""
    spans = []
    pos = 0
    for idx, t in enumerate(tokens):
        if t["p"] != para:
            continue
        spans.append((idx, pos, pos + len(t["w"])))
        pos += len(t["w"]) + len(t["s"])
    return spans


def _timed(tokens: list[Token], para: int) -> list[Token]:
    return [t for t in tokens if t["p"] == para and t["a"] is not None]


def time_for_anchor(tokens: list[Token], para: int, word: int) -> float | None:
    timed = _timed(tokens, para)
    if not timed:
        return None
    return timed[max(0, min(word, len(timed) - 1))]["a"]


def anchor_for_time(tokens: list[Token], t: float) -> tuple[int, int]:
    """(para, word) of the last timed token with a <= t; before the first, the first timed token."""
    best: Token | None = None
    first: Token | None = None
    for tok in tokens:
        if tok["a"] is None:
            continue
        if first is None:
            first = tok
        if tok["a"] <= t:
            best = tok
    target = best or first
    if target is None:
        return (0, 0)
    para = target["p"]
    word = 0
    for tok in tokens:
        if tok["p"] == para and tok["a"] is not None:
            if tok is target:
                break
            word += 1
    return (para, word)
