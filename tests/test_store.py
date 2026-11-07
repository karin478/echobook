import pytest

from kokoro_reader import config
from kokoro_reader.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite")


def mk(store, name):
    return store.add_book(name, "auth", f"hash-{name}")


def chapters(n=2):
    return [
        {"idx": i, "title": f"Ch {i}", "part": None, "is_matter": False,
         "chars": 100 + i, "first_line": f"line {i}"}
        for i in range(n)
    ]


def test_books_order_and_reorder(store):
    a, b, c = mk(store, "a"), mk(store, "b"), mk(store, "c")
    assert [x["id"] for x in store.list_books()] == [a, b, c]
    store.reorder_books([c, a, b])
    assert [x["id"] for x in store.list_books()] == [c, a, b]
    with pytest.raises(ValueError):
        store.reorder_books([a, b])


def test_find_by_hash(store):
    a = mk(store, "a")
    assert store.find_book_by_hash("hash-a")["id"] == a
    assert store.find_book_by_hash("nope") is None
    assert store.get_book(a)["voice"] == config.DEFAULT_VOICE


def test_set_book_voice(store):
    a = mk(store, "a")
    store.set_book_voice(a, "bf_emma")
    assert store.get_book(a)["voice"] == "bf_emma"


def test_chapters_roundtrip_defaults(store):
    a = mk(store, "a")
    store.add_chapters(a, chapters())
    ch = store.list_chapters(a)
    assert [c["idx"] for c in ch] == [0, 1]
    assert ch[0]["status"] == "none"
    assert ch[0]["duration"] is None
    assert ch[0]["read_ratio"] == 0
    assert ch[0]["is_matter"] is False
    assert ch[0]["title"] == "Ch 0" and ch[0]["chars"] == 100


def test_chapter_setters(store):
    a = mk(store, "a")
    store.add_chapters(a, chapters())
    store.set_chapter_status(a, 0, "done", duration=12.5, voice="af_heart")
    store.set_chapter_matter(a, 1, True)
    store.set_read_ratio(a, 1, 0.5)
    ch = store.list_chapters(a)
    assert ch[0]["status"] == "done" and ch[0]["duration"] == 12.5
    assert ch[0]["voice"] == "af_heart"
    assert ch[1]["is_matter"] is True and ch[1]["read_ratio"] == 0.5


def test_reset_stuck(store):
    a = mk(store, "a")
    store.add_chapters(a, chapters(3))
    store.set_chapter_status(a, 0, "converting")
    store.set_chapter_status(a, 1, "queued")
    store.set_chapter_status(a, 2, "done", duration=1.0, voice="af_heart")
    assert store.reset_stuck() == 2
    st = [c["status"] for c in store.list_chapters(a)]
    assert st == ["none", "none", "done"]


def test_reset_stuck_keeps_audio_of_a_chapter_queued_for_a_new_voice(store):
    a = mk(store, "a")
    store.add_chapters(a, chapters(2))
    store.set_chapter_status(a, 0, "done", duration=3.0, voice="af_heart")
    store.set_chapter_status(a, 0, "queued")  # 用新音色重录, then the service restarts
    store.set_chapter_status(a, 1, "converting")
    store.reset_stuck()
    st = [c["status"] for c in store.list_chapters(a)]
    assert st == ["done", "none"]  # its old audio is still there and playable


def test_delete_book_cascades(store):
    a = mk(store, "a")
    store.add_chapters(a, chapters())
    v = store.add_vocab({"book_id": a, "text": "hi", "sentence": "hi there"})
    store.set_progress(a, 0, 1, 2, 3.0)
    store.delete_book(a)
    assert store.get_book(a) is None
    assert store.list_chapters(a) == []
    assert store.get_vocab(v) is None
    assert store.list_vocab() == []
    assert store.get_progress(a) is None


def test_progress_roundtrip(store):
    a = mk(store, "a")
    assert store.get_progress(a) is None
    store.set_progress(a, 3, 4, 5, 6.5)
    assert store.get_progress(a) == {"chapter": 3, "para": 4, "word": 5, "time": 6.5}
    store.set_progress(a, 4, 0, 0, 0.0)
    assert store.get_progress(a)["chapter"] == 4


def test_vocab_list_newest_first(store):
    a = mk(store, "a")
    v1 = store.add_vocab({"book_id": a, "text": "one", "sentence": "s1"})
    v2 = store.add_vocab({"book_id": a, "text": "two", "sentence": "s2", "status": "new"})
    assert [v["id"] for v in store.list_vocab()] == [v2, v1]
    got = store.get_vocab(v1)
    assert got["text"] == "one" and got["id"] == v1 and "created_at" in got
    store.delete_vocab(v1)
    assert store.get_vocab(v1) is None


def test_home_env_override(home, monkeypatch):
    assert config.home() == home
    sub = home / "x" / "y"
    monkeypatch.setenv("KOKORO_READER_HOME", str(sub))
    assert config.home() == sub and sub.is_dir()


def test_port_env_override(monkeypatch):
    monkeypatch.delenv("KOKORO_READER_PORT", raising=False)
    assert config.port() == 7788
    monkeypatch.setenv("KOKORO_READER_PORT", "54321")
    assert config.port() == 54321
    monkeypatch.setenv("KOKORO_READER_PORT", "not-a-port")
    with pytest.raises(ValueError):
        config.port()


def test_main_uses_port_and_localhost(home, monkeypatch):
    import kokoro_reader.__main__ as entry

    seen = {}
    monkeypatch.setenv("KOKORO_READER_FAKE_TTS", "1")
    monkeypatch.setenv("KOKORO_READER_PORT", "54322")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: seen.update(kw))
    entry.main()
    assert seen["host"] == "127.0.0.1" and seen["port"] == 54322
    assert seen["access_log"] is False  # the UI polls; access lines would flood the launchd log


def test_constants():
    assert (config.HOST, config.PORT) == ("127.0.0.1", 7788)
    assert config.DEFAULT_VOICE == "af_heart"
    assert config.SAMPLE_RATE == 24000
    assert config.PARA_GAP == 0.2
    assert config.EDGE_KEEP == 0.2  # max silence kept at each end of a Kokoro chunk
    assert config.MP3_BITRATE == "96k"


def test_add_vocab_accepts_caller_supplied_id(store):
    a = store.add_book("A", None, "h1")
    vid = store.add_vocab({"id": "abc123", "book_id": a, "text": "hi", "sentence": "hi there"})
    assert vid == "abc123"
    assert store.get_vocab("abc123")["text"] == "hi"
