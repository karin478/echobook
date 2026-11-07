"""Build small EPUB files for tests with ebooklib (nav + NCX + spine)."""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path

from ebooklib import epub


def _toc_entries(toc: list) -> list:
    """Convert `[("Part", [("Ch 1", "c1.xhtml#a")]), ("Ch 3", "c3.xhtml")]` to ebooklib objects.

    A group may also carry its own page: `("Part Two", "p2.xhtml", [children])`.
    """
    out = []
    for entry in toc:
        if len(entry) == 3:
            title, href, children = entry
            out.append((epub.Section(title, href=href), _toc_entries(children)))
        elif isinstance(entry[1], list):
            out.append((epub.Section(entry[0]), _toc_entries(entry[1])))
        else:
            out.append(epub.Link(entry[1], entry[0], uuid.uuid4().hex))
    return out


def make_epub(
    path,
    *,
    title: str,
    author: str,
    docs: list[tuple[str, str]],
    toc: list,
    cover: bytes | None = None,
    cover_name: str = "cover.jpg",
    cover_via_meta_only: bool = False,
    nonlinear: frozenset[str] | set[str] = frozenset(),
) -> Path:
    """Write an EPUB to `path`.

    `docs` is a list of `(file_name, xhtml body)`; spine order follows `docs`.
    `toc` is nested: a leaf is `(title, href)`, a group is `(title, [children])`
    or `(title, href, [children])`.
    `cover` bytes are stored as a cover-image item (or, with `cover_via_meta_only`,
    only referenced from `<meta name="cover">`).
    File names in `nonlinear` get `linear="no"` in the spine.
    """
    path = Path(path)
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)

    if cover is not None:
        if cover_via_meta_only:
            img = epub.EpubImage(uid="cover-meta-img", file_name=cover_name, content=cover)
            book.add_item(img)
            book.add_metadata(None, "meta", "", {"name": "cover", "content": "cover-meta-img"})
        else:
            book.set_cover(cover_name, cover, create_page=False)

    items = []
    for i, (name, body) in enumerate(docs):
        item = epub.EpubHtml(uid=f"doc{i}", file_name=name, title=name, lang="en")
        item.content = f"<html><head><title>{name}</title></head><body>{body}</body></html>"
        book.add_item(item)
        items.append(item)

    book.toc = _toc_entries(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [(it.id, "no") if it.file_name in nonlinear else it for it in items]
    epub.write_epub(str(path), book)
    return path


CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def make_raw_epub(
    path,
    *,
    title: str,
    author: str,
    docs: list[tuple[str, str]],
    zip_names: dict[str, str | None] | None = None,
) -> Path:
    """Write a minimal EPUB 2 zip by hand: no nav, no NCX, documents kept byte-for-byte.

    Used for inputs ebooklib would normalise on write (e.g. HTML entities like `&nbsp;`).
    `docs` is `(file_name, full xhtml document text)`; spine order follows `docs`.
    `zip_names` maps a manifest file name to the name actually stored in the zip
    (under `OEBPS/`), or to None to leave the file out of the zip entirely.
    """
    zip_names = zip_names or {}
    path = Path(path)
    manifest = "\n".join(
        f'<item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>' for i, (name, _) in enumerate(docs)
    )
    spine = "\n".join(f'<itemref idref="d{i}"/>' for i in range(len(docs)))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:{uuid.uuid4()}</dc:identifier>
    <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>en</dc:language>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine>{spine}</spine>
</package>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        for name, text in docs:
            stored = zip_names.get(name, name)
            if stored is not None:
                zf.writestr(f"OEBPS/{stored}", text)
    return path
