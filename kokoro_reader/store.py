import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULT_VOICE


def _now() -> float:
    return time.time()  # set_progress has a `time` argument that hides the module

STATUSES = ("none", "queued", "converting", "done", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    sha256 TEXT NOT NULL,
    voice TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT,
    part TEXT,
    is_matter INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0,
    first_line TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    duration REAL,
    voice TEXT,
    read_ratio REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, idx)
);
CREATE TABLE IF NOT EXISTS progress (
    book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    chapter INTEGER NOT NULL,
    para INTEGER NOT NULL,
    word INTEGER NOT NULL,
    time REAL NOT NULL,
    read_at REAL  -- when this position was saved: the shelf's 继续阅读 is the latest one
);
CREATE TABLE IF NOT EXISTS vocab (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    book_id TEXT,  -- no foreign key: 生词本 entries outlive their book
    created_at REAL NOT NULL,
    data TEXT NOT NULL
);
"""

# Before: vocab.book_id cascaded, so deleting a book wiped its 生词. Rebuilt once, rows kept.
_VOCAB_MIGRATION = """
CREATE TABLE vocab_new (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    book_id TEXT,  -- no foreign key: 生词本 entries outlive their book
    created_at REAL NOT NULL,
    data TEXT NOT NULL
);
INSERT INTO vocab_new (seq, id, book_id, created_at, data) SELECT seq, id, book_id, created_at, data FROM vocab;
DROP TABLE vocab;
ALTER TABLE vocab_new RENAME TO vocab;
"""


class Store:
    """SQLite store. A fresh connection per operation keeps it thread-safe."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='vocab'").fetchone()[0]
            if "REFERENCES" in sql:
                c.executescript("BEGIN;" + _VOCAB_MIGRATION + "COMMIT;")
            if "read_at" not in [r[1] for r in c.execute("PRAGMA table_info(progress)")]:
                c.execute("ALTER TABLE progress ADD COLUMN read_at REAL")

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            with c:  # commit / rollback
                yield c
        finally:
            c.close()

    # ---- books ----
    def add_book(self, title, author, sha256, voice=DEFAULT_VOICE) -> str:
        bid = uuid.uuid4().hex
        with self._conn() as c:
            pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM books").fetchone()[0]
            c.execute(
                "INSERT INTO books (id, title, author, sha256, voice, position, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (bid, title, author, sha256, voice, pos, time.time()),
            )
        return bid

    def find_book_by_hash(self, sha256):
        with self._conn() as c:
            r = c.execute("SELECT * FROM books WHERE sha256=?", (sha256,)).fetchone()
        return dict(r) if r else None

    def get_book(self, id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM books WHERE id=?", (id,)).fetchone()
        return dict(r) if r else None

    def list_books(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM books ORDER BY position, created_at").fetchall()
        return [dict(r) for r in rows]

    def reorder_books(self, ids):
        with self._conn() as c:
            existing = [r[0] for r in c.execute("SELECT id FROM books")]
            if len(ids) != len(set(ids)) or set(ids) != set(existing):
                raise ValueError("ids must be a permutation of all book ids")
            for pos, bid in enumerate(ids):
                c.execute("UPDATE books SET position=? WHERE id=?", (pos, bid))

    def delete_book(self, id):
        with self._conn() as c:
            c.execute("DELETE FROM books WHERE id=?", (id,))

    def set_book_voice(self, id, voice):
        with self._conn() as c:
            c.execute("UPDATE books SET voice=? WHERE id=?", (voice, id))

    # ---- progress ----
    def set_progress(self, id, chapter, para, word, time):
        with self._conn() as c:
            c.execute(
                "INSERT INTO progress (book_id, chapter, para, word, time, read_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET chapter=excluded.chapter, "
                "para=excluded.para, word=excluded.word, time=excluded.time, read_at=excluded.read_at",
                (id, chapter, para, word, time, _now()),
            )

    def get_progress(self, id):
        with self._conn() as c:
            r = c.execute(
                "SELECT chapter, para, word, time FROM progress WHERE book_id=?", (id,)
            ).fetchone()
        return dict(r) if r else None

    def read_at(self, id):
        with self._conn() as c:
            r = c.execute("SELECT read_at FROM progress WHERE book_id=?", (id,)).fetchone()
        return r[0] if r else None

    # ---- chapters ----
    def add_chapters(self, book_id, chapters):
        with self._conn() as c:
            c.executemany(
                "INSERT INTO chapters (book_id, idx, title, part, is_matter, chars, first_line) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (book_id, ch["idx"], ch.get("title"), ch.get("part"),
                     int(bool(ch.get("is_matter"))), ch.get("chars", 0), ch.get("first_line"))
                    for ch in chapters
                ],
            )

    def list_chapters(self, book_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT idx, title, part, is_matter, chars, first_line, status, duration, "
                "voice, read_ratio FROM chapters WHERE book_id=? ORDER BY idx",
                (book_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_matter"] = bool(d["is_matter"])
            out.append(d)
        return out

    def set_chapter_status(self, book_id, idx, status, duration=None, voice=None):
        if status not in STATUSES:
            raise ValueError(f"bad status: {status}")
        with self._conn() as c:
            c.execute(
                "UPDATE chapters SET status=?, duration=COALESCE(?, duration), "
                "voice=COALESCE(?, voice) WHERE book_id=? AND idx=?",
                (status, duration, voice, book_id, idx),
            )

    def clear_audio(self, book_id):
        """Every chapter back to 未准备 (its audio files are gone)."""
        with self._conn() as c:
            c.execute("UPDATE chapters SET status='none', duration=NULL, voice=NULL WHERE book_id=?", (book_id,))

    def set_chapter_matter(self, book_id, idx, is_matter):
        with self._conn() as c:
            c.execute(
                "UPDATE chapters SET is_matter=? WHERE book_id=? AND idx=?",
                (int(bool(is_matter)), book_id, idx),
            )

    def set_read_ratio(self, book_id, idx, ratio):
        """R14: a chapter's read ratio only ever increases (re-reading doesn't un-finish it)."""
        with self._conn() as c:
            c.execute(
                "UPDATE chapters SET read_ratio=MAX(read_ratio, ?) WHERE book_id=? AND idx=?",
                (ratio, book_id, idx),
            )

    def reset_stuck(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                # a chapter that was done before (a voice is only written on done) still has its
                # audio: a re-record for a new voice that never ran leaves it done, not none
                "UPDATE chapters SET status = CASE WHEN voice IS NOT NULL THEN 'done' ELSE 'none' END "
                "WHERE status IN ('queued','converting')"
            )
            return cur.rowcount

    # ---- vocab ----
    def add_vocab(self, rec) -> str:
        vid = rec.get("id") or uuid.uuid4().hex
        created = rec.get("created_at") or time.time()
        data = {k: v for k, v in rec.items() if k not in ("id", "created_at")}
        with self._conn() as c:
            c.execute(
                "INSERT INTO vocab (id, book_id, created_at, data) VALUES (?,?,?,?)",
                (vid, rec.get("book_id"), created, json.dumps(data)),
            )
        return vid

    @staticmethod
    def _vocab_row(r):
        d = json.loads(r["data"])
        d["id"] = r["id"]
        d["created_at"] = r["created_at"]
        return d

    def list_vocab(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM vocab ORDER BY seq DESC").fetchall()
        return [self._vocab_row(r) for r in rows]

    def get_vocab(self, id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM vocab WHERE id=?", (id,)).fetchone()
        return self._vocab_row(r) if r else None

    def update_vocab(self, id, **fields):
        """Merge `fields` into a saved record's data (e.g. anki_note_id once it is in Anki)."""
        with self._conn() as c:
            r = c.execute("SELECT data FROM vocab WHERE id=?", (id,)).fetchone()
            if r is None:
                return
            data = {**json.loads(r["data"]), **fields}
            c.execute("UPDATE vocab SET data=? WHERE id=?", (json.dumps(data), id))

    def delete_vocab(self, id):
        with self._conn() as c:
            c.execute("DELETE FROM vocab WHERE id=?", (id,))
