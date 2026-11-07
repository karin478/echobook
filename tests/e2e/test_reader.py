import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.epub_factory import make_epub

pytestmark = pytest.mark.e2e

CH1 = [
    "The knock came twice, and there was no answer from the house.",
    "Mara waited on the step. She called again, louder this time.",
    "“Is anyone home?” she asked — quietly.",
    "The wind moved through the hedges and the gate swung on its hinge. " * 4,
    "At last the door opened.",
]
CH2 = ["Morning came slowly to the harbour town.", "The boats were already out."]
CH3 = ["• • •"]  # scene break only: converts to an empty (silent) chapter
CH4 = ["The storm broke just after midnight."]


def _xhtml(title: str, paras: list[str]) -> str:
    return f"<h1>{title}</h1>" + "".join(f"<p>{p.strip()}</p>" for p in paras)


@pytest.fixture
def reader_epub(tmp_path) -> Path:
    chapters = [("Chapter 1", CH1), ("Chapter 2", CH2), ("Chapter 3", CH3), ("Chapter 4", CH4)]
    docs = [(f"c{i}.xhtml", _xhtml(t, ps)) for i, (t, ps) in enumerate(chapters, start=1)]
    toc = [(t, f"c{i}.xhtml") for i, (t, _) in enumerate(chapters, start=1)]
    return make_epub(tmp_path / "reader.epub", title="The Quiet House", author="Ada Writer", docs=docs, toc=toc)


@pytest.fixture
def book(live_server, reader_epub) -> str:
    return live_server.upload(reader_epub)["id"]


def convert(live_server, book: str, *idxs: int) -> None:
    for i in idxs:
        live_server.api("POST", f"/api/books/{book}/convert", {"mode": "one", "chapter": i})
    for i in idxs:
        live_server.wait_chapter(book, i, "done", timeout=30)


def chapter(live_server, book: str, idx: int) -> dict:
    return live_server.api("GET", f"/api/books/{book}/chapters/{idx}")


def open_reader(page: Page, live_server, book: str, ch: int, playable: bool = True):
    page.goto(f"{live_server.url}/reader.html?book={book}&ch={ch}")
    if playable:
        expect(page.locator("#play")).to_be_visible()
        page.wait_for_function("document.getElementById('au').readyState >= 1")
        page.evaluate("document.getElementById('au').muted = true")


def audio(page: Page, prop: str):
    return page.evaluate(f"document.getElementById('au').{prop}")


def word(page: Page, para: int, text: str):
    return page.locator(f'#text p[data-p="{para}"] w').filter(has_text=re.compile(rf"^{re.escape(text)}$"))


def timed(tokens: list[dict], para: int) -> list[tuple[int, dict]]:
    return [(i, t) for i, t in enumerate(tokens) if t["p"] == para and t["a"] is not None]


def click_and_pause(locator) -> float:
    """Click a word, then pause in the same JS turn; returns currentTime right after the click."""
    return locator.evaluate("""el => {
      el.click();
      const au = document.getElementById('au');
      const t = au.currentTime;
      au.pause();
      return t;
    }""")


SELECT_JS = """([startPara, startWord, endPara, endWord, backward]) => {
  const w = (p, txt) => [...document.querySelectorAll(`#text p[data-p="${p}"] w`)].find(x => x.textContent === txt);
  const a = w(startPara, startWord), b = w(endPara, endWord);
  const s = getSelection();
  if (backward) s.setBaseAndExtent(b.firstChild, b.textContent.length, a.firstChild, 0);
  else s.setBaseAndExtent(a.firstChild, 0, b.firstChild, b.textContent.length);
  b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
  return s.toString();
}"""


def test_reader_highlights_and_click_seeks(page: Page, live_server, book):
    convert(live_server, book, 0)
    tokens = chapter(live_server, book, 0)["tokens"]
    open_reader(page, live_server, book, 0)
    target = word(page, 1, "called")
    i = int(target.get_attribute("data-i"))
    t = click_and_pause(target)
    assert t == pytest.approx(tokens[i]["a"], abs=0.01)
    expect(target).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#text w.on")).to_have_count(1)
    expect(page.locator('#text p[data-p="1"]')).to_have_class(re.compile(r"\bcur\b"))
    expect(page.locator("#text p.cur")).to_have_count(1)
    expect(page.locator("#time")).to_contain_text(" / ")


def test_reader_rendered_text_matches_display_paragraphs(page: Page, live_server, book):
    convert(live_server, book, 0)
    ch = chapter(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    expect(page.locator("h1")).to_have_text("Chapter 1")
    assert page.locator("#text p").all_text_contents() == ch["paragraphs"]
    assert page.locator("#text w").count() == len(ch["tokens"])
    assert page.locator("#text w").nth(5).get_attribute("data-i") == "5"


def test_speed_slider_and_brackets(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    assert audio(page, "playbackRate") == 1
    expect(page.locator("#rate-label")).to_have_text("1.00×")
    for _ in range(3):
        page.keyboard.press("BracketLeft")
    assert audio(page, "playbackRate") == pytest.approx(0.85)
    expect(page.locator("#rate-label")).to_have_text("0.85×")
    assert page.locator("#rate").input_value() == "0.85"
    for _ in range(30):
        page.keyboard.press("BracketRight")
    assert audio(page, "playbackRate") == 2.0
    expect(page.locator("#rate-label")).to_have_text("2.00×")
    # with the slider focused, [ still changes the rate exactly once
    page.locator("#rate").focus()
    page.keyboard.press("BracketLeft")
    assert audio(page, "playbackRate") == pytest.approx(1.95)
    page.locator("#rate").fill("2")
    assert audio(page, "playbackRate") == 2.0
    page.reload()
    page.wait_for_function("document.getElementById('au').readyState >= 1")
    assert audio(page, "playbackRate") == 2.0
    expect(page.locator("#rate-label")).to_have_text("2.00×")


@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_FAKE_DELAY": "1"}])
def test_unconverted_chapter_text_mode_then_switches(page: Page, live_server, book):
    open_reader(page, live_server, book, 0, playable=False)
    banner = page.locator("#banner")
    expect(banner).to_contain_text("本章音频还没好")
    expect(banner).to_contain_text(re.compile(r"生成音频 \d+%|排队中"))
    expect(page.locator("#play")).to_be_hidden()
    expect(page.locator("#text p")).to_have_count(len(CH1))
    assert page.locator("#text p").all_text_contents() == chapter(live_server, book, 0)["paragraphs"]
    page.evaluate("window.__marker = 42")
    expect(page.locator("#play")).to_be_visible(timeout=20000)
    expect(banner).to_be_hidden()
    assert page.locator("#text w").count() > 0
    assert page.evaluate("window.__marker") == 42
    assert page.locator("#text p").all_text_contents() == chapter(live_server, book, 0)["paragraphs"]


def test_resume_from_progress(page: Page, live_server, book):
    convert(live_server, book, 0)
    tokens = chapter(live_server, book, 0)["tokens"]
    i, tok = timed(tokens, 3)[4]
    # time is a stale cache: the (para, word) anchor wins
    live_server.api("PUT", f"/api/books/{book}/progress", {"chapter": 0, "para": 3, "word": 4, "time": 0.0, "ratio": 0})
    open_reader(page, live_server, book, 0)
    page.wait_for_function(f"Math.abs(document.getElementById('au').currentTime - {tok['a']}) < 0.05")
    expect(page.locator("#text w.on")).to_have_attribute("data-i", str(i))


def test_progress_saved_on_pause(page: Page, live_server, book):
    convert(live_server, book, 0)
    tokens = chapter(live_server, book, 0)["tokens"]
    open_reader(page, live_server, book, 0)
    target = word(page, 2, "asked")
    i = int(target.get_attribute("data-i"))
    k = [j for j, _ in timed(tokens, 2)].index(i)
    t = click_and_pause(target)
    prog = None
    for _ in range(50):
        prog = live_server.api("GET", f"/api/books/{book}")["progress"]
        if prog and prog["para"] == 2:
            break
        page.wait_for_timeout(100)
    assert prog is not None, "pausing never saved any progress"
    assert prog["chapter"] == 0 and prog["para"] == 2 and prog["word"] == k
    assert prog["time"] == pytest.approx(t, abs=0.05)


def test_auto_continue_next_chapter_in_place(page: Page, live_server, book):
    convert(live_server, book, 0, 1)
    open_reader(page, live_server, book, 0)
    page.evaluate("window.__marker = 7")
    page.evaluate("const au = document.getElementById('au'); au.currentTime = au.duration - 0.2; au.play()")
    expect(page.locator("h1")).to_have_text("Chapter 2", timeout=10000)
    expect(page).to_have_url(re.compile(r"[?&]ch=1(&|$)"))
    assert page.evaluate("window.__marker") == 7
    page.wait_for_function("!document.getElementById('au').paused && document.getElementById('au').currentTime > 0")
    assert page.locator("#text p").all_text_contents() == chapter(live_server, book, 1)["paragraphs"]
    first = next(c for c in live_server.api("GET", f"/api/books/{book}")["chapters"] if c["idx"] == 0)
    assert first["read_ratio"] >= 0.98


@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_FAKE_DELAY": "0.5"}])
def test_next_chapter_not_converted_waits_then_continues(page: Page, live_server, book):
    convert(live_server, book, 0)
    assert chapter(live_server, book, 1)["audio_url"] is None
    open_reader(page, live_server, book, 0)  # opening queues the next chapter
    page.evaluate("window.__marker = 9")
    page.evaluate("const au = document.getElementById('au'); au.currentTime = au.duration - 0.1; au.play()")
    banner = page.locator("#banner")
    expect(banner).to_contain_text("下一章音频还没好")
    expect(banner.get_by_role("button", name="准备")).to_be_visible()
    expect(page.locator("h1")).to_have_text("Chapter 2", timeout=15000)
    expect(page).to_have_url(re.compile(r"[?&]ch=1(&|$)"))
    assert page.evaluate("window.__marker") == 9


def test_reader_empty_chapter_skips(page: Page, live_server, book):
    convert(live_server, book, 2, 3)
    ch = chapter(live_server, book, 2)
    assert ch["duration"] == 0 and ch["tokens"] == []
    open_reader(page, live_server, book, 2, playable=False)
    page.evaluate("window.__marker = 3")
    expect(page.locator("#banner")).to_contain_text("本章没有可朗读的文字")
    expect(page.locator("#text p")).to_have_text(ch["paragraphs"])
    expect(page.locator("h1")).to_have_text("Chapter 4", timeout=6000)
    expect(page).to_have_url(re.compile(r"[?&]ch=3(&|$)"))
    assert page.evaluate("window.__marker") == 3
    expect(page.locator("#play")).to_be_visible()


def test_select_and_save_vocab(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    assert page.evaluate(SELECT_JS, [0, "no", 0, "answer", False]) == "no answer"
    star = page.get_by_role("button", name="⭐ 收藏")
    expect(star).to_be_visible()
    sel_box = word(page, 0, "answer").bounding_box()
    star_box = star.bounding_box()
    assert abs(star_box["y"] - sel_box["y"]) < 80
    star.click()
    expect(page.locator(".toast")).to_contain_text("已收藏：no answer")
    expect(star).to_be_hidden()
    assert page.evaluate("getSelection().toString()") == ""
    recs = live_server.api("GET", "/api/vocab")
    assert len(recs) == 1
    assert recs[0]["text"] == "no answer"
    assert recs[0]["sentence"] == CH1[0]
    assert (recs[0]["chapter"], recs[0]["para"]) == (0, 0)


def test_selection_does_not_seek(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    t0 = audio(page, "currentTime")
    a = word(page, 1, "Mara").bounding_box()
    b = word(page, 1, "step").bounding_box()
    page.mouse.move(a["x"] + 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] - 2, b["y"] + b["height"] / 2, steps=8)
    page.mouse.up()
    assert page.evaluate("getSelection().toString()").startswith("Mara waited on the")
    expect(page.get_by_role("button", name="⭐ 收藏")).to_be_visible()
    assert audio(page, "currentTime") == t0
    assert audio(page, "paused") is True

    # double-clicking a word selects it without moving playback
    word(page, 1, "louder").dblclick()
    assert page.evaluate("getSelection().toString().trim()") == "louder"
    page.wait_for_function(
        f"Math.abs(document.getElementById('au').currentTime - {t0}) < 0.05 && document.getElementById('au').paused")
    expect(page.get_by_role("button", name="⭐ 收藏")).to_be_visible()


def test_vocab_selection_across_paragraphs_clipped(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    # selected backwards (anchor in paragraph 2, focus in paragraph 1): start is still "Mara"
    page.evaluate(SELECT_JS, [1, "Mara", 2, "asked", True])
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toast")).to_contain_text("已收藏：Mara waited")
    rec = live_server.api("GET", "/api/vocab")[0]
    assert rec["para"] == 1
    assert rec["text"] == CH1[1]
    assert rec["end_char"] == len(CH1[1])


def test_keys_space_toggles_once_and_arrows_jump(page: Page, live_server, book):
    convert(live_server, book, 0)
    tokens = chapter(live_server, book, 0)["tokens"]
    open_reader(page, live_server, book, 0)
    page.locator("#play").click()
    page.wait_for_function("!document.getElementById('au').paused")
    page.keyboard.press("Space")
    assert audio(page, "paused") is True
    page.keyboard.press("Space")
    page.wait_for_function("!document.getElementById('au').paused")
    page.evaluate("document.getElementById('au').pause()")
    # ←/→ move by sentence; paused stays paused in 连续 mode
    page.wait_for_function("document.getElementById('zh-pos').textContent.includes('/')")
    starts = [timed(tokens, 1)[0][1]["a"],                                     # "Mara waited on the step."
              next(t["a"] for _, t in timed(tokens, 1) if t["w"] == "She")]     # "She called again, …"
    page.keyboard.press("ArrowRight")
    page.wait_for_function(f"Math.abs(document.getElementById('au').currentTime - {starts[0]}) < 0.05")
    page.keyboard.press("ArrowRight")
    page.wait_for_function(f"Math.abs(document.getElementById('au').currentTime - {starts[1]}) < 0.05")
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(f"Math.abs(document.getElementById('au').currentTime - {starts[0]}) < 0.05")
    assert audio(page, "paused") is True
    expect(page.locator("a.back")).to_have_attribute("href", f"book.html?id={book}")


def test_click_untimed_punctuation_seeks_to_nearest_word(page: Page, live_server, book):
    convert(live_server, book, 0)
    tokens = chapter(live_server, book, 0)["tokens"]
    open_reader(page, live_server, book, 0)
    quote = word(page, 2, "“")  # opening quote: an untimed token
    assert tokens[int(quote.get_attribute("data-i"))]["a"] is None
    first = timed(tokens, 2)[0][1]["a"]
    assert click_and_pause(quote) == pytest.approx(first, abs=0.01)


def test_resume_point_of_another_chapter_is_not_applied_later(page: Page, live_server, book):
    """Progress points into chapter 2 but the reader opens chapter 1: continuing into 2 starts at 0."""
    convert(live_server, book, 0, 1)
    live_server.api("PUT", f"/api/books/{book}/progress", {"chapter": 1, "para": 1, "word": 3, "time": 3.5, "ratio": 0.5})
    open_reader(page, live_server, book, 0)
    page.evaluate("const au = document.getElementById('au'); au.currentTime = au.duration - 0.2; au.play()")
    expect(page.locator("h1")).to_have_text("Chapter 2", timeout=10000)
    page.wait_for_function("document.getElementById('au').readyState >= 1 && document.getElementById('au').currentTime > 0")
    assert audio(page, "currentTime") < 1.5


def test_star_double_click_saves_once(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    page.evaluate(SELECT_JS, [0, "no", 0, "answer", False])
    box = page.get_by_role("button", name="⭐ 收藏").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(x, y)  # two quick clicks on the same spot (an impatient double-click)
    page.wait_for_timeout(60)
    page.mouse.click(x, y)
    expect(page.locator(".toast")).to_contain_text("已收藏：no answer")
    page.wait_for_timeout(800)
    assert len(live_server.api("GET", "/api/vocab")) == 1


@pytest.mark.parametrize("live_server_env", [{"KOKORO_READER_FAKE_DELAY": "0.5"}])
def test_waiting_for_next_does_not_yank_a_reader_who_acted(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)  # queues chapter 2 (slow)
    page.evaluate("const au = document.getElementById('au'); au.currentTime = au.duration - 0.1; au.play()")
    expect(page.locator("#banner")).to_contain_text("下一章音频还没好")
    page.keyboard.press("ArrowLeft")  # the reader goes back a sentence (stays paused)
    live_server.wait_chapter(book, 1, "done", timeout=20)
    page.wait_for_timeout(2500)  # > two poll intervals
    expect(page.locator("h1")).to_have_text("Chapter 1")
    assert audio(page, "paused") is True


# Translation extensions (e.g. 沉浸式翻译) inject translated text into each <p>; it must not
# shift vocab offsets or be saveable as a word.
INJECT_JS = """([para, where]) => {
  const p = document.querySelector(`p[data-p="${para}"]`);
  const w = document.createElement('font');
  w.className = 'immersive-translate-target-wrapper';
  w.innerHTML = '<br><font class="immersive-translate-target-inner">这是一段插件插入的中文译文。</font>';
  where === 'start' ? p.prepend(w) : p.append(w);
}"""


@pytest.mark.parametrize("where", ["start", "end"])
def test_vocab_offsets_ignore_injected_translation(page: Page, live_server, book, where):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    page.evaluate(INJECT_JS, [0, where])
    assert page.evaluate(SELECT_JS, [0, "no", 0, "answer", False]) == "no answer"
    page.get_by_role("button", name="⭐ 收藏").click()
    expect(page.locator(".toast")).to_contain_text("已收藏：no answer")
    recs = live_server.api("GET", "/api/vocab")
    assert [r["text"] for r in recs] == ["no answer"]
    assert recs[0]["sentence"] == CH1[0]


def test_selecting_injected_translation_offers_no_star(page: Page, live_server, book):
    convert(live_server, book, 0)
    open_reader(page, live_server, book, 0)
    page.evaluate(INJECT_JS, [0, "end"])
    page.evaluate("""() => {
      const t = document.querySelector('.immersive-translate-target-inner').firstChild;
      const r = document.createRange(); r.setStart(t, 0); r.setEnd(t, 4);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
    }""")
    page.wait_for_timeout(300)
    expect(page.get_by_role("button", name="⭐ 收藏")).to_be_hidden()
