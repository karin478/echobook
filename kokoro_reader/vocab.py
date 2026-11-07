"""Capture a selected word/phrase with its sentence and a sentence audio clip."""

from __future__ import annotations

import re
import time
import uuid

from .anchors import display_paragraphs, token_char_spans
from .audio import cut_clip
from .expand import expand
from .library import Library
from .sentences import split
from .store import Store


def _times(tokens, para: int, start: int, end: int):
    """(earliest a, latest b) of timed tokens whose span overlaps [start, end); (None, None) if none."""
    a = b = None
    for idx, s, e in token_char_spans(tokens, para):
        t = tokens[idx]
        if t["a"] is None or t["b"] is None or not (s < end and e > start):
            continue
        a = t["a"] if a is None else min(a, t["a"])
        b = t["b"] if b is None else max(b, t["b"])
    return a, b


def _strip_span(text: str, start: int, end: int) -> tuple[int, int]:
    seg = text[start:end]
    lead = len(seg) - len(seg.lstrip())
    return start + lead, end - (len(seg) - len(seg.rstrip()))


def _locate(store: Store, library: Library, book_id: str, chapter: int, para: int,
            start_char: int, end_char: int, whole: bool = False) -> dict:
    """The selection as the reader sees it: its paragraph text, trimmed span and sentence span.
    The selection is clipped to the start paragraph; ``whole`` = the selection IS the sentence."""
    book = store.get_book(book_id)
    if book is None:
        raise ValueError(f"unknown book: {book_id}")
    paragraphs = library.paragraphs(book_id, chapter)
    tokens = library.tokens(book_id, chapter)
    shown = display_paragraphs(paragraphs, tokens)
    if not 0 <= para < len(shown):
        raise ValueError(f"paragraph out of range: {para}")
    ptext = shown[para]

    start = max(0, start_char)
    end = min(end_char, len(ptext))
    if start >= end:
        raise ValueError("empty selection")
    start, end = _strip_span(ptext, start, end)
    if start >= end:
        raise ValueError("empty selection")

    if whole:
        s_start, s_end = start, end
    else:
        sent_span = next(((a, b) for a, b in split(ptext) if a <= start < b), None)
        s_start, s_end = _strip_span(ptext, *(sent_span or (0, len(ptext))))
    return {"book": book, "tokens": tokens, "ptext": ptext, "start": start, "end": end,
            "s_start": s_start, "s_end": s_end}


# 讲解逐条收藏: a ▸ head is the chunk as the model wrote it — maybe with a "(lemma)" note, sb/sth
# placeholders, straight apostrophes, or "..." between the parts of a split pattern.
_PLACEHOLDERS = {"sb", "sth", "sb's", "sth's", "someone", "somebody", "something", "one's", "oneself"}
_APOS = "['’]"


def _piece_re(piece: str) -> str | None:
    words = [w for w in piece.split() if w.lower().replace("’", "'") not in _PLACEHOLDERS]
    if not words:
        return None
    pat = r"\s+".join(_APOS.join(re.escape(x) for x in re.split(_APOS, w)) for w in words)
    return rf"(?<![\w’']){pat}(?![\w’'])"


def find_unit(sentence: str, head: str, near: tuple[int, int] | None = None):
    """Where a 讲解 unit head sits in ``sentence``: (start, end, text as in the book) or None.
    A split pattern gives the span from its first part to its last, text "a ... b". With ``near``,
    the occurrence overlapping that range wins over the first one."""
    bare = re.sub(r"\s*[(（][^)）]*[)）]", " ", head)
    parts = [r for r in (_piece_re(x) for x in re.split(r"\.\.\.|…", bare)) if r]
    if not parts:
        return None
    rx = re.compile(r"(.*?)".join(f"({x})" for x in parts), re.I | re.S)
    found = []
    pos = 0
    while (m := rx.search(sentence, pos)):
        found.append(m)
        pos = m.start() + 1
    if not found:
        return None
    m = found[0]
    if near:
        m = next((x for x in found if x.start() < near[1] and x.end() > near[0]), m)
    text = " ... ".join(m.group(2 * i + 1) for i in range(len(parts)))
    return m.start(), m.end(), text


CONTEXT_CHARS = 600  # either side of the sentence: enough to disambiguate, cheap to send


def selection_context(store: Store, library: Library, book_id: str, chapter: int, para: int,
                      start_char: int, end_char: int) -> dict:
    """What a 划词 lookup needs: the selected text (grown to whole words / the phrasal verb, with
    its new ``start``/``end``), its sentence, nearby text and the book title."""
    loc = _locate(store, library, book_id, chapter, para, start_char, end_char)
    ptext, s_start, s_end = loc["ptext"], loc["s_start"], loc["s_end"]
    start, end = loc["start"], loc["end"]
    if s_start <= start and end <= s_end:  # 智能扩选 (within one sentence): whole words / phrasal verb
        a, b = expand(ptext[s_start:s_end], start - s_start, end - s_start)
        start, end = s_start + a, s_start + b
    lo = max(0, s_start - CONTEXT_CHARS)
    return {"text": ptext[start:end], "start": start, "end": end,
            "sentence": ptext[loc["s_start"]:loc["s_end"]],
            "context": ptext[lo:loc["s_end"] + CONTEXT_CHARS],
            "title": loc["book"]["title"]}


def save_vocab(store: Store, library: Library, book_id: str, chapter: int, para: int,
               start_char: int, end_char: int, whole: bool = False,
               zh: str | None = None, gloss: str | None = None,
               gloss_mode: str | None = None, unit: str | None = None) -> dict:
    """Save a selection. ``whole``: the selection is itself the sentence unit to keep (收藏本句),
    otherwise the containing sentence is looked up. ``zh``: the Chinese of that sentence, if known.
    ``gloss``: the 划词 翻译 / 讲解 shown for the selection (``gloss_mode`` says which).
    ``unit``: the ▸ head of one 讲解 block — the entry becomes that chunk of the sentence."""
    loc = _locate(store, library, book_id, chapter, para, start_char, end_char, whole)
    book, tokens, ptext = loc["book"], loc["tokens"], loc["ptext"]
    start, end, s_start, s_end = loc["start"], loc["end"], loc["s_start"], loc["s_end"]
    audio = library.chapter_audio_path(book_id, chapter)
    converted = tokens is not None and audio.exists()
    text = ptext[start:end]
    sentence = ptext[s_start:s_end]
    if unit and unit.strip():
        hit = find_unit(sentence, unit, near=(start - s_start, end - s_start))
        if hit:
            start, end, text = s_start + hit[0], s_start + hit[1], hit[2]
        else:  # not literally in the sentence (another form of the word): keep the selection's place
            text = re.sub(r"\s*[(（][^)）]*[)）]", "", unit).strip()

    start_ts = end_ts = sent_a = sent_b = None
    if converted:
        sent_a, sent_b = _times(tokens, para, s_start, s_end)
        if sent_a is not None:
            start_ts, end_ts = _times(tokens, para, start, end)
        else:
            converted = False

    vocab_id = uuid.uuid4().hex
    clip = None
    if converted:
        dst = library.home / "vocab" / f"{vocab_id}.mp3"
        cut_clip(audio, sent_a, sent_b, dst)
        clip = str(dst)

    chapters = store.list_chapters(book_id)
    ch = next((c for c in chapters if c["idx"] == chapter), None)
    # 1-based position among story (non-matter) chapters — same numbering as the book page.
    story = [c["idx"] for c in chapters if not c["is_matter"]]
    story_number = story.index(chapter) + 1 if chapter in story else None
    rec = {
        "id": vocab_id, "kind": "sentence" if whole else "word",
        "text": text, "sentence": sentence, "zh": (zh or "").strip() or None,
        "gloss": (gloss or "").strip() or None, "gloss_mode": gloss_mode if (gloss or "").strip() else None,
        "book_id": book_id, "book_title": book["title"], "chapter": chapter, "para": para,
        "chapter_title": (ch or {}).get("title"), "story_number": story_number,
        "start_char": start, "end_char": end,
        "sentence_start_char": s_start, "sentence_end_char": s_end,
        "sentence_clip": clip,
        "start_ts": start_ts, "end_ts": end_ts,
        "sentence_start_ts": sent_a, "sentence_end_ts": sent_b,
        "voice": (ch or {}).get("voice") if clip else None,
        "created_at": time.time(), "status": "new",
    }
    try:
        store.add_vocab(rec)
    except BaseException:
        if clip:
            (library.home / "vocab" / f"{vocab_id}.mp3").unlink(missing_ok=True)
        raise
    return rec
