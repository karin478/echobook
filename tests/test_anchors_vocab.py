from pathlib import Path

import pytest

from kokoro_reader.anchors import (
    anchor_for_time, display_paragraphs, time_for_anchor, token_char_spans,
)
from kokoro_reader.library import Library
from kokoro_reader.queue import ConversionQueue
from kokoro_reader.sentences import split
from kokoro_reader.store import Store
from kokoro_reader.tts import FakeSynth
from kokoro_reader.vocab import save_vocab
from tests.epub_factory import make_epub

P1 = "It is a truth. Mr. Bennet made no answer."
P2 = "“Do you not want to know?” cried his wife."
SCENE = "• • •"
P3 = "She said nothing more."


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = Store(home / "kokoro.db")
    lib = Library(home, store)
    body = "".join(f"<p>{p}</p>" for p in (P1, P2, SCENE, P3))
    epub = make_epub(tmp_path / "b.epub", title="Pride", author="A",
                     docs=[("c0.xhtml", f"<h1>Chapter 0</h1>{body}")], toc=[("Chapter 0", "c0.xhtml")])
    bid, _ = lib.import_epub(epub)
    return store, lib, bid


def convert(env):
    store, lib, bid = env
    q = ConversionQueue(store, lib, FakeSynth(), autostart=False)
    q.enqueue(bid, [0], "high")
    assert q.run_once()
    return lib.tokens(bid, 0)


def test_display_paragraphs_matches_tokens(env):
    store, lib, bid = env
    tokens = convert(env)
    paras = lib.paragraphs(bid, 0)
    assert SCENE in paras
    shown = display_paragraphs(paras, tokens)
    assert len(shown) == len(paras)
    for i, p in enumerate(paras):
        expect = "".join(t["w"] + t["s"] for t in tokens if t["p"] == i).rstrip()
        if expect:
            assert shown[i] == expect
    scene_i = paras.index(SCENE)
    assert shown[scene_i] == SCENE  # no tokens -> falls back to original paragraph


def test_display_paragraphs_without_tokens():
    assert display_paragraphs(["a b", "c"], None) == ["a b", "c"]


def test_token_char_spans_index_into_display_text(env):
    store, lib, bid = env
    tokens = convert(env)
    shown = display_paragraphs(lib.paragraphs(bid, 0), tokens)
    for para in range(len(shown)):
        for idx, a, b in token_char_spans(tokens, para):
            assert tokens[idx]["p"] == para
            assert shown[para][a:b] == tokens[idx]["w"]


def test_anchor_time_roundtrip(env):
    store, lib, bid = env
    tokens = convert(env)
    checked = 0
    a_counts = {}
    for t in tokens:
        if t["a"] is not None:
            a_counts[t["a"]] = a_counts.get(t["a"], 0) + 1
    for para in sorted({t["p"] for t in tokens}):
        timed = [t for t in tokens if t["p"] == para and t["a"] is not None]
        for word, t in enumerate(timed):
            if a_counts[t["a"]] > 1:  # zero-length punctuation shares a start with its neighbour
                continue
            ts = time_for_anchor(tokens, para, word)
            assert ts == t["a"]
            assert anchor_for_time(tokens, ts) == (para, word)
            checked += 1
    assert checked > 10


def test_anchor_edge_cases(env):
    store, lib, bid = env
    tokens = convert(env)
    scene_i = lib.paragraphs(bid, 0).index(SCENE)
    assert time_for_anchor(tokens, scene_i, 0) is None
    last = [t for t in tokens if t["p"] == 0 and t["a"] is not None][-1]
    assert time_for_anchor(tokens, 0, 999) == last["a"]
    assert anchor_for_time(tokens, -5.0) == (0, 0)
    assert anchor_for_time(tokens, 1e6) == (3, len([t for t in tokens if t["p"] == 3 and t["a"] is not None]) - 1)


def test_split_sentences():
    text = "Mr. Bennet replied. She left."
    spans = split(text)
    assert [text[a:b].strip() for a, b in spans] == ["Mr. Bennet replied.", "She left."]


def _idx(env, p):
    return env[1].paragraphs(env[2], 0).index(p)


def test_save_vocab_converted_has_clip(env):
    store, lib, bid = env
    convert(env)
    para = _idx(env, P1)
    s = P1.index("no answer")
    rec = save_vocab(store, lib, bid, 0, para, s, s + len("no answer"))
    assert rec["text"] == "no answer"
    assert rec["sentence"] == "Mr. Bennet made no answer."
    assert rec["book_title"] == "Pride" and rec["book_id"] == bid
    assert rec["chapter"] == 0 and rec["para"] == para and rec["status"] == "new"
    assert rec["sentence_start_char"] == P1.index("Mr.") and rec["sentence_end_char"] == len(P1)
    assert rec["voice"] == store.list_chapters(bid)[0]["voice"]
    assert Path(rec["sentence_clip"]).is_file()
    assert Path(rec["sentence_clip"]) == lib.home / "vocab" / f"{rec['id']}.mp3"
    assert rec["sentence_start_ts"] < rec["start_ts"] < rec["end_ts"] <= rec["sentence_end_ts"]
    assert store.get_vocab(rec["id"])["sentence"] == rec["sentence"]


def test_save_vocab_unconverted_no_clip(env):
    store, lib, bid = env
    para = _idx(env, P1)
    s = P1.index("no answer")
    rec = save_vocab(store, lib, bid, 0, para, s, s + 9)
    assert rec["sentence"] == "Mr. Bennet made no answer."
    assert rec["sentence_clip"] is None and rec["voice"] is None
    for k in ("start_ts", "end_ts", "sentence_start_ts", "sentence_end_ts"):
        assert rec[k] is None
    assert not (lib.home / "vocab").exists() or not list((lib.home / "vocab").iterdir())


def test_vocab_selection_clipped_to_start_paragraph(env):
    store, lib, bid = env
    para = _idx(env, P1)
    s = P1.index("answer")
    rec = save_vocab(store, lib, bid, 0, para, s, 10_000)
    assert rec["text"] == "answer."
    assert rec["end_char"] == len(P1)


def test_save_vocab_empty_selection_raises(env):
    store, lib, bid = env
    para = _idx(env, P1)
    with pytest.raises(ValueError):
        save_vocab(store, lib, bid, 0, para, 5, 5)
    with pytest.raises(ValueError):
        save_vocab(store, lib, bid, 0, para, 9, 3)
    with pytest.raises(ValueError):
        save_vocab(store, lib, bid, 0, para, 100, 200)  # beyond paragraph end
    with pytest.raises(ValueError):
        save_vocab(store, lib, bid, 0, para, 2, 3)  # whitespace only ("It is": index 2 is ' ')
    assert store.list_vocab() == []


def test_save_vocab_stores_chapter_title_and_story_number(tmp_path):
    """Matter chapters are not counted, so the vocab label matches the book page's 'Ch N'."""
    home = tmp_path / "home"
    home.mkdir()
    store = Store(home / "kokoro.db")
    lib = Library(home, store)
    docs = [("copyright.xhtml", "<h1>Copyright</h1><p>All rights reserved.</p>"),
            ("c1.xhtml", f"<h1>Chapter One</h1><p>{P1}</p>"),
            ("c2.xhtml", f"<h1>Chapter Two</h1><p>{P3}</p>")]
    toc = [("Copyright", "copyright.xhtml"), ("Chapter One", "c1.xhtml"), ("Chapter Two", "c2.xhtml")]
    bid, _ = lib.import_epub(make_epub(tmp_path / "m.epub", title="Pride", author="A", docs=docs, toc=toc))
    store.set_chapter_matter(bid, 0, True)
    for i in (1, 2):
        store.set_chapter_matter(bid, i, False)

    para = lib.paragraphs(bid, 2).index(P3)
    rec = save_vocab(store, lib, bid, 2, para, 0, len("She"))
    assert rec["chapter"] == 2
    assert rec["story_number"] == 2
    assert rec["chapter_title"] == "Chapter Two"
    saved = store.get_vocab(rec["id"])
    assert saved["story_number"] == 2 and saved["chapter_title"] == "Chapter Two"

    para0 = lib.paragraphs(bid, 0).index("All rights reserved.")
    rec0 = save_vocab(store, lib, bid, 0, para0, 0, len("All"))
    assert rec0["story_number"] is None
    assert rec0["chapter_title"] == "Copyright"


def test_save_whole_sentence_unit_with_chinese(env):
    """收藏本句: the selection IS the sentence (a unit may span several sentences); Chinese kept."""
    store, lib, bid = env
    convert(env)
    para = _idx(env, P1)
    rec = save_vocab(store, lib, bid, 0, para, 0, len(P1), whole=True, zh="这是真理。班纳特先生没有回答。")
    assert rec["kind"] == "sentence"
    assert rec["text"] == rec["sentence"] == P1  # both sentences, not just the first
    assert rec["zh"] == "这是真理。班纳特先生没有回答。"
    assert rec["sentence_start_char"] == 0 and rec["sentence_end_char"] == len(P1)
    assert Path(rec["sentence_clip"]).is_file()
    assert rec["sentence_start_ts"] == rec["start_ts"] and rec["sentence_end_ts"] == rec["end_ts"]
    assert store.get_vocab(rec["id"])["zh"] == rec["zh"]


def test_save_word_keeps_chinese_of_its_sentence(env):
    store, lib, bid = env
    para = _idx(env, P1)
    s = P1.index("no answer")
    rec = save_vocab(store, lib, bid, 0, para, s, s + 9, zh="班纳特先生没有回答。")
    assert rec["kind"] == "word" and rec["zh"] == "班纳特先生没有回答。"
    plain = save_vocab(store, lib, bid, 0, para, s, s + 9)
    assert plain["zh"] is None
