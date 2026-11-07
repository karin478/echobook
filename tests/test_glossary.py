from kokoro_reader import glossary as gl

BOOK = [
    "Jonathan raised his eyebrows. Jonathan's voice was low.",
    "Suddenly the Metropolitan Museum of Art went quiet.",
    "She said Jonathan would come to the Metropolitan Museum of Art.",
    "Suddenly Jonathan laughed. Later Sunny called from the Hamptons.",
    "Sunny and Jonathan left the Metropolitan Museum of Art.",
    "Sunny waved. Sunny was in the Hamptons, and the Hamptons were hot.",
    "suddenly nothing.",
]


def test_candidates_count_names_over_the_whole_book():
    cands = {t: n for t, n, _ in gl.extract_candidates(BOOK, min_count=3)}
    assert cands["Jonathan"] == 5           # possessive folded in
    assert cands["Metropolitan Museum"] == 3  # lowercase "of" ends the run
    assert cands["Hamptons"] == 3
    assert "Suddenly" not in cands          # sentence-start capital is grammar, not a name
    assert "She" not in cands


def test_candidates_carry_context_and_respect_limits():
    out = gl.extract_candidates(BOOK, min_count=3, max_candidates=1)
    assert len(out) == 1
    term, n, ctx = out[0]
    assert term == "Jonathan" and term in ctx


def test_parse_and_clean_lines():
    reply = ("```\nJonathan => Jonathan\n- Sunny → Sunny\nthe Hamptons => 汉普顿\n"
             "Sovereign => sovereign\nJonathan => 乔纳森\nnoise line\n```")
    assert gl.parse_entries(reply) == [
        {"source": "Jonathan", "zh": "Jonathan"},
        {"source": "Sunny", "zh": "Sunny"},
        {"source": "the Hamptons", "zh": "汉普顿"},
    ]


def test_relevant_picks_entries_present_in_the_text_whole_word():
    entries = [{"source": "Sunny", "zh": "Sunny"}, {"source": "Art", "zh": "艺术"},
               {"source": "Met", "zh": "大都会"}]
    rel = gl.relevant(entries, ["Sunny went to the Metropolitan.", "Nothing else."])
    assert rel == [{"source": "Sunny", "zh": "Sunny"}]


def test_prompt_block_lists_entries():
    block = gl.prompt_block([{"source": "the Met", "zh": "大都会博物馆"}])
    assert "the Met => 大都会博物馆" in block
    assert gl.prompt_block([]) == ""


class FakeGlossaryTranslator:
    def __init__(self, reply="Jonathan => Jonathan\nHamptons => 汉普顿"):
        self.reply, self.calls = reply, []

    def glossary(self, rows):
        self.calls.append(rows)
        return self.reply


def test_build_glossary_sends_candidates_once_and_parses():
    t = FakeGlossaryTranslator()
    entries = gl.build_glossary(BOOK, t, min_count=3)
    assert entries == [{"source": "Jonathan", "zh": "Jonathan"}, {"source": "Hamptons", "zh": "汉普顿"}]
    assert len(t.calls) == 1 and "Jonathan ×5" in t.calls[0]


def test_build_glossary_without_candidates_skips_the_model():
    t = FakeGlossaryTranslator()
    assert gl.build_glossary(["nothing capitalised here."], t) == []
    assert t.calls == []


# ---------------------------------------------------------------- review #6
import pytest

NAMES = ["Zorro", "Quill", "Brandt", "Okafor", "Vance", "Lucan", "Mirela", "Tobias", "Gwen", "Hollis"]
BIG_BOOK = [f"We met {n} today, and {n} smiled. Later we saw {n} again." for n in NAMES]


def test_many_candidates_but_nothing_parsed_raises():
    assert len(gl.extract_candidates(BIG_BOOK)) >= 10
    with pytest.raises(gl.GlossaryParseError):
        gl.build_glossary(BIG_BOOK, FakeGlossaryTranslator(reply="Sorry, I can't help with that."))


def test_few_candidates_and_nothing_kept_is_a_valid_empty_glossary():
    assert gl.build_glossary(BOOK, FakeGlossaryTranslator(reply="(none)"), min_count=3) == []


def test_parse_accepts_colon_lines_without_breaking_arrows():
    reply = ("Here is the glossary:\n"
             "Jonathan: Jonathan\n"
             "- Metropolitan Museum： 大都会博物馆\n"
             "Star Wars: A New Hope => 《星球大战》\n"
             "Note: these are only the names\n"
             "http://example.com\n"
             "Hamptons → 汉普顿\n")
    assert gl.parse_entries(reply) == [
        {"source": "Jonathan", "zh": "Jonathan"},
        {"source": "Metropolitan Museum", "zh": "大都会博物馆"},
        {"source": "Star Wars: A New Hope", "zh": "《星球大战》"},
        {"source": "Hamptons", "zh": "汉普顿"},
    ]
