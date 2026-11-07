"""EPUB → chapters of plain-text paragraphs, plus title/author/cover.

Algorithm (`extract_book`):

1. Parse every spine document, in spine order (skipping `linear="no"` documents
   that no TOC entry points at, e.g. popup footnotes), into one linear stream of
   *blocks*. A block is a block-level element with no block-level descendants
   (`p, div, blockquote, li, h1–h6`, plus structural tags such as `section`,
   `td`, `figure`…). Loose inline text sitting directly inside a container next
   to child blocks becomes its own block, so no text is lost. Each block keeps
   the file it came from. Every `id` (and old-style `<a name>`) is mapped to a
   stream position: an id on a block or anywhere inside it points at that
   block; an id on a container or on a skipped element points at the next block
   emitted after it; the `<body>` id points at the file's first block. Each
   file's first position is recorded too.
2. Flatten the table of contents into entries `(title, href, part)`; a group's
   title is the `part` of its children. A group that has its own href (a part
   title page) is also an entry, so its page never leaks into the previous
   chapter; it only becomes a chapter if it has text of its own, and that
   chapter is always matter (a Part is a separator, not story).
3. Each entry href (file + optional `#anchor`) resolves to a start position in
   the stream; a chapter runs from its start to the next entry's start and may
   span several files. Text before the first entry is dropped. A `#fragment`
   that does not exist falls back to the start of its file; hrefs whose file
   cannot be found are ignored. A leaf spanning >= 3 files with > 60,000
   characters is split into one chapter per file (titled by the file's first
   h1–h3, else "<TOC title> · <n>"); a file with <= 300 characters joins its
   neighbour instead of becoming a chapter.
4. If there is no TOC, or none of its hrefs resolve, every spine document is a
   chapter, titled by its first h1–h3, else "Section N" (a blank TOC title is
   filled the same way).
5. Text cleaning per block:
   - `get_text()` with no separator, then whitespace collapsed to single spaces
     (drop caps like `<span>I</span>T is` → "IT is"); `<br>` becomes a space; an
     `<img>` whose alt is a single letter (image drop cap) contributes that letter.
   - Removed: `<sup>`, elements whose `epub:type` / `role` marks a noteref or
     footnote/endnote, `<aside>`, `<figcaption>`, elements whose class matches
     `caption|figcenter|illus`, and Project Gutenberg boilerplate
     (`class="pg-boilerplate"`), plus `script/style`.
   - A heading block whose text equals the chapter title (ignoring case and
     punctuation; "Chapter 2" also matches a bare "2") is not kept as a paragraph. Empty paragraphs are dropped, and
     chapters left with no paragraphs are dropped.
   - TOC titles are normalized: whitespace collapsed, and a leading illustration
     caption before "chapter <number/roman>" is cut off, and a glued
     "CHAPTERXXVII." becomes "CHAPTER XXVII." (`normalize_title`); headings are
     normalized the same way before the dedup above.
6. Metadata: DC `title` and `creator`. Cover: the item with the `cover-image`
   property first, else the item named by `<meta name="cover">`; kept only when
   its magic bytes say JPEG/PNG/GIF/WebP, and the extension comes from that.

Documents are parsed as XML when well-formed (after dropping any DOCTYPE and
mapping HTML named entities such as `&nbsp;` to numeric references), otherwise
with the lenient HTML parser.

Robustness: the zip's central directory is checked for zip bombs before
anything is parsed; a manifest file missing from the zip (or differing in case)
reads as the case-insensitive match, else as empty; any unexpected exception
becomes the generic `EpubError`.
"""

from __future__ import annotations

import html.entities
import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import ebooklib
from bs4 import BeautifulSoup, NavigableString, Tag, UnicodeDammit
from bs4.element import CData
from ebooklib import epub
from lxml import etree

log = logging.getLogger(__name__)

class EpubError(Exception):
    """The file cannot be used as a book. The message is Chinese, for the UI."""


@dataclass
class ChapterData:
    title: str
    part: str | None
    paragraphs: list[str]
    is_matter: bool


@dataclass
class BookData:
    title: str
    author: str
    cover: bytes | None
    cover_ext: str | None
    chapters: list[ChapterData]


# ---------------------------------------------------------------- titles / matter

_CHAPTER_AT_END = re.compile(r"\bchapter\s+(?:\d+|[ivxlcdm]+)\.?\s*$", re.IGNORECASE)
# "CHAPTERXXVII." (a typo in the Gutenberg source): "chapter" glued to a trailing
# number or upper-case roman numeral ("Chapterdim" is a word, not a numeral).
_GLUED_CHAPTER = re.compile(r"\b((?i:chapter))(\d+|[IVXLCDM]+)(\.?)$")

MATTER_KEYWORDS = (
    "copyright", "contents", "acknowledg", "about the author", "also by", "dedication",
    "title page", "cover", "praise for", "index", "bibliography", "colophon", "newsletter",
    "half title", "books by", "notes", "license", "gutenberg",
    "read more", "discussion questions", "reading group", "excerpt", "also available",
)  # fmt: skip
# Multi-word phrases (and "gutenberg") are specific enough to count anywhere in a title.
_MATTER_ANYWHERE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in MATTER_KEYWORDS if " " in k or k == "gutenberg") + r")\b"
)
# Single words are whole words ("Covered" is not "cover", "Discontents" is not
# "contents"), except "acknowledg…" / "excerpt…" which take any ending. They only
# count at the start of the title, in a short title, or over short text.
_MATTER_WORD = re.compile(
    r"\b(?:copyright|contents|dedications?|covers?|index|bibliography|colophon|newsletters?"
    r"|licen[cs]e|acknowledg\w*|excerpt\w*)\b"
)
# "notes" ends a word ("Endnotes") and must end the title ("Author's Notes"), or
# open a typical back-matter heading; "Notes from Underground" is a story title.
_MATTER_NOTES = re.compile(
    r"notes\W*$|^notes\s+(?:and|on|to)\s+(?:the\s+)?(?:sources?|references?|text|translation|chapters?)\b"
)
_MATTER_NOTES_ANY = re.compile(r"notes\b")
MATTER_SHORT_TITLE_WORDS = 4
_NUM_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
)
_ROMAN = r"(?=[mdclxvi])m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
# A title that opens with a chapter number is story, whatever words follow:
# "Chapter 12: …", "Ch. 4 …", "Part Two …", "12. …", "XII. …" (a bare roman
# numeral needs punctuation or the end after it, so "Mix Tape" is not "MIX").
_NUMBERED_TITLE = re.compile(
    rf"^(?:(?:chapter\s*|ch\.\s*|ch\s+|part\s+)(?:\d+|{_ROMAN}|{_NUM_WORD}(?:[-\s]{_NUM_WORD})?)\b"
    rf"|\d+\b|{_ROMAN}(?:\s*[.:)\-–—]|\s*$))"
)
_STORY_WORDS = re.compile(r"chapter|prologue|epilogue")
MATTER_MIN_CHARS = 300
# A title page (+ preface) named like the book is matter, but a single TOC entry
# named like the book that holds the whole story (e.g. an epistolary novel) is not.
TITLE_PAGE_MAX_CHARS = 50_000


def _norm(text: str) -> str:
    """Lowercase, punctuation → spaces, whitespace collapsed (for comparisons)."""
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def normalize_title(title: str) -> str:
    """Collapse whitespace; drop a caption glued before a trailing "chapter <n>".

    A trailing "CHAPTERXXVII." gets its missing space back first.

    "I hope Mr. Bingley will like it. CHAPTER II." → "CHAPTER II."; only cuts when
    "chapter <n>[.]" ends the title ("The Chapter I Never Wrote" is kept).
    """
    title = re.sub(r"\s+", " ", title or "").strip()
    title = _GLUED_CHAPTER.sub(r"\1 \2\3", title)
    m = _CHAPTER_AT_END.search(title)
    if m and title[: m.start()].strip():
        return title[m.start() :]
    return title


def classify_matter(title: str, paragraphs: list[str], *, book_title: str | None = None) -> bool:
    """True if a chapter looks like front/back matter rather than story text.

    - the title equals the book title (title page) and the text is under
      `TITLE_PAGE_MAX_CHARS`; otherwise
    - a title opening with a chapter number ("Chapter 12: Covered Tracks",
      "Ch. 4", "Part Two", "12.", "XII.") is never matter;
    - a matter keyword (see `MATTER_KEYWORDS`) counts when it is a multi-word
      phrase or "gutenberg" (anywhere), "notes" ending the title, a single
      keyword that opens the title or sits in a title of <= 4 words, or any
      keyword (incl. "notes" elsewhere) with under 300 characters of text;
    - all paragraphs together are under 300 characters and the title has no
      digit and no "chapter" / "prologue" / "epilogue".
    """
    t = re.sub(r"\s+", " ", (title or "").lower()).strip()
    total = sum(len(p) for p in paragraphs)
    if book_title and _norm(t) and _norm(t) == _norm(book_title) and total < TITLE_PAGE_MAX_CHARS:
        return True
    if _NUMBERED_TITLE.match(t):
        return False
    if _MATTER_ANYWHERE.search(t) or _MATTER_NOTES.search(t):
        return True
    m = _MATTER_WORD.search(t)
    if m and (not re.search(r"\w", t[: m.start()]) or len(t.split()) <= MATTER_SHORT_TITLE_WORDS):
        return True
    if (m or _MATTER_NOTES_ANY.search(t)) and total < MATTER_MIN_CHARS:
        return True
    return total < MATTER_MIN_CHARS and not re.search(r"\d", t) and not _STORY_WORDS.search(t)


# ---------------------------------------------------------------- document parsing

_STRUCTURAL = {
    "p", "div", "blockquote", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "body", "section", "article", "header", "footer", "main", "nav", "figure", "aside",
    "ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "pre", "hgroup", "address", "hr", "caption",
}  # fmt: skip
_STRUCTURAL_NAMES = sorted(_STRUCTURAL)
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"sup", "aside", "figcaption", "script", "style"}
_SKIP_CLASS = re.compile(r"caption|figcenter|illus")
_NOTE_TYPES = {"noteref", "footnote", "endnote", "rearnote"}
_NOTE_ROLES = {"doc-noteref", "doc-footnote", "doc-endnote"}
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
# `<!DOCTYPE …>` with an optional internal subset `[ … ]` (quoted strings may hold `]` / `>`).
_DOCTYPE = re.compile(
    r"""<!DOCTYPE(?:[^\[>"']|"[^"]*"|'[^']*')*(?:\[(?:[^\]"']|"[^"]*"|'[^']*')*\]\s*)?>""", re.IGNORECASE
)


def _is_skipped(tag: Tag) -> bool:
    if tag.name in _SKIP_TAGS:
        return True
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    if "pg-boilerplate" in classes or _SKIP_CLASS.search(" ".join(classes)):
        return True
    types: set[str] = set()
    for key in ("epub:type", "{http://www.idpf.org/2007/ops}type"):
        types |= set((tag.get(key) or "").split())
    if types & _NOTE_TYPES:
        return True
    return bool(set((tag.get("role") or "").split()) & _NOTE_ROLES)


def _numeric_entity(m: re.Match) -> str:
    name = m.group(1)
    if name in _XML_ENTITIES:
        return m.group(0)
    value = html.entities.html5.get(name + ";")
    if value is None:
        return m.group(0)
    return "".join(f"&#{ord(c)};" for c in value)


def _parse_document(content: bytes) -> Tag:
    """Return the <body> (or the root) of an (X)HTML document, tag names lowercased."""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:  # honour the declared encoding (or sniff one)
            text = UnicodeDammit(content, is_html=False).unicode_markup or content.decode("utf-8", "replace")
    else:
        text = content
    text = re.sub(r"^\ufeff?\s*<\?xml[^>]*\?>", "", text)
    # An internal DTD subset makes BeautifulSoup's XML mode return only the doctype
    # (the chapter vanishes): drop any DOCTYPE. Custom entities it declared are then
    # undefined, so such a document falls through to the lenient HTML parser.
    text = _DOCTYPE.sub("", text, count=1)
    text = _ENTITY.sub(_numeric_entity, text)
    try:
        etree.fromstring(text.encode("utf-8"), etree.XMLParser(recover=False, resolve_entities=False))
        soup = BeautifulSoup(text, "lxml-xml")
    except etree.XMLSyntaxError:
        soup = BeautifulSoup(text, "lxml")
    for t in soup.find_all(True):
        t.name = t.name.lower()
    return soup.find("body") or soup


@dataclass
class _Block:
    file: str
    text: str
    heading_level: int  # 0 = not a heading


class _Stream:
    def __init__(self) -> None:
        self.blocks: list[_Block] = []
        self.anchors: dict[tuple[str, str], int] = {}
        self.file_start: dict[str, int] = {}

    # ids -------------------------------------------------------------------
    def _register(self, file: str, tag: Tag, pos: int) -> None:
        for t in [tag, *tag.find_all(True)]:
            for key in ("id", "xml:id") + (("name",) if t.name == "a" else ()):
                v = t.get(key)
                if v:
                    self.anchors.setdefault((file, v), pos)

    # documents -------------------------------------------------------------
    def add_document(self, file: str, content: bytes) -> None:
        self.file_start.setdefault(file, len(self.blocks))
        body = _parse_document(content)
        self._register_own(file, body)
        self._walk(file, body)

    def _walk(self, file: str, node: Tag) -> None:
        inline: list = []

        def flush() -> None:
            if inline:
                self._emit(file, list(inline))
                inline.clear()

        for child in list(node.children):
            if isinstance(child, NavigableString):
                if type(child) in (NavigableString, CData):
                    inline.append(child)
                continue
            if not isinstance(child, Tag):
                continue
            has_blocks = child.find(_STRUCTURAL_NAMES) is not None
            if _is_skipped(child):
                if child.name in _STRUCTURAL or has_blocks:
                    flush()
                self._register(file, child, len(self.blocks))
                continue
            if child.name in _STRUCTURAL or has_blocks:
                flush()
                if has_blocks:
                    self._register_own(file, child)
                    self._walk(file, child)
                else:
                    self._emit(file, [child])
            else:
                inline.append(child)
        flush()

    def _register_own(self, file: str, tag: Tag) -> None:
        for key in ("id", "xml:id"):
            v = tag.get(key)
            if v:
                self.anchors.setdefault((file, v), len(self.blocks))

    def _emit(self, file: str, nodes: list) -> None:
        pos = len(self.blocks)
        parts: list[str] = []
        for n in nodes:
            if isinstance(n, Tag):
                self._register(file, n, pos)
                parts.append(_clean_text(n))
            else:
                parts.append(str(n))
        text = re.sub(r"\s+", " ", "".join(parts)).strip()
        level = 0
        if len(nodes) == 1 and isinstance(nodes[0], Tag) and nodes[0].name in _HEADINGS:
            level = int(nodes[0].name[1])
        self.blocks.append(_Block(file, text, level))


def _clean_text(tag: Tag) -> str:
    for t in tag.find_all(_is_skipped):
        if not t.decomposed:
            t.decompose()
    for br in tag.find_all("br"):
        br.replace_with(" ")
    for img in tag.find_all("img"):
        alt = (img.get("alt") or "").strip()
        img.replace_with(alt if len(alt) == 1 and alt.isalpha() else "")
    return tag.get_text()


# ---------------------------------------------------------------- TOC

@dataclass
class _Entry:
    title: str
    href: str | None
    part: str | None
    is_group: bool = False  # a Part-like group: its own page is never a story chapter


def _toc_title(node) -> str:
    return re.sub(r"\s+", " ", getattr(node, "title", None) or "").strip()


def _flatten_toc(toc, part: str | None = None) -> list[_Entry]:
    if not isinstance(toc, (list, tuple)) or (
        isinstance(toc, tuple) and len(toc) == 2 and isinstance(toc[1], list)
    ):
        toc = [toc]
    out: list[_Entry] = []
    for node in toc:
        if isinstance(node, (tuple, list)) and len(node) == 2 and isinstance(node[1], (list, tuple)):
            section, children = node
            title = _toc_title(section)
            out.append(_Entry(title, getattr(section, "href", None) or None, part, is_group=True))
            out.extend(_flatten_toc(list(children), title or part))
        elif isinstance(node, (epub.Link, epub.Section)):
            out.append(_Entry(_toc_title(node), node.href or None, part))
        elif isinstance(node, epub.EpubHtml):
            out.append(_Entry(_toc_title(node), node.get_name(), part))
    return out


# ---------------------------------------------------------------- book

_READ_ERROR = "无法读取这个文件：它不是有效的 EPUB，或者文件已损坏。"
_NO_TEXT_ERROR = "这本 EPUB 里没有找到可以朗读的文字。"
_TOO_BIG_ERROR = "这个 EPUB 解压后体积过大（可能是恶意压缩包），已拒绝导入。"
# Zip-bomb guard (uncompressed sizes from the zip's central directory).
ZIP_MAX_TOTAL = 1024**3  # 1 GB for the whole book
ZIP_MAX_MEMBER = 300 * 1024**2  # 300 MB for any single file
ZIP_MAX_RATIO = 200  # compression ratio limit …
ZIP_RATIO_MIN_SIZE = 50 * 1024**2  # … applied to files over 50 MB
# R9: a single TOC leaf this big, spread over this many files, is split per file.
SPLIT_MIN_FILES = 3
SPLIT_MIN_CHARS = 60_000


@dataclass
class _RawChapter:
    title: str
    part: str | None
    paragraphs: list[str]
    is_matter: bool | None = None  # None → decided by classify_matter


class _TolerantReader(epub.EpubReader):
    """A manifest file missing from the zip must not sink the whole book.

    Lookup falls back to a case-insensitive match, then to empty bytes (a missing
    chapter simply has no text; a missing container/OPF still fails to parse).
    """

    def read_file(self, name):
        try:
            return super().read_file(name)
        except KeyError:
            want = posixpath.normpath(name).lower()
            match = next((n for n in self.zf.namelist() if n.lower() == want), None)
            return self.zf.read(match) if match is not None else b""


def _read(path: Path) -> epub.EpubBook:
    last: Exception | None = None
    for options in ({"ignore_ncx": True}, {"ignore_ncx": False}):
        try:
            reader = _TolerantReader(str(path), options)
            book = reader.load()
            reader.process()
            return book
        except Exception as e:  # noqa: BLE001 — ebooklib raises many kinds on bad input
            last = e
    raise EpubError(_READ_ERROR) from last


def _first_meta(book: epub.EpubBook, name: str) -> str:
    for value, _ in book.get_metadata("DC", name):
        if value and value.strip():
            return re.sub(r"\s+", " ", value).strip()
    return ""


def _image_ext(data: bytes) -> str | None:
    """The real image format from magic bytes (jpg/png/gif/webp), else None.

    The cover is served same-origin later, so its extension must never come from
    the (attacker-controlled) manifest name: SVG/HTML could carry scripts.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _cover(book: epub.EpubBook) -> tuple[bytes | None, str | None]:
    item = next((i for i in book.get_items_of_type(ebooklib.ITEM_COVER) if i.get_content()), None)
    if item is None:
        ids = [attrs.get("content") for _, attrs in book.get_metadata("OPF", "cover")]
        ids += [attrs.get("content") for _, attrs in book.get_metadata("OPF", "meta") if attrs.get("name") == "cover"]
        for ref in filter(None, ids):
            cand = book.get_item_with_id(ref) or book.get_item_with_href(ref)
            if cand is not None and (cand.media_type or "").startswith("image/") and cand.get_content():
                item = cand
                break
    if item is None:
        return None, None
    data = item.get_content()
    ext = _image_ext(data)
    return (data, ext) if ext else (None, None)


def _spine_documents(book: epub.EpubBook) -> list[tuple[epub.EpubHtml, bool]]:
    """Spine documents in order, each with its `linear` flag (False for linear="no")."""
    docs, seen = [], set()
    for idref, linear in book.spine:
        item = book.get_item_with_id(idref)
        if isinstance(item, epub.EpubHtml) and item.get_name() not in seen:
            seen.add(item.get_name())
            docs.append((item, str(linear or "yes").strip().lower() != "no"))
    return docs


def _split_href(href: str) -> tuple[str, str]:
    path, _, frag = unquote(href.strip()).partition("#")
    return path, frag


def _resolve_name(path: str, names: list[str], toc_dirs: list[str]) -> str | None:
    """Map a TOC href path to a spine document name."""
    if not path:
        return None
    known = set(names)
    cands = [path, posixpath.normpath(path)] + [posixpath.normpath(posixpath.join(d, path)) for d in toc_dirs]
    name = next((c for c in cands if c in known), None)
    if name is None:
        base = posixpath.basename(path)
        matches = [n for n in names if posixpath.basename(n) == base]
        name = matches[0] if len(matches) == 1 else None
    return name


def _resolve(href: str, stream: _Stream, toc_dirs: list[str]) -> int | None:
    path, frag = _split_href(href)
    name = _resolve_name(path, list(stream.file_start), toc_dirs)
    if name is None:
        return None
    if frag and (name, frag) in stream.anchors:
        return stream.anchors[(name, frag)]
    return stream.file_start[name]  # no fragment, or a fragment that does not exist


def _title_variants(*titles: str) -> set[str]:
    """Normalized forms a chapter's own heading may take: "Chapter 2" → {"chapter 2", "2"}."""
    out = set()
    for t in titles:
        n = _norm(t)
        if n:
            out.add(n)
            out.add(re.sub(r"^chapter\s+", "", n))
    return out


def _paragraphs(blocks: list[_Block], titles: set[str]) -> list[str]:
    out = []
    for b in blocks:
        if not b.text:
            continue
        if b.heading_level and (_norm(b.text) in titles or _norm(normalize_title(b.text)) in titles):
            continue
        out.append(b.text)
    return out


def _first_heading(blocks: list[_Block]) -> str:
    return normalize_title(next((b.text for b in blocks if 1 <= b.heading_level <= 3 and b.text), ""))


def _blocks_by_file(blocks: list[_Block]) -> list[list[_Block]]:
    groups: list[list[_Block]] = []
    for b in blocks:
        if groups and groups[-1][0].file == b.file:
            groups[-1].append(b)
        else:
            groups.append([b])
    return groups


def _split_oversized(
    title: str, raw_title: str, part: str | None, blocks: list[_Block], paras: list[str], book_title: str
) -> list[_RawChapter] | None:
    """R9: a leaf over `SPLIT_MIN_CHARS` spread over `SPLIT_MIN_FILES`+ files → one chapter per file.

    Sub-chapters are titled by their first h1–h3, else "<TOC title> · <n>"; they all
    inherit the matter decision of the whole leaf. A file with <= `MATTER_MIN_CHARS`
    of text (a two-line note in an epistolary novel) is not a chapter of its own: it
    joins the previous sub-chapter, or the next one when it comes first.
    """
    files = {b.file for b in blocks if b.text}
    if len(files) < SPLIT_MIN_FILES or sum(len(p) for p in paras) <= SPLIT_MIN_CHARS:
        return None
    matter = classify_matter(title, paras, book_title=book_title)
    out: list[_RawChapter] = []
    carry: list[str] = []  # a tiny piece before any chapter waits for the next one
    for group in _blocks_by_file(blocks):
        n = len(out) + 1
        sub_title = _first_heading(group) or f"{title} · {n}"
        sub_paras = _paragraphs(group, _title_variants(sub_title, raw_title, title))
        if not sub_paras:
            continue
        if sum(len(p) for p in sub_paras) <= MATTER_MIN_CHARS:  # a short note, not a chapter
            if out:
                out[-1].paragraphs.extend(sub_paras)
            else:
                carry.extend(sub_paras)
            continue
        out.append(_RawChapter(sub_title, part, carry + sub_paras, matter))
        carry = []
    if carry:  # every piece was tiny
        out.append(_RawChapter(f"{title} · 1", part, carry, matter))
    return out


def _chapters_from_toc(
    entries: list[_Entry], stream: _Stream, toc_dirs: list[str], book_title: str
) -> list[_RawChapter]:
    starts: list[tuple[int, int, _Entry]] = []
    for order, entry in enumerate(entries):
        if entry.href:
            pos = _resolve(entry.href, stream, toc_dirs)
            if pos is not None:
                starts.append((pos, order, entry))
    starts.sort(key=lambda s: (s[0], s[1]))
    out: list[_RawChapter] = []
    for i, (pos, _, entry) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(stream.blocks)
        blocks = stream.blocks[pos:end]
        title = normalize_title(entry.title) or _first_heading(blocks) or f"Section {len(out) + 1}"
        paras = _paragraphs(blocks, _title_variants(entry.title, title))
        if not paras:
            continue
        if entry.is_group:  # a Part page is a separator, never a story chapter
            out.append(_RawChapter(title, entry.part, paras, True))
            continue
        split = _split_oversized(title, entry.title, entry.part, blocks, paras, book_title)
        out.extend(split if split else [_RawChapter(title, entry.part, paras)])
    return out


def _chapters_per_document(stream: _Stream) -> list[_RawChapter]:
    files = list(stream.file_start)
    out: list[_RawChapter] = []
    for i, f in enumerate(files):
        start = stream.file_start[f]
        end = stream.file_start[files[i + 1]] if i + 1 < len(files) else len(stream.blocks)
        blocks = stream.blocks[start:end]
        title = _first_heading(blocks) or f"Section {len(out) + 1}"
        paras = _paragraphs(blocks, _title_variants(title))
        if paras:
            out.append(_RawChapter(title, None, paras))
    return out


def _check_zip_size(path: Path) -> None:
    """Reject zip bombs from the central directory, before anything is decompressed."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as e:
        raise EpubError(_READ_ERROR) from e
    total = 0
    for info in infos:
        size = info.file_size
        total += size
        ratio = size / max(info.compress_size, 1)
        if size > ZIP_MAX_MEMBER or (size > ZIP_RATIO_MIN_SIZE and ratio > ZIP_MAX_RATIO):
            raise EpubError(_TOO_BIG_ERROR)
    if total > ZIP_MAX_TOTAL:
        raise EpubError(_TOO_BIG_ERROR)


def extract_book(path: Path) -> BookData:
    """Read an EPUB into `BookData`. Raises `EpubError` (Chinese message) on bad input.

    Any unexpected exception from a malformed book is logged and reported as the
    generic read error, so callers only ever see `EpubError`.
    """
    path = Path(path)
    try:
        return _extract_book(path)
    except EpubError:
        raise
    except Exception as e:  # noqa: BLE001 — a weird book must never surface as a bare 500
        log.exception("unexpected error while extracting %s", path)
        raise EpubError(_READ_ERROR) from e


def _extract_book(path: Path) -> BookData:
    _check_zip_size(path)
    book = _read(path)
    spine = _spine_documents(book)
    if not spine:
        raise EpubError(_READ_ERROR)

    entries = _flatten_toc(book.toc)
    toc_dirs = sorted({posixpath.dirname(i.get_name()) for i in book.get_items_of_type(ebooklib.ITEM_NAVIGATION)})
    names = [doc.get_name() for doc, _ in spine]
    in_toc = {_resolve_name(_split_href(e.href)[0], names, toc_dirs) for e in entries if e.href}

    stream = _Stream()
    for doc, linear in spine:
        if not linear and doc.get_name() not in in_toc:
            continue  # linear="no" popups (footnotes etc.) that no TOC entry points at
        # raw bytes from the zip: get_content() would re-parse through ebooklib's HTML template
        stream.add_document(doc.get_name(), doc.content)

    title = _first_meta(book, "title") or path.stem
    author = ", ".join(
        re.sub(r"\s+", " ", v).strip() for v, _ in book.get_metadata("DC", "creator") if v and v.strip()
    )
    cover, cover_ext = _cover(book)

    raw = _chapters_from_toc(entries, stream, toc_dirs, title) or _chapters_per_document(stream)
    if not raw:
        raise EpubError(_NO_TEXT_ERROR)
    chapters = [
        ChapterData(
            title=c.title,
            part=c.part,
            paragraphs=c.paragraphs,
            is_matter=c.is_matter if c.is_matter is not None else classify_matter(c.title, c.paragraphs, book_title=title),
        )
        for c in raw
    ]
    return BookData(title=title, author=author, cover=cover, cover_ext=cover_ext, chapters=chapters)
