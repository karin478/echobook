"""Book library: EPUB import (atomic book folders) and deletion.

Layout under `<home>/books/<book_id>/`:
    source.epub, cover.<ext>, text.json, ch/<idx>.mp3, ch/<idx>.json
Vocab clips live at `<home>/vocab/<vocab_id>.mp3`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

from .epub_extract import extract_book
from .store import Store


class Library:
    def __init__(self, home: Path, store: Store):
        self.home = Path(home)
        self.store = store

    # ---- paths ----
    @property
    def _books(self) -> Path:
        return self.home / "books"

    def book_dir(self, book_id: str) -> Path:
        return self._books / book_id

    def chapter_audio_path(self, book_id: str, idx: int) -> Path:
        return self.book_dir(book_id) / "ch" / f"{idx}.mp3"

    def chapter_tokens_path(self, book_id: str, idx: int) -> Path:
        return self.book_dir(book_id) / "ch" / f"{idx}.json"

    def translation_path(self, book_id: str, idx: int) -> Path:
        return self.book_dir(book_id) / "zh" / f"{idx}.json"

    def translation(self, book_id: str, idx: int) -> dict | None:
        p = self.translation_path(book_id, idx)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def glossary_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "zh" / "glossary.json"

    def glossary(self, book_id: str) -> list[dict] | None:
        p = self.glossary_path(book_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8")).get("entries") or []

    def save_glossary(self, book_id: str, entries: list[dict], model: str = "") -> None:
        p = self.glossary_path(book_id)
        p.parent.mkdir(exist_ok=True)  # FileNotFoundError if the book folder is gone
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"model": model, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def save_translation(self, book_id: str, idx: int, data: dict) -> None:
        """Atomic write; never recreates a deleted book's folder."""
        p = self.translation_path(book_id, idx)
        p.parent.mkdir(exist_ok=True)  # FileNotFoundError if the book folder is gone
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def cover_path(self, book_id: str) -> Path | None:
        for p in sorted(self.book_dir(book_id).glob("cover.*")):
            return p
        return None

    # ---- reading ----
    def paragraphs(self, book_id: str, idx: int) -> list[str]:
        data = json.loads((self.book_dir(book_id) / "text.json").read_text(encoding="utf-8"))
        return data["chapters"][idx]["paragraphs"]

    def tokens(self, book_id: str, idx: int):
        p = self.chapter_tokens_path(book_id, idx)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    # ---- import / delete ----
    def import_epub(self, src: Path) -> tuple[str, bool]:
        src = Path(src)
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        existing = self.store.find_book_by_hash(sha)
        if existing:
            return existing["id"], False

        book = extract_book(src)  # raises EpubError before anything is written

        self._books.mkdir(parents=True, exist_ok=True)
        tmp = self._books / f".tmp-{uuid.uuid4().hex}"
        book_id = None
        try:
            tmp.mkdir()
            shutil.copyfile(src, tmp / "source.epub")
            if book.cover:
                ext = (book.cover_ext or "jpg").lstrip(".")
                (tmp / f"cover.{ext}").write_bytes(book.cover)
            text = {
                "chapters": [
                    {"title": c.title, "part": c.part, "paragraphs": c.paragraphs}
                    for c in book.chapters
                ]
            }
            (tmp / "text.json").write_text(
                json.dumps(text, ensure_ascii=False), encoding="utf-8"
            )

            book_id = self.store.add_book(book.title, book.author, sha)
            self.store.add_chapters(
                book_id,
                [
                    {
                        "idx": i,
                        "title": c.title,
                        "part": c.part,
                        "is_matter": c.is_matter,
                        "chars": sum(len(p) for p in c.paragraphs),
                        "first_line": c.paragraphs[0][:60] if c.paragraphs else "",
                    }
                    for i, c in enumerate(book.chapters)
                ],
            )
            tmp.rename(self.book_dir(book_id))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            if book_id is not None:
                self.store.delete_book(book_id)
            raise
        return book_id, True

    def delete_book(self, book_id: str) -> None:
        """The DB row goes first, so a conversion or translation still running sees the book is
        gone before its folder disappears (and never recreates it). 生词本 entries and their
        sentence clips (in <home>/vocab) stay."""
        self.store.delete_book(book_id)
        shutil.rmtree(self.book_dir(book_id), ignore_errors=True)

    def delete_audio(self, book_id: str) -> None:
        """Only the chapter audio (and its word timings): text, Chinese and progress stay."""
        self.store.clear_audio(book_id)
        shutil.rmtree(self.book_dir(book_id) / "ch", ignore_errors=True)

    # ---- disk use ----
    def disk_usage(self, book_id: str) -> dict:
        """Bytes of a book's audio (ch/: mp3 + word timings) and of everything else in its folder."""
        audio = other = 0
        root = self.book_dir(book_id)
        for f in root.rglob("*"):
            if f.is_file():
                n = f.stat().st_size
                if f.parent == root / "ch":
                    audio += n
                else:
                    other += n
        return {"audio": audio, "other": other}

    def sweep(self) -> int:
        """Remove leftovers (call at startup, before any work starts): folders of books no longer in
        the DB, interrupted imports, half-written files and clips no 生词 points at. Bytes freed."""
        freed = 0

        def drop(p: Path) -> None:
            nonlocal freed
            files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
            freed += sum(f.stat().st_size for f in files)
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

        books = {b["id"] for b in self.store.list_books()}
        if self._books.is_dir():
            for d in self._books.iterdir():
                if d.is_dir() and d.name not in books:
                    drop(d)
            for f in self._books.rglob("*.tmp"):
                drop(f)
        vdir = self.home / "vocab"
        if vdir.is_dir():
            keep = {f"{v['id']}.mp3" for v in self.store.list_vocab()}
            for f in vdir.iterdir():
                if f.is_file() and f.name not in keep:
                    drop(f)
        return freed
