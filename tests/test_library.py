import json

import pytest

import kokoro_reader.library as libmod
from kokoro_reader.epub_extract import EpubError
from kokoro_reader.library import Library
from kokoro_reader.store import Store
from tests.epub_factory import make_epub

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
LONG = "This is an ordinary sentence of the story that keeps going. " * 8


@pytest.fixture
def lib(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Library(home, Store(home / "kokoro.db"))


@pytest.fixture
def epub(tmp_path):
    return make_epub(
        tmp_path / "b.epub",
        title="My Novel",
        author="Jane Doe",
        cover=JPEG,
        docs=[
            ("c1.xhtml", f"<h1>Chapter 1</h1><p>{LONG}</p><p>Second para.</p>"),
            ("c2.xhtml", f"<h1>Chapter 2</h1><p>{LONG}</p>"),
        ],
        toc=[("Chapter 1", "c1.xhtml"), ("Chapter 2", "c2.xhtml")],
    )


def test_import_creates_files_and_rows(lib, epub):
    bid, created = lib.import_epub(epub)
    assert created is True
    d = lib.book_dir(bid)
    assert (d / "source.epub").exists()
    data = json.loads((d / "text.json").read_text(encoding="utf-8"))
    assert len(data["chapters"]) == 2
    assert set(data["chapters"][0]) == {"title", "part", "paragraphs"}
    chs = lib.store.list_chapters(bid)
    assert len(chs) == 2
    assert [c["idx"] for c in chs] == [0, 1]
    assert all(c["status"] == "none" for c in chs)
    paras = data["chapters"][0]["paragraphs"]
    assert chs[0]["chars"] == sum(len(p) for p in paras)
    assert chs[0]["first_line"] == paras[0][:60]
    assert lib.store.get_book(bid)["title"] == "My Novel"
    assert lib.cover_path(bid).read_bytes() == JPEG
    assert not [p for p in (lib.home / "books").iterdir() if p.name.startswith(".tmp-")]


def test_import_duplicate_returns_existing(lib, epub):
    a, c1 = lib.import_epub(epub)
    b, c2 = lib.import_epub(epub)
    assert (a, c1, c2) == (b, True, False)
    assert len(lib.store.list_books()) == 1


def test_import_corrupt_leaves_nothing(lib, tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not an epub")
    with pytest.raises(EpubError):
        lib.import_epub(bad)
    books = lib.home / "books"
    assert not books.exists() or list(books.iterdir()) == []
    assert lib.store.list_books() == []


def test_import_db_failure_cleans_rows_and_tmp(lib, epub, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(lib.store, "add_chapters", boom)
    with pytest.raises(RuntimeError):
        lib.import_epub(epub)
    assert lib.store.list_books() == []
    assert list((lib.home / "books").iterdir()) == []


def test_import_rename_failure_cleans_rows_and_tmp(lib, epub, monkeypatch):
    def boom(self, target):
        raise OSError("rename failed")

    monkeypatch.setattr(libmod.Path, "rename", boom)
    with pytest.raises(OSError):
        lib.import_epub(epub)
    monkeypatch.undo()
    assert lib.store.list_books() == []
    assert list((lib.home / "books").iterdir()) == []


def test_delete_book_removes_dir_and_vocab_clips(lib, epub):
    bid, _ = lib.import_epub(epub)
    other, _ = lib.store.add_book("o", "a", "x"), None
    vid = lib.store.add_vocab({"book_id": bid, "word": "w"})
    ovid = lib.store.add_vocab({"book_id": other, "word": "o"})
    vdir = lib.home / "vocab"
    vdir.mkdir()
    (vdir / f"{vid}.mp3").write_bytes(b"x")
    (vdir / f"{ovid}.mp3").write_bytes(b"x")
    lib.delete_book(bid)
    assert not lib.book_dir(bid).exists()
    assert lib.store.get_book(bid) is None
    assert not (vdir / f"{vid}.mp3").exists()
    assert (vdir / f"{ovid}.mp3").exists()
    assert lib.store.list_chapters(bid) == []


def test_paragraphs_roundtrip(lib, epub):
    bid, _ = lib.import_epub(epub)
    assert lib.paragraphs(bid, 1) == [LONG.strip()]
    assert lib.paragraphs(bid, 0)[-1] == "Second para."


def test_chapter_paths_and_tokens(lib, epub):
    bid, _ = lib.import_epub(epub)
    assert lib.chapter_audio_path(bid, 3) == lib.book_dir(bid) / "ch" / "3.mp3"
    assert lib.chapter_tokens_path(bid, 3) == lib.book_dir(bid) / "ch" / "3.json"
    assert lib.tokens(bid, 0) is None
    p = lib.chapter_tokens_path(bid, 0)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([{"w": "hi"}]), encoding="utf-8")
    assert lib.tokens(bid, 0) == [{"w": "hi"}]


def test_cover_path_none_without_cover(lib, tmp_path):
    p = make_epub(tmp_path / "n.epub", title="N", author="A",
                  docs=[("c1.xhtml", f"<h1>Chapter 1</h1><p>{LONG}</p>")],
                  toc=[("Chapter 1", "c1.xhtml")])
    bid, _ = lib.import_epub(p)
    assert lib.cover_path(bid) is None
