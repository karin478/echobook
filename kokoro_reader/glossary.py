"""Whole-book glossary so recurring names and terms read the same in every chapter.

Ported from the glossary pre-pass of an earlier EPUB-translation tool: capitalised proper-noun candidates are
counted over the WHOLE book locally (no API cost), then the top candidates with one line of
context each go to the model in ONE call, which answers `source => 中文` lines. Each translation
batch then gets only the entries that occur in it.

Unlike that tool, people's and places' names stay in English (the reader is learning to
recognise them in the English text); other terms (titles, organisations, nicknames, foreign
words, invented things) get one fixed Chinese rendering.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Protocol

WORD_RE = re.compile(r"[^\W\d_][\w'’\-]*")
SENTENCE_BREAK = set('.!?…:;"“”‘(—')
# Capitalised for grammar, not because they are names: they break runs and are never candidates.
STOP = set("""
I I'm I'd I'll I've A An The And But Or Nor So Yet For Of In On At To By From With
As If Then Than When While Where Why How What Who Whom Whose Which That This These
Those There Here He She It We You They Me Him Her Us Them My His Its Our Your
Their Mine Yours Ours Theirs Is Was Are Were Be Been Am Do Does Did Have Has Had
Not No Yes Oh Ah OK Okay Well Now Just Only Even Still Also Too Very All Any Some
Each Every Both Either Neither One Two Another Other Such Most More Many Much
Mr Mrs Ms Dr Sir Madam Lord Lady Chapter Part Book
""".split())

MIN_COUNT = 3
MAX_CANDIDATES = 600
CONTEXT_CHARS = 60
# this many candidates and not one parsable line means the reply was junk, not "nothing to keep"
MIN_CANDIDATES_EXPECTING_ENTRIES = 10


class GlossaryParseError(ValueError):
    """The model's glossary reply could not be read (not saved, so the next try builds again)."""


class GlossaryModel(Protocol):
    def glossary(self, rows: str) -> str: ...


def _runs(paragraph: str):
    """(term, start, end, at_sentence_start) for each run of capitalised non-stop words."""
    run, run_start, run_sent = [], 0, False
    prev_end = 0
    for m in WORD_RE.finditer(paragraph):
        tok = m.group().replace("’", "'")
        if tok.endswith("'s"):          # possessive: Sunny's → Sunny
            tok = tok[:-2]
        tok = tok.rstrip("'-")
        gap = paragraph[prev_end:m.start()]
        sent_start = prev_end == 0 or any(c in SENTENCE_BREAK for c in gap)
        is_cap = tok[:1].isupper() and tok not in STOP and len(tok) > 1
        if run and (not is_cap or gap != " "):
            yield " ".join(t for t, _ in run), run_start, run[-1][1], run_sent
            run = []
        if is_cap:
            if not run:
                run_start, run_sent = m.start(), sent_start
            run.append((tok, m.start() + len(tok)))
        prev_end = m.end()
    if run:
        yield " ".join(t for t, _ in run), run_start, run[-1][1], run_sent


def extract_candidates(paragraphs: list[str], min_count: int = MIN_COUNT,
                       max_candidates: int = MAX_CANDIDATES) -> list[tuple[str, int, str]]:
    """[(term, count, context)], most frequent first. A lone capitalised word at a sentence
    start is grammar ("Suddenly") unless the same word also shows up capitalised mid-sentence
    (then it is a name and every occurrence counts); a run at sentence start sheds a first word that also occurs
    in lowercase ("Later Sunny" → "Sunny")."""
    paragraphs = [p for p in paragraphs if p and p.strip()]
    lower_vocab = {w.lower() for p in paragraphs for w in WORD_RE.findall(p) if w[:1].islower()}
    counts: Counter = Counter()
    initial: Counter = Counter()  # lone capitalised words at a sentence start
    first: dict[str, tuple[int, str]] = {}
    for p in paragraphs:
        for term, start, end, at_start in _runs(p):
            words = term.split(" ")
            if at_start:
                if len(words) == 1:
                    initial[term] += 1
                    continue
                if words[0].lower() in lower_vocab:
                    words = words[1:]
                    start = p.find(words[0], start)
                    term = " ".join(words)
            counts[term] += 1
            if term not in first:
                lo = max(0, start - CONTEXT_CHARS)
                first[term] = (len(first), p[lo:end + CONTEXT_CHARS].strip()[:160])
    for term, n in initial.items():  # "Jonathan raised…" counts once Jonathan is known as a name
        if term in counts:
            counts[term] += n
    ranked = sorted((t for t, n in counts.items() if n >= min_count),
                    key=lambda t: (-counts[t], first[t][0]))
    return [(t, counts[t], first[t][1]) for t in ranked[:max_candidates]]


LINE_RE = re.compile(r"^\s*(?:[-*•]\s*)?(.+?)\s*(?:=>|→)\s*(.+?)\s*$")
# `source: zh` fallback (source has no colon). Only trusted when the rendering is Chinese or an
# echo of the source, so prose like "Note: these are names" or URLs never become entries.
COLON_RE = re.compile(r"^\s*(?:[-*•]\s*)?([^:：]+?)\s*[:：]\s*(.+?)\s*$")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def parse_entries(reply: str) -> list[dict]:
    """`source => zh` (or `source: 中文`) lines → entries. Drops case-only echoes (the model gave up) and repeats
    (first wins); a verbatim echo is a deliberate keep-in-English."""
    seen, out = set(), []
    for line in reply.splitlines():
        m = LINE_RE.match(line)
        colon = m is None
        m = m or COLON_RE.match(line)
        if not m:
            continue
        src, zh = m.group(1).strip("`\"' "), m.group(2).strip("`\"' ")
        if colon and zh != src and not CJK_RE.search(zh):
            continue
        if not src or not zh or src in seen or (zh != src and zh.lower() == src.lower()):
            continue
        seen.add(src)
        out.append({"source": src, "zh": zh})
    return out


def candidate_rows(cands: list[tuple[str, int, str]]) -> str:
    return "\n".join(f"{t} ×{n} | {ctx}" for t, n, ctx in cands)


def build_glossary(paragraphs: list[str], model: GlossaryModel, min_count: int = MIN_COUNT) -> list[dict]:
    cands = extract_candidates(paragraphs, min_count=min_count)
    if not cands:
        return []
    entries = parse_entries(model.glossary(candidate_rows(cands)))
    if not entries and len(cands) >= MIN_CANDIDATES_EXPECTING_ENTRIES:
        raise GlossaryParseError(f"glossary reply had no readable entries for {len(cands)} candidates")
    return entries


def relevant(entries: list[dict], texts: list[str]) -> list[dict]:
    """Entries whose source occurs as a whole word/phrase in any of `texts`."""
    blob = "\n".join(texts)
    return [e for e in entries
            if re.search(r"(?<![\w])" + re.escape(e["source"]) + r"(?![\w])", blob)]


def prompt_block(entries: list[dict]) -> str:
    if not entries:
        return ""
    return ("Glossary for this book — wherever a word below is used as this name or term, render "
            "it exactly like this so it reads the same in every chapter (a rendering equal to the "
            "source means keep it in English; ordinary uses of the same word translate normally):\n"
            + "\n".join(f"{e['source']} => {e['zh']}" for e in entries))
