"""智能扩选: a 划词 selection grows to whole words and to the whole phrasal verb it is part of,
locally (spaCy parse + a small list of prepositional verbs) — no model call."""

import pytest

from kokoro_reader.expand import expand


def pick(sentence: str, part: str, nth: int = 0) -> tuple[int, int]:
    at = -1
    for _ in range(nth + 1):
        at = sentence.index(part, at + 1)
    return at, at + len(part)


def grown(sentence: str, part: str) -> str:
    a, b = expand(sentence, *pick(sentence, part))
    return sentence[a:b]


@pytest.mark.parametrize("sentence, part, want", [
    # a partial word becomes the word
    ("She called again, louder this time.", "loud", "louder"),
    ("She called again, louder this time.", "oude", "louder"),
    ("I’d never seen anything quite like it.", "d neve", "I’d never"),
    # particles (spaCy: prt), on either side, adjacent or split by the object
    ("It turned out that she had been there all along.", "turned", "turned out"),
    ("It turned out that she had been there all along.", "out", "turned out"),
    ("She gave it up after a week.", "gave", "gave it up"),
    ("I'll pick you up at six.", "up", "pick you up"),
    ("Jutting out from the dense green canopy was a pillar of rock.", "Jutting", "Jutting out"),
    # prepositional verbs from the list, any tense
    ("He looked after the kids all summer.", "looked", "looked after"),
    ("She ran into an old friend.", "ran", "ran into"),
    ("She ran into an old friend.", "into", "ran into"),
    ("They carried on talking.", "carried", "carried on"),
    ("I can't put up with this any longer.", "put", "put up with"),
    ("Don't let him get away with it.", "get", "get away with"),
    # literal uses and long selections stay as they are
    ("He looked at the sky.", "looked", "looked"),
    ("She ran into the house.", "house", "house"),
    ("It turned out that she had been there all along.", "she had been there all along", "she had been there all along"),
])
def test_expand(sentence, part, want):
    assert grown(sentence, part) == want


def test_expand_trims_spaces_and_keeps_offsets_in_the_sentence():
    s = "It turned out fine."
    assert expand(s, 2, 10) == (3, 13)  # " turned " → "turned out"
