import io
import re

import pytest
from fastapi.testclient import TestClient

from kokoro_reader import config
from kokoro_reader.app import create_app
from kokoro_reader.tts import FakeSynth, VOICES
from tests.epub_factory import make_epub

BASE = "http://127.0.0.1"  # TrustedHostMiddleware rejects TestClient's default "testserver"
LONG = "This is an ordinary sentence of the story that keeps going. " * 8


def build_epub(path, n=4, title="Book"):
    docs = [(f"c{i}.xhtml", f"<h1>Chapter {i}</h1><p>{LONG}</p><p>Second para.</p>") for i in range(n)]
    toc = [(f"Chapter {i}", f"c{i}.xhtml") for i in range(n)]
    return make_epub(path, title=title, author="A", docs=docs, toc=toc)


class CountingSynth(FakeSynth):
    def __init__(self):
        self.calls = 0

    def synth_chapter(self, *a, **kw):
        self.calls += 1
        return super().synth_chapter(*a, **kw)


@pytest.fixture
def ctx(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    synth = CountingSynth()
    app = create_app(home, synth, start_worker=False)
    with TestClient(app, base_url=BASE) as client:
        yield client, app, synth, tmp_path


def upload(client, path, name=None):
    data = path.read_bytes() if hasattr(path, "read_bytes") else path
    return client.post("/api/books", files={"file": (name or "b.epub", io.BytesIO(data), "application/epub+zip")})


@pytest.fixture
def book(ctx):
    client, app, synth, tmp = ctx
    r = upload(client, build_epub(tmp / "b.epub"))
    assert r.status_code == 201
    bid = r.json()["book"]["id"]
    for i in range(4):
        app.state.store.set_chapter_matter(bid, i, False)
    return bid


def test_upload_list_reorder_delete(ctx):
    client, app, synth, tmp = ctx
    r1 = upload(client, build_epub(tmp / "a.epub", title="A"))
    r2 = upload(client, build_epub(tmp / "b.epub", n=3, title="B"))
    assert r1.status_code == 201 and r1.json()["created"] is True
    a, b = r1.json()["book"]["id"], r2.json()["book"]["id"]
    books = client.get("/api/books").json()
    assert [x["id"] for x in books] == [a, b]
    assert set(books[0]) >= {"id", "title", "author", "has_cover", "voice", "total", "converted", "progress"}
    assert books[0]["progress"] is None and books[0]["has_cover"] is False
    assert books[0]["read_at"] is None
    assert client.put("/api/books/order", json={"ids": [b, a]}).status_code == 200
    assert [x["id"] for x in client.get("/api/books").json()] == [b, a]
    assert client.put("/api/books/order", json={"ids": [a]}).status_code == 400
    assert client.delete(f"/api/books/{a}").status_code == 204
    assert [x["id"] for x in client.get("/api/books").json()] == [b]
    assert client.get(f"/api/books/{a}").status_code == 404


def test_upload_duplicate_200_created_false(ctx):
    client, app, synth, tmp = ctx
    p = build_epub(tmp / "a.epub")
    first = upload(client, p)
    second = upload(client, p)
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["book"]["id"] == first.json()["book"]["id"]


def test_upload_corrupt_400_chinese_detail(ctx):
    client, *_ = ctx
    r = upload(client, b"not an epub at all", name="x.epub")
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d and re.search(r"[一-鿿]", d)


def test_upload_non_epub_extension_400(ctx):
    client, app, synth, tmp = ctx
    r = upload(client, build_epub(tmp / "a.epub"), name="a.txt")
    assert r.status_code == 400
    assert r.json()["detail"] == "只支持 EPUB 文件"
    assert upload(client, build_epub(tmp / "c.epub"), name="A.EPUB").status_code == 201


def test_convert_open_then_chapter_has_tokens_and_audio(ctx, book):
    client, app, synth, _ = ctx
    ch = client.get(f"/api/books/{book}/chapters/0").json()
    assert ch["tokens"] is None and ch["audio_url"] is None and ch["status"] == "none"
    snap = client.post(f"/api/books/{book}/convert", json={"mode": "open", "chapter": 0}).json()
    assert snap["pending"]
    q = client.get("/api/queue").json()
    assert {k: q[k] for k in ("current", "pending")} == snap and set(q["eta"]) == {book}
    app.state.queue.run_once()
    ch = client.get(f"/api/books/{book}/chapters/0").json()
    assert ch["status"] == "done" and ch["tokens"] and ch["duration"] > 0
    assert ch["audio_url"].startswith(f"/api/books/{book}/chapters/0/audio.mp3?v=")
    assert isinstance(ch["paragraphs"], list) and ch["paragraphs"]
    assert client.post(f"/api/books/{book}/convert", json={"mode": "bogus"}).status_code == 400


def test_reconverting_chapter_stays_playable(ctx, book):
    client, app, *_ = ctx
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 1})
    app.state.queue.run_once()
    client.post(f"/api/books/{book}/convert", json={"mode": "reconvert", "chapter": 1})
    ch = client.get(f"/api/books/{book}/chapters/1").json()
    assert ch["status"] == "queued" and ch["tokens"] and ch["audio_url"]


def test_audio_supports_range(ctx, book):
    client, app, *_ = ctx
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 0})
    app.state.queue.run_once()
    url = f"/api/books/{book}/chapters/0/audio.mp3"
    r = client.get(url, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert len(r.content) == 100
    assert client.get(f"/api/books/{book}/chapters/2/audio.mp3").status_code == 404


def test_chapter_prev_next_skip_matter(ctx, book):
    client, app, *_ = ctx
    app.state.store.set_chapter_matter(book, 1, True)
    c0 = client.get(f"/api/books/{book}/chapters/0").json()
    c2 = client.get(f"/api/books/{book}/chapters/2").json()
    c1 = client.get(f"/api/books/{book}/chapters/1").json()
    assert c0["prev_idx"] is None and c0["next_idx"] == 2
    assert c2["prev_idx"] == 0 and c2["next_idx"] == 3
    assert c1["prev_idx"] == 0 and c1["next_idx"] == 2
    assert client.get(f"/api/books/{book}/chapters/99").status_code == 404
    assert client.put(f"/api/books/{book}/chapters/1/matter", json={"is_matter": False}).status_code == 200
    assert client.get(f"/api/books/{book}/chapters/0").json()["next_idx"] == 1


def test_book_detail_and_counts(ctx, book):
    client, app, *_ = ctx
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 0})
    app.state.queue.run_once()
    d = client.get(f"/api/books/{book}").json()
    assert len(d["chapters"]) == 4 and d["progress"] is None
    row = client.get("/api/books").json()[0]
    assert row["total"] == 4 and row["converted"] == 1


def test_set_voice_invalid_400(ctx, book):
    client, *_ = ctx
    assert client.put(f"/api/books/{book}/voice", json={"voice": "nope"}).status_code == 400
    assert client.put(f"/api/books/{book}/voice", json={"voice": "bf_emma"}).status_code == 200
    assert client.get(f"/api/books/{book}").json()["voice"] == "bf_emma"


def test_progress_roundtrip_updates_read_ratio(ctx, book):
    client, app, *_ = ctx
    body = {"chapter": 2, "para": 1, "word": 3, "time": 12.5, "ratio": 0.4}
    assert client.put(f"/api/books/{book}/progress", json=body).status_code == 200
    d = client.get(f"/api/books/{book}").json()
    assert d["progress"] == {"chapter": 2, "para": 1, "word": 3, "time": 12.5}
    assert d["chapters"][2]["read_ratio"] == 0.4
    assert client.get("/api/books").json()[0]["progress"]["chapter"] == 2
    assert client.get("/api/books").json()[0]["read_at"] > 0  # the shelf's 继续阅读 picks the latest


def test_cover_404_without_cover(ctx, book):
    client, *_ = ctx
    assert client.get(f"/api/books/{book}/cover").status_code == 404


def test_cover_served(ctx):
    client, app, synth, tmp = ctx
    docs = [("c0.xhtml", f"<h1>C</h1><p>{LONG}</p>")]
    p = make_epub(tmp / "cv.epub", title="T", author="A", docs=docs, toc=[("C", "c0.xhtml")],
                  cover=b"\xff\xd8\xff\xe0fakejpeg")
    bid = upload(client, p).json()["book"]["id"]
    assert client.get("/api/books").json()[0]["has_cover"] is True
    assert client.get(f"/api/books/{bid}/cover").status_code == 200


def test_bad_ids_404(ctx):
    client, *_ = ctx
    for bad in ("..", "nothex", "0" * 32):
        assert client.get(f"/api/books/{bad}").status_code == 404
        assert client.get(f"/api/books/{bad}/cover").status_code == 404
        assert client.get(f"/api/vocab/{bad}/clip.mp3").status_code == 404
    assert client.get("/api/books/%2e%2e/chapters/0").status_code == 404
    assert client.delete("/api/vocab/not-an-id").status_code == 404


def test_vocab_create_list_delete(ctx, book):
    client, app, *_ = ctx
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 0})
    app.state.queue.run_once()
    body = {"book_id": book, "chapter": 0, "para": 0, "start_char": 0, "end_char": 4}
    r = client.post("/api/vocab", json=body)
    assert r.status_code == 201
    rec = r.json()
    assert rec["text"] == "This"
    lst = client.get("/api/vocab").json()
    assert [v["id"] for v in lst] == [rec["id"]]
    clip = client.get(f"/api/vocab/{rec['id']}/clip.mp3")
    assert clip.status_code == 200
    assert client.delete(f"/api/vocab/{rec['id']}").status_code == 204
    assert client.get("/api/vocab").json() == []
    assert client.get(f"/api/vocab/{rec['id']}/clip.mp3").status_code == 404
    assert client.post("/api/vocab", json={**body, "start_char": 5, "end_char": 5}).status_code == 400
    assert client.post("/api/vocab", json={**body, "book_id": "0" * 32}).status_code == 400


def test_vocab_sentence_favorite_with_chinese(ctx, book):
    client, app, *_ = ctx
    body = {"book_id": book, "chapter": 0, "para": 0, "start_char": 0, "end_char": 4,
            "whole": True, "zh": "这。"}
    rec = client.post("/api/vocab", json=body).json()
    assert rec["kind"] == "sentence" and rec["zh"] == "这。" and rec["text"] == "This"
    word = client.post("/api/vocab", json={k: v for k, v in body.items() if k != "whole"}).json()
    assert word["kind"] == "word" and word["zh"] == "这。"
    old = client.post("/api/vocab", json={k: v for k, v in body.items() if k not in ("whole", "zh")}).json()
    assert old["kind"] == "word" and old["zh"] is None


def test_voices_and_preview_cached(ctx):
    client, app, _, _ = ctx
    synth = app.state.preview_synth
    assert client.get("/api/voices").json() == [v for v in VOICES if v["engine"] == "kokoro"]  # Microsoft: opt-in
    r1 = client.get("/api/voices/af_heart/preview.mp3")
    assert r1.status_code == 200 and r1.content
    assert synth.calls == 1
    r2 = client.get("/api/voices/af_heart/preview.mp3")
    assert r2.status_code == 200
    assert synth.calls == 1
    assert client.get("/api/voices/nope/preview.mp3").status_code == 404


def test_root_redirects_to_shelf(ctx):
    client, *_ = ctx
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/shelf.html"
    assert client.get("/shelf.html").status_code == 200


def test_binds_localhost_only():
    from kokoro_reader import __main__ as m
    assert m.HOST == "127.0.0.1" == config.HOST


# ---- fix round 1 ----
def test_preview_uses_own_synth_not_queue_synth(tmp_path):
    main, prev = CountingSynth(), CountingSynth()
    app = create_app(tmp_path / "h", main, start_worker=False, preview_synth=prev)
    with TestClient(app, base_url=BASE) as c:
        assert c.get("/api/voices/af_heart/preview.mp3").status_code == 200
    assert prev.calls == 1 and main.calls == 0


def test_preview_synth_defaults_to_separate_instance(tmp_path):
    main = CountingSynth()
    app = create_app(tmp_path / "h", main, start_worker=False)
    with TestClient(app, base_url=BASE) as c:
        c.get("/api/voices/af_heart/preview.mp3")
    assert main.calls == 0


def test_kokoro_pipe_creation_locked(monkeypatch):
    import sys, threading, time, types
    from kokoro_reader.tts import KokoroSynth
    made = []

    class KP:
        def __init__(self, **kw):
            time.sleep(0.05)
            made.append(1)
    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=KP))
    s = KokoroSynth()
    ts = [threading.Thread(target=s._pipe, args=("af_heart",)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(made) == 1


def test_empty_chapter_done_tokens_empty_audio_null(tmp_path):
    app = create_app(tmp_path / "h", FakeSynth(), start_worker=False)
    docs = [("c0.xhtml", f"<h1>C0</h1><p>{LONG}</p>"), ("c1.xhtml", "<h1>C1</h1><p>• • •</p>")]
    p = make_epub(tmp_path / "e.epub", title="E", author="A", docs=docs,
                  toc=[("C0", "c0.xhtml"), ("C1", "c1.xhtml")])
    with TestClient(app, base_url=BASE) as c:
        bid = upload(c, p).json()["book"]["id"]
        c.post(f"/api/books/{bid}/convert", json={"mode": "one", "chapter": 1})
        app.state.queue.run_once()
        ch = c.get(f"/api/books/{bid}/chapters/1").json()
    assert ch["status"] == "done" and ch["tokens"] == [] and ch["audio_url"] is None


def test_worker_started_in_lifespan_and_stopped(tmp_path):
    app = create_app(tmp_path / "h", FakeSynth(), start_worker=True)
    q = app.state.queue
    assert q._thread is None
    with TestClient(app, base_url=BASE):
        assert q._thread is not None and q._thread.is_alive()
        t = q._thread
    assert not t.is_alive()


def test_progress_unknown_chapter_404_and_ratio_clamped(ctx, book):
    client, *_ = ctx
    body = {"chapter": 99, "para": 0, "word": 0, "time": 0, "ratio": 0.5}
    assert client.put(f"/api/books/{book}/progress", json=body).status_code == 404
    assert client.get(f"/api/books/{book}").json()["progress"] is None
    client.put(f"/api/books/{book}/progress", json={**body, "chapter": 1, "ratio": 7})
    assert client.get(f"/api/books/{book}").json()["chapters"][1]["read_ratio"] == 1.0
    client.put(f"/api/books/{book}/progress", json={**body, "chapter": 2, "ratio": -3})
    assert client.get(f"/api/books/{book}").json()["chapters"][2]["read_ratio"] == 0.0


def test_read_ratio_only_increases_but_position_is_latest(ctx, book):
    """R14: re-reading a finished chapter must not lower its read_ratio; the resume point still moves."""
    client, *_ = ctx
    client.put(f"/api/books/{book}/progress", json={"chapter": 1, "para": 3, "word": 0, "time": 90.0, "ratio": 0.9})
    client.put(f"/api/books/{book}/progress", json={"chapter": 1, "para": 0, "word": 2, "time": 20.0, "ratio": 0.2})
    d = client.get(f"/api/books/{book}").json()
    assert d["chapters"][1]["read_ratio"] == 0.9
    assert d["progress"] == {"chapter": 1, "para": 0, "word": 2, "time": 20.0}


def test_convert_one_is_high_priority(ctx, book):
    client, *_ = ctx
    client.post(f"/api/books/{book}/prepare")
    snap = client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 3}).json()
    assert snap["pending"][0] == [book, 3]


# ---- R19: DNS-rebinding guard ----
@pytest.mark.parametrize("host", ["127.0.0.1:7788", "localhost:7788", "127.0.0.1", "localhost"])
def test_local_host_headers_allowed(ctx, host):
    client = ctx[0]
    assert client.get("/api/books", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:7788", "testserver"])
def test_foreign_host_header_rejected(ctx, host):
    client = ctx[0]
    assert client.get("/api/books", headers={"host": host}).status_code == 400


def test_static_files_revalidate_so_updates_show_up(ctx):
    client, *_ = ctx
    for path in ("/reader.html", "/js/reader.js", "/css/app.css"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"


# ---- review fixes ----
@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"},
                                     {"origin": "null"},
                                     {"origin": "http://127.0.0.1:3000"},
                                     {"sec-fetch-site": "cross-site"}])
def test_cross_site_writes_are_rejected(ctx, headers):
    client, app, synth, tmp = ctx
    p = make_epub(tmp / "x.epub", title="T", author="A", docs=[("c0.xhtml", f"<p>{LONG}</p>")],
                  toc=[("C", "c0.xhtml")])
    r = client.post("/api/books", headers=headers,
                    files={"file": ("x.epub", p.read_bytes(), "application/epub+zip")})
    assert r.status_code == 403
    assert client.get("/api/books").json() == []


@pytest.mark.parametrize("host", ["127.0.0.1:7788", "localhost:7799"])
def test_same_origin_writes_allowed(ctx, host):
    client, app, synth, tmp = ctx
    p = make_epub(tmp / "y.epub", title="T", author="A", docs=[("c0.xhtml", f"<p>{LONG}</p>")],
                  toc=[("C", "c0.xhtml")])
    r = client.post("/api/books", headers={"host": host, "origin": f"http://{host}",
                                           "sec-fetch-site": "same-origin"},
                    files={"file": ("y.epub", p.read_bytes(), "application/epub+zip")})
    assert r.status_code == 201


def test_cover_is_served_as_an_image_never_sniffed(ctx):
    client, app, synth, tmp = ctx
    p = make_epub(tmp / "cv2.epub", title="T", author="A", docs=[("c0.xhtml", f"<p>{LONG}</p>")],
                  toc=[("C", "c0.xhtml")], cover=b"\xff\xd8\xff\xe0fakejpeg")
    bid = upload(client, p).json()["book"]["id"]
    r = client.get(f"/api/books/{bid}/cover")
    assert r.headers["content-type"].startswith("image/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_upload_over_the_size_limit_is_413(ctx, monkeypatch):
    import kokoro_reader.app as appmod
    client, app, synth, tmp = ctx
    monkeypatch.setattr(appmod, "MAX_UPLOAD_BYTES", 1000)
    r = client.post("/api/books", files={"file": ("big.epub", b"x" * 5000, "application/epub+zip")})
    assert r.status_code == 413
    assert "太大" in r.json()["detail"]


def test_audio_url_changes_when_the_audio_is_redone(ctx, book):
    client, app, synth, tmp = ctx
    queue = app.state.queue
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 0})
    queue.run_once()
    first = client.get(f"/api/books/{book}/chapters/0").json()["audio_url"]
    assert "?v=" in first
    mp3 = app.state.library.chapter_audio_path(book, 0)
    import os
    st = mp3.stat()
    os.utime(mp3, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # re-recorded later
    assert client.get(f"/api/books/{book}/chapters/0").json()["audio_url"] != first
    assert client.get(first).status_code == 200


def test_vocab_rejects_a_chapter_that_does_not_exist(ctx, book):
    client, *_ = ctx
    r = client.post("/api/vocab", json={"book_id": book, "chapter": -1, "para": 0,
                                        "start_char": 0, "end_char": 4})
    assert r.status_code == 404
    assert client.get("/api/vocab").json() == []


@pytest.mark.parametrize("mode", ["next5", "all"])
def test_unused_batch_convert_modes_are_gone(ctx, book, mode):
    client, *_ = ctx
    assert client.post(f"/api/books/{book}/convert", json={"mode": mode}).status_code == 400


def test_delete_only_the_audio_of_a_book(ctx, book):
    """书架 → 删除 → 只删音频: chapters go back to 未准备; text, Chinese, progress and 生词本 stay."""
    client, app, synth, tmp = ctx
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 0})
    app.state.queue.run_once()
    rec = client.post("/api/vocab", json={"book_id": book, "chapter": 0, "para": 0,
                                           "start_char": 0, "end_char": 4}).json()
    use = client.get("/api/storage").json()
    assert use["books"][book]["audio"] > 0 and use["books"][book]["other"] > 0
    assert use["total"] >= use["books"][book]["audio"] + use["books"][book]["other"]

    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 1})  # queued, not run
    r = client.delete(f"/api/books/{book}/audio")
    assert r.status_code == 409 and "生成音频" in r.json()["detail"]
    app.state.queue.run_once()

    assert client.delete(f"/api/books/{book}/audio").status_code == 204
    chs = client.get(f"/api/books/{book}").json()["chapters"]
    assert all(c["status"] == "none" for c in chs)
    assert client.get("/api/storage").json()["books"][book]["audio"] == 0
    assert client.get(f"/api/books/{book}/chapters/0").status_code == 200  # text still readable
    assert client.get(f"/api/vocab/{rec['id']}/clip.mp3").status_code == 200

    # deleting the whole book keeps the 生词本 entry and its clip too
    assert client.delete(f"/api/books/{book}").status_code == 204
    assert [v["id"] for v in client.get("/api/vocab").json()] == [rec["id"]]
    assert client.get(f"/api/vocab/{rec['id']}/clip.mp3").status_code == 200
    assert client.delete("/api/books/" + "0" * 32 + "/audio").status_code == 404


def test_startup_sweeps_leftovers(tmp_path):
    home = tmp_path / "home"
    ghost = home / "books" / ("0" * 32)
    ghost.mkdir(parents=True)
    (ghost / "source.epub").write_bytes(b"x")
    app = create_app(home, CountingSynth(), start_worker=False)
    with TestClient(app, base_url=BASE):
        assert not ghost.exists()


def test_book_and_queue_carry_what_the_estimates_need(ctx, book):
    client, app, synth, tmp = ctx
    est = client.get(f"/api/books/{book}").json()["estimate"]
    assert set(est) == {"conv_cps", "audio_cps", "audio_bytes_per_sec", "measured"}
    assert est["audio_bytes_per_sec"] == 6000
    disk = client.get(f"/api/books/{book}").json()["disk"]
    assert disk["audio"] == 0 and disk["other"] > 0
    assert client.get("/api/queue").json()["eta"] == {}
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 1})
    q = client.get("/api/queue").json()
    chars = client.get(f"/api/books/{book}").json()["chapters"][1]["chars"]
    assert q["eta"][book] == pytest.approx(chars / est["conv_cps"])



def test_estimates_follow_the_books_engine(ctx, book, monkeypatch):
    from kokoro_reader.estimate import CONV_CPS, EDGE_CONV_CPS
    monkeypatch.setenv("KOKORO_READER_EDGE", "1")
    client, app, synth, tmp = ctx
    assert client.get(f"/api/books/{book}").json()["estimate"]["conv_cps"] == CONV_CPS
    assert client.put(f"/api/books/{book}/voice", json={"voice": "en-GB-SoniaNeural"}).status_code == 200
    assert client.get(f"/api/books/{book}").json()["estimate"]["conv_cps"] == EDGE_CONV_CPS
    client.post(f"/api/books/{book}/convert", json={"mode": "one", "chapter": 1})
    chars = client.get(f"/api/books/{book}").json()["chapters"][1]["chars"]
    assert client.get("/api/queue").json()["eta"][book] == pytest.approx(chars / EDGE_CONV_CPS)



def test_microsoft_voices_only_when_switched_on(ctx, book, monkeypatch):
    client, app, synth, tmp = ctx
    ms = "en-GB-SoniaNeural"
    assert ms not in {v["id"] for v in client.get("/api/voices").json()}
    assert client.put(f"/api/books/{book}/voice", json={"voice": ms}).status_code == 400
    assert client.get(f"/api/voices/{ms}/preview.mp3").status_code == 404
    monkeypatch.setenv("KOKORO_READER_EDGE", "1")
    assert ms in {v["id"] for v in client.get("/api/voices").json()}
    assert client.put(f"/api/books/{book}/voice", json={"voice": ms}).status_code == 200


# ---- 试听 made ahead: picking a voice plays at once ----
def _wait(cond, timeout=20.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_previews_are_made_ahead_for_every_voice(tmp_path):
    home, prev = tmp_path / "h", CountingSynth()
    app = create_app(home, CountingSynth(), start_worker=False, preview_synth=prev, warm_previews=True)
    ids = [v["id"] for v in VOICES if v["engine"] == "kokoro"]
    with TestClient(app, base_url=BASE) as c:
        assert _wait(lambda: all((home / "previews" / f"{i}.mp3").exists() for i in ids))
        assert prev.calls == len(ids)
        assert c.get(f"/api/voices/{ids[-1]}/preview.mp3").status_code == 200
        assert prev.calls == len(ids)  # served from disk, not made again


def test_warming_previews_skips_ones_on_disk_and_survives_a_failure(tmp_path):
    home = tmp_path / "h"
    (home / "previews").mkdir(parents=True)
    (home / "previews" / "af_heart.mp3").write_bytes(b"old")

    class Flaky(CountingSynth):
        def synth_chapter(self, paras, voice, *a, **kw):
            if voice == "am_adam":
                raise RuntimeError("boom")
            return super().synth_chapter(paras, voice, *a, **kw)

    prev = Flaky()
    app = create_app(home, CountingSynth(), start_worker=False, preview_synth=prev, warm_previews=True)
    with TestClient(app, base_url=BASE):
        assert _wait(lambda: (home / "previews" / "bm_lewis.mp3").exists())
    assert (home / "previews" / "af_heart.mp3").read_bytes() == b"old"
    assert not (home / "previews" / "am_adam.mp3").exists()
    assert not list((home / "previews").glob("*.tmp"))


def test_listing_voices_fills_in_missing_previews(tmp_path, monkeypatch):
    home, prev = tmp_path / "h", CountingSynth()
    app = create_app(home, CountingSynth(), start_worker=False, preview_synth=prev, warm_previews=True)
    with TestClient(app, base_url=BASE) as c:
        target = home / "previews" / "bf_emma.mp3"
        assert _wait(target.exists)
        assert _wait(lambda: not app.state.previews_warming())
        target.unlink()  # e.g. a voice that just became available (Microsoft switched on)
        c.get("/api/voices")
        assert _wait(target.exists)


def test_no_previews_made_ahead_unless_asked(ctx):
    client, app, _, tmp = ctx
    import time
    time.sleep(0.3)
    assert app.state.preview_synth.calls == 0
