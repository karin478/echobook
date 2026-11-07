"""智能扩选: grow a 划词 selection to whole words, then to the whole phrasal verb it belongs to.

Local only. True particles come from spaCy's parse (`prt`: turned *out*, gave it *up*). Verbs
with a preposition (look *after*, run *into*) parse the same as literal uses (looked *at* the
sky), so those come from a short list of common ones, matched on the verb's lemma.
"""

from __future__ import annotations

import re
import threading

MAX_WORDS = 4   # longer selections are the reader's own choice: only snapped to words
MAX_GAP = 3     # tokens allowed between a verb and its particle (gave *it* up, pick *you* up)

# prepositional / three-part verbs whose second (and third) word the parse can't tell from a literal preposition
LEXICON: set[tuple[str, ...]] = {tuple(e.split()) for e in """
look after|look into|look for|look up to|look down on|look forward to|run into|run out of|come across|
come up with|come down with|carry on|go on|go through|go over|get over|get away with|get along with|
get on with|get rid of|put up with|take after|deal with|rely on|depend on|count on|stick to|care for|
keep up with|catch up with|cut down on|make up for|make do with|run across|bump into|fall for|
call for|call on|come by|come into|get at|get through|go for|go without|hear from|hear of|
hold on to|lead to|live on|live up to|long for|part with|see to|settle for|stand for|stand by|
take to|talk into|think of|turn to|wait on|walk out on|wonder at|zero in on
""".replace("\n", "").split("|") if e.strip()}

_WORD = re.compile(r"[\w’'-]")
_nlp = None
_lock = threading.Lock()


def _get_nlp():
    global _nlp
    with _lock:
        if _nlp is None:
            import spacy
            _nlp = spacy.load("en_core_web_sm", exclude=["ner"])  # keeps the lemmatizer
        return _nlp


def _snap(s: str, a: int, b: int) -> tuple[int, int]:
    while a < b and s[a].isspace():
        a += 1
    while b > a and s[b - 1].isspace():
        b -= 1
    while 0 < a < len(s) and _WORD.match(s[a - 1]) and _WORD.match(s[a]):
        a -= 1
    while 0 < b < len(s) and _WORD.match(s[b - 1]) and _WORD.match(s[b]):
        b += 1
    return a, b


def _lexicon_span(doc, verb) -> int | None:
    """Index of the last token of a listed prepositional verb starting at `verb`, if any."""
    lemma = verb.lemma_.lower()
    for n in (3, 2):
        words = [t.lower_ for t in doc[verb.i + 1: verb.i + n]]
        if len(words) == n - 1 and (lemma, *words) in LEXICON:
            return verb.i + n - 1
    return None


def expand(sentence: str, start: int, end: int) -> tuple[int, int]:
    """[start, end) inside `sentence` → the grown range (same coordinates)."""
    a, b = _snap(sentence, start, end)
    if a >= b or len(sentence[a:b].split()) > MAX_WORDS:
        return a, b
    doc = _get_nlp()(sentence)
    sel = [t for t in doc if t.idx < b and t.idx + len(t) > a and not t.is_space]
    if not sel:
        return a, b
    lo, hi = sel[0].i, sel[-1].i
    verbs = set()
    for t in sel:
        if t.pos_ in ("VERB", "AUX"):
            verbs.add(t)
        if t.dep_ in ("prt", "prep") and t.head.pos_ in ("VERB", "AUX") and 0 < t.i - t.head.i <= MAX_GAP:
            verbs.add(t.head)  # the selection is the particle / preposition: reach back to the verb
    for v in verbs:
        prts = [c.i for c in v.children if c.dep_ == "prt" and 0 < c.i - v.i <= MAX_GAP]
        last = max(prts) if prts else None
        listed = _lexicon_span(doc, doc[last] if last is not None else v)
        if last is not None and listed is not None:
            last = listed  # come *up with*: particle, then the listed preposition
        elif last is not None and (v.lemma_.lower(), *[t.lower_ for t in doc[v.i + 1: last + 2]]) in LEXICON:
            last += 1      # put up *with*: the parse marks "up" prt; the list adds "with"
        elif last is None:
            last = _lexicon_span(doc, v)
        if last is None:
            continue
        if not (v.i <= hi and last >= lo) and not (lo <= v.i <= hi or lo <= last <= hi):
            continue
        lo, hi = min(lo, v.i), max(hi, last)
    return doc[lo].idx, doc[hi].idx + len(doc[hi])
