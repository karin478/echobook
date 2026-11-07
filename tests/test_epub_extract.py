import os
from pathlib import Path

import pytest

from kokoro_reader.epub_extract import (
    BookData,
    ChapterData,
    EpubError,
    classify_matter,
    extract_book,
    normalize_title,
)
from tests.epub_factory import make_epub, make_raw_epub

GUTENBERG_PP = Path(os.environ.get("KOKORO_READER_SMOKE_BOOKS") or "/nonexistent") / "pride_and_prejudice.epub"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
LONG = "This is an ordinary sentence of the story that keeps going. " * 8  # > 300 chars


def _book(tmp_path, docs, toc, **kw):
    kw.setdefault("title", "Test Book")
    kw.setdefault("author", "Ann Author")
    return extract_book(make_epub(tmp_path / "b.epub", docs=docs, toc=toc, **kw))


def test_basic_metadata_and_cover(tmp_path):
    book = _book(
        tmp_path,
        [("c1.xhtml", "<h1>Chapter 1</h1><p>Hello.</p>")],
        [("Chapter 1", "c1.xhtml")],
        title="My Novel",
        author="Jane Doe",
        cover=JPEG,
    )
    assert isinstance(book, BookData)
    assert book.title == "My Novel"
    assert book.author == "Jane Doe"
    assert book.cover == JPEG
    assert book.cover_ext == "jpg"
    assert all(isinstance(c, ChapterData) for c in book.chapters)


def test_cover_from_meta_name_fallback(tmp_path):
    book = _book(
        tmp_path,
        [("c1.xhtml", "<p>Hello.</p>")],
        [("Chapter 1", "c1.xhtml")],
        cover=b"\x89PNGfake",
        cover_name="images/front.png",
        cover_via_meta_only=True,
    )
    assert book.cover == b"\x89PNGfake"
    assert book.cover_ext == "png"


def test_no_cover(tmp_path):
    book = _book(tmp_path, [("c1.xhtml", "<p>Hello.</p>")], [("Chapter 1", "c1.xhtml")])
    assert book.cover is None
    assert book.cover_ext is None


def test_chapter_spans_multiple_files(tmp_path):
    book = _book(
        tmp_path,
        [
            ("a.xhtml", "<h1>Chapter 1</h1><p>Alpha one.</p>"),
            ("b.xhtml", "<p>Bravo two.</p>"),
            ("c.xhtml", "<h1>Chapter 2</h1><p>Charlie three.</p>"),
        ],
        [("Chapter 1", "a.xhtml"), ("Chapter 2", "c.xhtml")],
    )
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert book.chapters[0].paragraphs == ["Alpha one.", "Bravo two."]
    assert book.chapters[1].paragraphs == ["Charlie three."]


def test_anchor_split_within_one_file(tmp_path):
    body = (
        '<h2 id="ch1">Chapter 1</h2><p>First chapter text.</p>'
        '<div id="ch2"><h2>Chapter 2</h2><p>Second chapter text.</p></div>'
    )
    book = _book(
        tmp_path,
        [("all.xhtml", body)],
        [("Chapter 1", "all.xhtml#ch1"), ("Chapter 2", "all.xhtml#ch2")],
    )
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert book.chapters[0].paragraphs == ["First chapter text."]
    assert book.chapters[1].paragraphs == ["Second chapter text."]


def test_anchor_inside_paragraph_starts_at_that_paragraph(tmp_path):
    body = '<p>Intro.</p><p><a id="mid"></a>Middle starts here.</p><p>End.</p>'
    book = _book(
        tmp_path,
        [("x.xhtml", body)],
        [("Chapter 1", "x.xhtml"), ("Chapter 2", "x.xhtml#mid")],
    )
    assert book.chapters[0].paragraphs == ["Intro."]
    assert book.chapters[1].paragraphs == ["Middle starts here.", "End."]


def test_nested_toc_parts(tmp_path):
    book = _book(
        tmp_path,
        [
            ("c1.xhtml", "<h2>Chapter 1</h2><p>One.</p>"),
            ("c2.xhtml", "<h2>Chapter 2</h2><p>Two.</p>"),
        ],
        [("Part One", [("Chapter 1", "c1.xhtml"), ("Chapter 2", "c2.xhtml")])],
    )
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert [c.part for c in book.chapters] == ["Part One", "Part One"]


def test_part_page_with_href_is_boundary_not_chapter(tmp_path):
    # Part entry has its own page (only its title): it must not leak into the
    # previous chapter and must not become a chapter itself.
    book = _book(
        tmp_path,
        [
            ("c1.xhtml", "<h2>Chapter 1</h2><p>One.</p>"),
            ("p2.xhtml", "<h1>Part Two</h1>"),
            ("c2.xhtml", "<h2>Chapter 2</h2><p>Two.</p>"),
        ],
        [("Chapter 1", "c1.xhtml"), ("Part Two", "p2.xhtml", [("Chapter 2", "c2.xhtml")])],
    )
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert book.chapters[0].paragraphs == ["One."]
    assert book.chapters[1].part == "Part Two"


def test_part_page_with_content_becomes_its_own_section(tmp_path):
    book = _book(
        tmp_path,
        [
            ("p1.xhtml", "<h1>Part One</h1><p>An epigraph line.</p>"),
            ("c1.xhtml", "<h2>Chapter 1</h2><p>" + LONG + "</p>"),
        ],
        [("Part One", "p1.xhtml", [("Chapter 1", "c1.xhtml")])],
    )
    assert [(c.title, c.part) for c in book.chapters] == [("Part One", None), ("Chapter 1", "Part One")]
    assert book.chapters[0].paragraphs == ["An epigraph line."]
    assert book.chapters[0].is_matter is True
    assert book.chapters[1].is_matter is False


def test_div_paragraphs_and_footnote_refs(tmp_path):
    body = (
        '<div>Hello<sup><a href="#n1">1</a></sup> world.</div>'
        '<p>See<a epub:type="noteref" href="#n2">*</a> this.</p>'
        '<aside epub:type="footnote" id="n1"><p>Footnote text one.</p></aside>'
        '<aside id="n2"><p>Footnote text two.</p></aside>'
    )
    book = _book(tmp_path, [("c1.xhtml", body)], [("Chapter 1", "c1.xhtml")])
    paras = book.chapters[0].paragraphs
    assert paras == ["Hello world.", "See this."]
    assert not any("Footnote" in p for c in book.chapters for p in c.paragraphs)


def test_drop_cap_joined(tmp_path):
    body = '<p><span class="dropcap">I</span>T is a truth</p>'
    book = _book(tmp_path, [("c1.xhtml", body)], [("Chapter 1", "c1.xhtml")])
    assert book.chapters[0].paragraphs == ["IT is a truth"]


def test_drop_cap_image_alt_letter_kept(tmp_path):
    body = '<p><span class="letra"><img alt="M" src="m.png"/></span>R. BENNET was among</p>'
    book = _book(tmp_path, [("c1.xhtml", body)], [("Chapter 1", "c1.xhtml")])
    assert book.chapters[0].paragraphs == ["MR. BENNET was among"]


def test_br_becomes_space_and_whitespace_collapsed(tmp_path):
    body = "<p>Line one<br/>line   two\n\tend.</p>"
    book = _book(tmp_path, [("c1.xhtml", body)], [("Chapter 1", "c1.xhtml")])
    assert book.chapters[0].paragraphs == ["Line one line two end."]


def test_caption_skipped_and_heading_dedup(tmp_path):
    body = (
        '<h2 id="c1"><span class="caption">A pretty picture.</span> CHAPTER ONE.</h2>'
        '<div class="figcenter"><img src="x.jpg" alt=""/><div class="caption"><p>He came down</p></div></div>'
        '<figure><img src="y.jpg" alt=""/><figcaption>Figure 1</figcaption></figure>'
        '<div class="illustration"><p>Illus text</p></div>'
        "<h3>A Subheading</h3>"
        "<p>Real text.</p>"
    )
    book = _book(tmp_path, [("c1.xhtml", body)], [("Chapter One", "c1.xhtml#c1")])
    assert book.chapters[0].paragraphs == ["A Subheading", "Real text."]


def test_mixed_inline_text_in_container_kept(tmp_path):
    body = "<div>Loose text <em>here</em>.<p>Inner para.</p>Tail text.</div>"
    book = _book(tmp_path, [("c1.xhtml", body)], [("Chapter 1", "c1.xhtml")])
    assert book.chapters[0].paragraphs == ["Loose text here.", "Inner para.", "Tail text."]


def test_no_toc_fallback_per_spine_doc(tmp_path):
    book = _book(
        tmp_path,
        [
            ("a.xhtml", "<h1>The Beginning</h1><p>Alpha.</p>"),
            ("b.xhtml", "<p>Bravo.</p>"),
            ("c.xhtml", "<h3>Third Part</h3><p>Charlie.</p>"),
        ],
        [],
    )
    assert [c.title for c in book.chapters] == ["The Beginning", "Section 2", "Third Part"]
    assert [c.paragraphs for c in book.chapters] == [["Alpha."], ["Bravo."], ["Charlie."]]
    assert all(c.part is None for c in book.chapters)


def test_toc_with_unresolvable_hrefs_falls_back(tmp_path):
    book = _book(
        tmp_path,
        [("a.xhtml", "<h1>Real One</h1><p>Alpha.</p>")],
        [("Ghost", "missing.xhtml")],
    )
    assert [c.title for c in book.chapters] == ["Real One"]


def test_empty_chapters_dropped(tmp_path):
    book = _book(
        tmp_path,
        [
            ("cover.xhtml", '<div><img src="cover.jpg" alt=""/></div>'),
            ("c1.xhtml", "<h1>Chapter 1</h1><p>Text.</p>"),
        ],
        [("Cover", "cover.xhtml"), ("Chapter 1", "c1.xhtml")],
    )
    assert [c.title for c in book.chapters] == ["Chapter 1"]


def test_raw_epub_html_entities_and_no_toc(tmp_path):
    doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head>'
        "<body><h1>Opening</h1><p>Mr.&nbsp;Darcy &amp; Elizabeth&mdash;at last.</p><div/><p>After.</p></body></html>"
    )
    book = extract_book(make_raw_epub(tmp_path / "r.epub", title="Raw", author="R. Author", docs=[("a.xhtml", doc)]))
    assert book.title == "Raw"
    assert book.author == "R. Author"
    assert [c.title for c in book.chapters] == ["Opening"]
    assert book.chapters[0].paragraphs == ["Mr. Darcy & Elizabeth—at last.", "After."]


def test_gutenberg_boilerplate_skipped(tmp_path):
    body = (
        '<header class="pg-boilerplate pgheader" id="pg-header"><h2>The Project Gutenberg eBook</h2>'
        "<p>Project Gutenberg header.</p></header>"
        '<h2 id="c1">Chapter I.</h2><p>Story text.</p>'
    )
    end = (
        '<footer class="pg-boilerplate" id="pg-footer"><div><span>*** END OF THE PROJECT GUTENBERG EBOOK ***</span></div>'
        "<div>Project Gutenberg license prelude.</div>"
        '<h2 id="pg-footer-heading">THE FULL PROJECT GUTENBERG LICENSE</h2><p>License text.</p></footer>'
    )
    book = _book(
        tmp_path,
        [("main.xhtml", body), ("end.xhtml", end)],
        [("Chapter I.", "main.xhtml#c1"), ("THE FULL PROJECT GUTENBERG LICENSE", "end.xhtml#pg-footer-heading")],
    )
    assert [c.title for c in book.chapters] == ["Chapter I."]
    assert book.chapters[0].paragraphs == ["Story text."]


def test_corrupt_raises_epuberror(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"\x00\x13garbage" * 100)
    with pytest.raises(EpubError) as ei:
        extract_book(bad)
    assert str(ei.value)  # Chinese message for the UI


def test_zip_without_opf_raises_epuberror(tmp_path):
    import zipfile

    bad = tmp_path / "nozip.epub"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "not an epub")
    with pytest.raises(EpubError):
        extract_book(bad)


def test_normalize_title():
    assert normalize_title("I hope Mr. Bingley will like it. CHAPTER II.") == "CHAPTER II."
    assert normalize_title("“The obsequious civility.” CHAPTER LX.") == "CHAPTER LX."
    assert normalize_title("He rode a black horse. Chapter 3") == "Chapter 3"
    assert normalize_title("Chapter I.") == "Chapter I."
    assert normalize_title("  The   Boy Who Lived ") == "The Boy Who Lived"
    assert normalize_title("Chapters of Life") == "Chapters of Life"


def test_toc_titles_are_normalized(tmp_path):
    book = _book(
        tmp_path,
        [("c2.xhtml", '<h2><span class="caption">I hope it.</span> CHAPTER II.</h2><p>Text.</p>')],
        [("I hope it. CHAPTER II.", "c2.xhtml")],
    )
    assert book.chapters[0].title == "CHAPTER II."
    assert book.chapters[0].paragraphs == ["Text."]


def test_classify_matter():
    assert classify_matter("Copyright", ["All rights reserved. " * 30]) is True
    assert classify_matter("Acknowledgments", [LONG]) is True
    assert classify_matter("Chapter 1", ["A short line."]) is False
    assert classify_matter("Prologue", ["Brief."]) is False
    assert classify_matter("Untitled", ["Short."]) is True
    assert classify_matter("Epilogue", ["Brief."]) is False
    assert classify_matter("12", ["Brief."]) is False
    assert classify_matter("The Storm", [LONG]) is False


def test_classify_matter_keywords_match_word_starts():
    assert classify_matter("Discovery", [LONG]) is False
    assert classify_matter("Civilization and Its Discontents", [LONG]) is False
    assert classify_matter("Endnotes", [LONG]) is True
    assert classify_matter("Notes", [LONG]) is True
    assert classify_matter("Table of Contents", [LONG]) is True
    assert classify_matter("ABOUT THE AUTHOR", [LONG]) is True


def test_classify_matter_book_title_license_gutenberg():
    assert classify_matter("PRIDE. and PREJUDICE", [LONG], book_title="Pride and Prejudice") is True
    assert classify_matter("Pride and Prejudice", [LONG]) is False
    assert classify_matter("THE FULL PROJECT GUTENBERG™ LICENSE", [LONG]) is True
    assert classify_matter("License", [LONG]) is True
    assert classify_matter("Gutenberg Notes", [LONG]) is True


def test_extract_sets_is_matter(tmp_path):
    book = _book(
        tmp_path,
        [
            ("t.xhtml", "<h1>Test Book</h1><p>" + LONG + "</p>"),
            ("cr.xhtml", "<h1>Copyright</h1><p>All rights reserved.</p>"),
            ("c1.xhtml", "<h1>Chapter 1</h1><p>Short.</p>"),
        ],
        [("Test Book", "t.xhtml"), ("Copyright", "cr.xhtml"), ("Chapter 1", "c1.xhtml")],
    )
    assert [c.is_matter for c in book.chapters] == [True, True, False]


@pytest.mark.skipif(not GUTENBERG_PP.exists(), reason="Gutenberg sample EPUB not present")
def test_gutenberg_pride_and_prejudice():
    book = extract_book(GUTENBERG_PP)
    assert book.title == "Pride and Prejudice"
    assert book.author == "Jane Austen"
    assert book.cover and book.cover_ext == "jpg"
    story = [c for c in book.chapters if not c.is_matter]
    assert len(story) == 61
    assert story[0].title == "Chapter I."
    assert story[0].paragraphs[0].startswith("IT is a truth universally acknowledged")
    assert story[1].title == "CHAPTER II."
    assert story[1].paragraphs[0].startswith("MR. BENNET was among the earliest")
    assert story[-1].title == "CHAPTER LXI."
    assert story[26].title == "CHAPTER XXVII."
    assert story[26].paragraphs[0].startswith("WITH no greater events")
    assert all(" " in c.title for c in story)
    for c in book.chapters:
        for p in c.paragraphs:
            assert "Project Gutenberg" not in p, (c.title, p[:80])
            assert "PROJECT GUTENBERG" not in p, (c.title, p[:80])
    # captions such as "He came down to see the place" are not read aloud
    assert not any("He came down to see the place" in p for p in story[0].paragraphs)


def test_bare_number_heading_dedup_for_chapter_title(tmp_path):
    # Commercial EPUBs: TOC says "Chapter 2", the page heading is just "2".
    book = _book(
        tmp_path,
        [("c2.xhtml", '<h1 class="chapter-number">2</h1><p>First line.</p><h2>3</h2><p>Second.</p>')],
        [("Chapter 2", "c2.xhtml")],
    )
    assert book.chapters[0].paragraphs == ["First line.", "3", "Second."]


def test_book_title_rule_does_not_fold_a_whole_novel():
    # The Letters: one TOC entry titled like the book covers the entire story.
    huge = ["Dear Felix, " + "a letter line that goes on. " * 40] * 100  # ~110k chars
    assert classify_matter("The Letters", huge, book_title="The Letters") is False
    assert classify_matter("The Letters", [LONG] * 10, book_title="The Letters") is True


def test_raw_xhtml_self_closing_tags_respected(tmp_path):
    # Read the raw XHTML: an HTML re-parse would turn <span class="figcenter"/> into
    # an open span swallowing the following text (and then skip it as a caption).
    doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head>'
        '<body><h1>Opening</h1><p><span class="figcenter"/>Visible text.</p></body></html>'
    )
    book = extract_book(make_raw_epub(tmp_path / "r.epub", title="Raw", author="A", docs=[("a.xhtml", doc)]))
    assert book.chapters[0].paragraphs == ["Visible text."]


def test_non_utf8_document_decoded(tmp_path):
    doc = (
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head>'
        "<body><h1>Opening</h1><p>Un café crème.</p></body></html>"
    ).encode("iso-8859-1")
    book = extract_book(make_raw_epub(tmp_path / "r.epub", title="Raw", author="A", docs=[("a.xhtml", doc)]))
    assert book.chapters[0].paragraphs == ["Un café crème."]


def test_blank_toc_title_uses_first_heading(tmp_path):
    book = _book(
        tmp_path,
        [("c1.xhtml", "<h2>The Start</h2><p>Text one.</p>"), ("c2.xhtml", "<p>Text two.</p>")],
        [(" ", "c1.xhtml"), ("", "c2.xhtml")],
    )
    assert [c.title for c in book.chapters] == ["The Start", "Section 2"]
    assert [c.paragraphs for c in book.chapters] == [["Text one."], ["Text two."]]


# ---------------------------------------------------------------- fix round 1


def test_group_page_text_is_always_matter(tmp_path):
    # (a) A Part is only a separator: its own page text never counts as a story chapter,
    # even when the short-text rule would not fire (title has a digit).
    book = _book(
        tmp_path,
        [
            ("p1.xhtml", "<h1>Part 1</h1><p>An epigraph line.</p>"),
            ("c1.xhtml", "<h2>Chapter 1</h2><p>" + LONG + "</p>"),
            ("c2.xhtml", "<h2>Chapter 2</h2><p>" + LONG + "</p>"),
        ],
        [("Part 1", "p1.xhtml", [("Chapter 1", "c1.xhtml"), ("Chapter 2", "c2.xhtml")])],
    )
    assert [(c.title, c.is_matter) for c in book.chapters] == [
        ("Part 1", True),
        ("Chapter 1", False),
        ("Chapter 2", False),
    ]
    assert len([c for c in book.chapters if not c.is_matter]) == 2


def _letters(n: int, chars_each: int, heading: bool = True) -> list[tuple[str, str]]:
    body = "<p>" + ("Dear friend, this letter continues at length. " * (chars_each // 46 + 1))[:chars_each] + "</p>"
    return [(f"l{i}.xhtml", (f"<h2>Letter {i}</h2>" if heading else "") + body) for i in range(1, n + 1)]


def test_oversized_toc_leaf_split_per_spine_file(tmp_path):
    # (b) R9: one leaf spanning >= 3 files and > 60,000 chars → one chapter per file.
    docs = [("pre.xhtml", "<h1>A Preface</h1><p>" + LONG + "</p>"), *_letters(4, 16_000)]
    docs[3] = ("l3.xhtml", docs[3][1].replace("<h2>Letter 3</h2>", ""))  # no heading in file 3
    book = _book(tmp_path, docs, [("A Preface", "pre.xhtml"), ("The Letters", "l1.xhtml")])
    assert [c.title for c in book.chapters] == [
        "A Preface",
        "Letter 1",
        "Letter 2",
        "The Letters · 3",
        "Letter 4",
    ]
    assert all(not c.is_matter for c in book.chapters)
    assert book.chapters[1].paragraphs[0].startswith("Dear friend")
    assert len(book.chapters[1].paragraphs) == 1


def test_large_leaf_not_split_below_thresholds(tmp_path):
    # only 2 files (even if huge), or >= 3 files but <= 60,000 chars → unchanged
    two = _book(tmp_path, _letters(2, 40_000), [("Big", "l1.xhtml")])
    assert [c.title for c in two.chapters] == ["Big"]
    (tmp_path / "b.epub").unlink()
    small = _book(tmp_path, _letters(3, 15_000), [("Small", "l1.xhtml")])
    assert [c.title for c in small.chapters] == ["Small"]


def test_r10_matter_keywords():
    for title in ["Read More", "Discussion Questions", "A Reading Group Guide", "Excerpt", "Also Available"]:
        assert classify_matter(title, [LONG] * 20) is True, title


def test_missing_anchor_falls_back_to_file_start(tmp_path):
    # (d) a TOC fragment that does not exist still starts the chapter at its file
    book = _book(
        tmp_path,
        [
            ("c1.xhtml", '<h2 id="ch1">Chapter 1</h2><p>One.</p>'),
            ("c2.xhtml", "<h2>Chapter 2</h2><p>Two.</p>"),
        ],
        [("Chapter 1", "c1.xhtml#ch1"), ("Chapter 2", "c2.xhtml#no-such-id")],
    )
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert book.chapters[0].paragraphs == ["One."]
    assert book.chapters[1].paragraphs == ["Two."]


def test_body_id_registered_as_anchor():
    from kokoro_reader.epub_extract import _Stream

    stream = _Stream()
    stream.add_document("a.xhtml", b"<html><body><p>A.</p></body></html>")
    stream.add_document("b.xhtml", b'<html xmlns="http://www.w3.org/1999/xhtml"><body id="b-top"><p>B.</p></body></html>')
    assert stream.anchors[("b.xhtml", "b-top")] == 1


def test_nonlinear_spine_doc_not_in_toc_is_skipped(tmp_path):
    # (e) linear="no" popup notes files are not streamed into the previous chapter
    book = _book(
        tmp_path,
        [
            ("c1.xhtml", "<h2>Chapter 1</h2><p>One.</p>"),
            ("notes.xhtml", "<p>Popup note text.</p>"),
            ("extra.xhtml", "<h2>Map</h2><p>A map description.</p>"),
            ("c2.xhtml", "<h2>Chapter 2</h2><p>Two.</p>"),
        ],
        [("Chapter 1", "c1.xhtml"), ("Map", "extra.xhtml"), ("Chapter 2", "c2.xhtml")],
        nonlinear={"notes.xhtml", "extra.xhtml"},
    )
    assert [c.title for c in book.chapters] == ["Chapter 1", "Map", "Chapter 2"]
    assert book.chapters[0].paragraphs == ["One."]
    assert book.chapters[1].paragraphs == ["A map description."]


def test_normalize_title_only_cuts_trailing_chapter_number():
    # (f)
    assert normalize_title("The Chapter I Never Wrote") == "The Chapter I Never Wrote"
    assert normalize_title("Before Chapter 3 begins") == "Before Chapter 3 begins"
    assert normalize_title("I hope Mr. Bingley will like it. CHAPTER II.") == "CHAPTER II."
    assert normalize_title("I hope it. CHAPTER II.  ") == "CHAPTER II."


def test_normalize_title_separates_glued_chapter_number():
    # Gutenberg P&P ch 27–28: the source itself reads "CHAPTERXXVII." (no space)
    assert normalize_title("“On the Stairs.” CHAPTERXXVII.") == "CHAPTER XXVII."
    assert normalize_title("CHAPTERXXVIII.") == "CHAPTER XXVIII."
    assert normalize_title("Chapter12") == "Chapter 12"
    # ordinary words are left alone
    assert normalize_title("Chapterdim") == "Chapterdim"
    assert normalize_title("Chapters of Life") == "Chapters of Life"


def test_glued_chapter_heading_is_titled_and_deduplicated(tmp_path):
    book = _book(
        tmp_path,
        [(
            "c27.xhtml",
            '<h2><img alt="" src="x.jpg"/><br/><span class="caption">“On the Stairs.”</span>'
            "<br/><br/>CHAPTERXXVII.</h2><p>With no greater events.</p>",
        )],
        [("“On the Stairs.” CHAPTERXXVII.", "c27.xhtml")],
    )
    assert book.chapters[0].title == "CHAPTER XXVII."
    assert book.chapters[0].paragraphs == ["With no greater events."]


def test_oversized_split_merges_tiny_files_into_neighbours(tmp_path):
    # Epistolary books (The Letters): a split piece of <= 300 chars (a two-line
    # thank-you note) is not a chapter of its own; it joins the previous piece, or
    # the next one when it comes first.
    big = _letters(3, 25_000, heading=False)
    note = "<p>Dear Mr. Lübeck,</p><p>Thank you for the cookies. Happy New Year.</p>"
    docs = [("n0.xhtml", note), big[0], ("n1.xhtml", note), big[1], big[2]]
    book = _book(tmp_path, docs, [("The Letters", "n0.xhtml")])
    assert [c.title for c in book.chapters] == ["The Letters · 1", "The Letters · 2", "The Letters · 3"]
    first = book.chapters[0].paragraphs
    assert first[:2] == ["Dear Mr. Lübeck,", "Thank you for the cookies. Happy New Year."]
    assert first[2].startswith("Dear friend")
    assert first[3:] == ["Dear Mr. Lübeck,", "Thank you for the cookies. Happy New Year."]
    assert all(sum(len(p) for p in c.paragraphs) > 300 for c in book.chapters)


# ---------------------------------------------------------------- review fixes (round 2)

GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
SVG_COVER = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _xdoc(heading: str, body: str, prolog: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n' + prolog + '<html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>x</title></head><body><h1>{heading}</h1>{body}</body></html>"
    )


@pytest.mark.parametrize(
    "data,name,ext",
    [
        (JPEG, "cover.png", "jpg"),
        (b"\x89PNG\r\n\x1a\nrest", "cover.jpg", "png"),
        (GIF, "cover.jpg", "gif"),
        (WEBP, "cover.jpg", "webp"),
    ],
)
def test_cover_ext_from_magic_bytes(tmp_path, data, name, ext):
    book = _book(tmp_path, [("c1.xhtml", "<p>Hello.</p>")], [("Chapter 1", "c1.xhtml")], cover=data, cover_name=name)
    assert (book.cover, book.cover_ext) == (data, ext)


@pytest.mark.parametrize(
    "data,name,meta_only",
    [
        (SVG_COVER, "cover.svg", False),
        (SVG_COVER, "cover.svg", True),
        (b"<html><body><script>alert(1)</script></body></html>", "cover.html", False),
        (b"not really an image", "cover.jpg", False),
        (b"not really an image", "images/front.png", True),
    ],
)
def test_cover_rejected_unless_known_image_format(tmp_path, data, name, meta_only):
    book = _book(
        tmp_path,
        [("c1.xhtml", "<p>Hello.</p>")],
        [("Chapter 1", "c1.xhtml")],
        cover=data,
        cover_name=name,
        cover_via_meta_only=meta_only,
    )
    assert (book.cover, book.cover_ext) == (None, None)


def test_doctype_with_internal_subset_and_custom_entity_keeps_chapter(tmp_path):
    doc = _xdoc(
        "Opening",
        "<p>Plain words here.</p><p>Uses &x; entity.</p>",
        prolog='<!DOCTYPE html [<!ENTITY x "XX">]>\n',
    )
    book = extract_book(make_raw_epub(tmp_path / "r.epub", title="Raw", author="A", docs=[("a.xhtml", doc)]))
    assert [c.title for c in book.chapters] == ["Opening"]
    paras = book.chapters[0].paragraphs
    assert paras[0] == "Plain words here."
    assert paras[1].startswith("Uses") and paras[1].endswith("entity.")


def test_doctype_with_internal_subset_still_parsed_as_xml(tmp_path):
    # no custom entity in use → stays on the XML path (self-closing span respected)
    doc = _xdoc(
        "Opening",
        '<p><span class="figcenter"/>Visible text.</p>',
        prolog='<!DOCTYPE html [\n  <!ENTITY x "a]b">\n]>\n',
    )
    book = extract_book(make_raw_epub(tmp_path / "r.epub", title="Raw", author="A", docs=[("a.xhtml", doc)]))
    assert book.chapters[0].paragraphs == ["Visible text."]


def test_external_xhtml11_doctype_still_works(tmp_path):
    doc = _xdoc(
        "Opening",
        '<p><span class="figcenter"/>Visible&nbsp;text.</p>',
        prolog='<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n',
    )
    book = extract_book(make_raw_epub(tmp_path / "r.epub", title="Raw", author="A", docs=[("a.xhtml", doc)]))
    assert [c.title for c in book.chapters] == ["Opening"]
    assert book.chapters[0].paragraphs == ["Visible text."]


def test_manifest_file_missing_from_zip_is_tolerated(tmp_path):
    docs = [
        ("c1.xhtml", _xdoc("One", "<p>First text.</p>")),
        ("c2.xhtml", _xdoc("Two", "<p>Second text.</p>")),
        ("c3.xhtml", _xdoc("Three", "<p>Third text.</p>")),
    ]
    path = make_raw_epub(tmp_path / "r.epub", title="Raw", author="A", docs=docs, zip_names={"c2.xhtml": None})
    book = extract_book(path)
    assert [(c.title, c.paragraphs) for c in book.chapters] == [("One", ["First text."]), ("Three", ["Third text."])]


def test_manifest_path_case_mismatch_is_tolerated(tmp_path):
    docs = [("Text/Ch1.xhtml", _xdoc("One", "<p>First text.</p>"))]
    path = make_raw_epub(
        tmp_path / "r.epub", title="Raw", author="A", docs=docs, zip_names={"Text/Ch1.xhtml": "text/ch1.xhtml"}
    )
    book = extract_book(path)
    assert [(c.title, c.paragraphs) for c in book.chapters] == [("One", ["First text."])]


def _bomb_epub(tmp_path, chars: int) -> Path:
    doc = _xdoc("One", "<p>" + "a" * chars + "</p>")
    return make_raw_epub(tmp_path / "z.epub", title="Raw", author="A", docs=[("a.xhtml", doc)])


@pytest.mark.parametrize(
    "limits",
    [
        {"ZIP_MAX_TOTAL": 50_000},
        {"ZIP_MAX_MEMBER": 50_000},
        {"ZIP_RATIO_MIN_SIZE": 50_000},  # 100k of "a" compresses far beyond 200:1
    ],
)
def test_zip_bomb_rejected_before_parsing(tmp_path, monkeypatch, limits):
    import kokoro_reader.epub_extract as ex

    for name, value in limits.items():
        monkeypatch.setattr(ex, name, value, raising=False)

    def no_parse(*a, **k):
        raise AssertionError("must not parse a zip bomb")

    monkeypatch.setattr(ex.epub.EpubReader, "load", no_parse)
    with pytest.raises(EpubError) as ei:
        extract_book(_bomb_epub(tmp_path, 100_000))
    assert "过大" in str(ei.value)


def test_zip_bomb_defaults_and_normal_book_unaffected(tmp_path):
    import kokoro_reader.epub_extract as ex

    assert ex.ZIP_MAX_TOTAL == 1024**3
    assert ex.ZIP_MAX_MEMBER == 300 * 1024**2
    assert ex.ZIP_MAX_RATIO == 200
    assert ex.ZIP_RATIO_MIN_SIZE == 50 * 1024**2
    book = extract_book(_bomb_epub(tmp_path, 100_000))  # highly compressible but small
    assert book.chapters[0].title == "One"


def test_unexpected_error_becomes_generic_epuberror(tmp_path, monkeypatch, caplog):
    import logging

    import kokoro_reader.epub_extract as ex

    def boom(*a, **k):
        raise RuntimeError("boom inside extraction")

    monkeypatch.setattr(ex, "_chapters_from_toc", boom)
    path = make_epub(tmp_path / "b.epub", title="T", author="A", docs=[("c1.xhtml", "<p>Hi.</p>")], toc=[("C", "c1.xhtml")])
    with caplog.at_level(logging.ERROR, logger=ex.__name__):
        with pytest.raises(EpubError) as ei:
            extract_book(path)
    assert str(ei.value) == ex._READ_ERROR
    assert "boom inside extraction" in caplog.text


def test_epuberror_messages_pass_through_unchanged(tmp_path):
    import kokoro_reader.epub_extract as ex

    with pytest.raises(EpubError) as ei:
        _book(tmp_path, [("c1.xhtml", "<div></div>")], [("Chapter 1", "c1.xhtml")])
    assert str(ei.value) == ex._NO_TEXT_ERROR


def test_numbered_chapter_titles_with_matter_words_are_story():
    for title in [
        "Chapter 12: Covered Tracks",
        "Chapter 3: The Index Card",
        "Chapter 9 - Contents of the Safe",
        "CHAPTER TWELVE. The Copyright Thief",
        "Ch. 4 Notes",
        "Part 2: Acknowledgement of Debt",
        "12. The Index",
        "XII. Contents of the Safe",
    ]:
        assert classify_matter(title, [LONG]) is False, title
        assert classify_matter(title, ["Brief."]) is False, title


def test_matter_keyword_needs_title_start_short_title_or_short_text():
    assert classify_matter("Notes from Underground", [LONG] * 5) is False
    assert classify_matter("Covered Tracks", [LONG]) is False
    assert classify_matter("The Night We Read the Index Cards Aloud", [LONG]) is False
    assert classify_matter("The Night We Read the Index Cards Aloud", ["Brief."]) is True
    # still matter
    assert classify_matter("Index", [LONG] * 5) is True
    assert classify_matter("Contents", [LONG]) is True
    assert classify_matter("Copyright Page and Credits Information", [LONG]) is True
    assert classify_matter("The Index", [LONG]) is True
    assert classify_matter("Other Books by Ann Author", [LONG]) is True
    assert classify_matter("Author's Notes", [LONG]) is True
    assert classify_matter("Notes on Sources", [LONG] * 5) is True
