import re
import subprocess
import numpy as np
import pytest

from kokoro_reader import audio, tts
from kokoro_reader.config import DEFAULT_VOICE, PARA_GAP, SAMPLE_RATE


def _duration(path) -> float:
    out = subprocess.run(
        ["/opt/homebrew/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def test_fake_synth_timestamps_absolute_and_gap():
    samples, toks = tts.FakeSynth().synth_chapter(["Hello big world.", "Second para"], "af_heart")
    assert samples.dtype == np.float32 and not samples.any()
    p0 = [t for t in toks if t["p"] == 0]
    p1 = [t for t in toks if t["p"] == 1]
    assert [t["w"] for t in p0] == ["Hello", "big", "world", "."]
    assert p0[-1]["a"] == p0[-1]["b"]
    last_end = max(t["b"] for t in p0)
    assert p1[0]["a"] == pytest.approx(last_end + PARA_GAP)
    times = [x for t in toks for x in (t["a"], t["b"]) if x is not None]
    assert times == sorted(times)
    assert len(samples) == pytest.approx((max(t["b"] for t in p1) + PARA_GAP) * SAMPLE_RATE, abs=2)


def test_fake_synth_empty():
    for paras in ([], [""]):
        samples, toks = tts.FakeSynth().synth_chapter(paras, "af_heart")
        assert len(samples) == 0 and toks == []


def test_fake_synth_scene_break_silent_no_gap():
    samples, toks = tts.FakeSynth().synth_chapter(["One two", "• • •", "Three"], "af_heart")
    assert {t["p"] for t in toks} == {0, 2}
    p2 = [t for t in toks if t["p"] == 2]
    # silent paragraph adds neither tokens nor gap
    assert p2[0]["a"] == pytest.approx(0.6 + PARA_GAP)


def test_fake_synth_progress():
    seen = []
    tts.FakeSynth().synth_chapter(["a b", "• • •", "c"], "af_heart", on_progress=seen.append)
    assert seen == [pytest.approx(1 / 3), pytest.approx(2 / 3), 1.0]
    tts.FakeSynth().synth_chapter(["a"], "af_heart", on_progress=None)


def test_write_mp3_and_duration(tmp_path):
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    dst = tmp_path / "a.mp3"
    d = audio.write_mp3((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), dst)
    assert d == pytest.approx(1.0, abs=0.01)
    assert _duration(dst) == pytest.approx(1.0, abs=0.1)


def test_write_mp3_empty(tmp_path):
    dst = tmp_path / "a.mp3"
    assert audio.write_mp3(np.zeros(0, dtype=np.float32), dst) == 0.0
    assert not dst.exists()


def test_cut_clip(tmp_path):
    t = np.arange(3 * SAMPLE_RATE) / SAMPLE_RATE
    src = tmp_path / "s.mp3"
    audio.write_mp3((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), src)
    dst = tmp_path / "c.mp3"
    audio.cut_clip(src, 1.0, 2.0, dst)
    assert _duration(dst) == pytest.approx(1.3, abs=0.1)


def test_voices_kokoro_and_edge_english_only():
    kokoro = [v for v in tts.VOICES if v["engine"] == "kokoro"]
    edge = [v for v in tts.VOICES if v["engine"] == "edge"]
    assert len(kokoro) == 28 and all(v["id"][0] in "ab" for v in kokoro)
    assert len(edge) == 17 and all(re.fullmatch(r"en-(US|GB)-\w+Neural", v["id"]) for v in edge)
    assert {v["id"] for v in edge} == set(tts.EDGE_VOICE_IDS)
    assert len(kokoro) + len(edge) == len(tts.VOICES) == len({v["id"] for v in tts.VOICES})
    assert {v["accent"] for v in tts.VOICES} == {"美式", "英式"}
    assert {v["gender"] for v in tts.VOICES} == {"女", "男"}
    assert all(set(v) == {"id", "label", "accent", "gender", "engine", "default"} for v in tts.VOICES)
    assert [v["id"] for v in tts.VOICES if v["default"]] == [DEFAULT_VOICE]


def test_synth_preview_uses_fixed_sentence():
    samples = tts.synth_preview(tts.FakeSynth(), "af_heart")
    assert len(samples) > 0


class _Tok:
    def __init__(self, text, ws, a, b):
        self.text, self.whitespace, self.start_ts, self.end_ts = text, ws, a, b


class _Res:
    def __init__(self, n, tokens, torch_like=False, audio=None):
        arr = np.ones(n, dtype=np.float32) if audio is None else audio
        if torch_like:
            class T:
                def numpy(self_inner):
                    return arr
            self.audio = T()
        else:
            self.audio = arr
        self.tokens = tokens


def test_kokoro_synth_offsets_untimed_gap_progress():
    calls = []

    def pipe(text, voice):
        calls.append((text, voice))
        if text == "x":
            yield _Res(24000, [_Tok("x", " ", 0.0, 0.5), _Tok(",", "", None, None)])
            yield _Res(12000, [_Tok("x2", "\n", 0.1, 0.3)], torch_like=True)
        else:
            yield _Res(6000, [_Tok("y", "", 0.2, 0.4)])

    synth = tts.KokoroSynth()
    synth._pipes["a"] = pipe
    seen = []
    samples, toks = synth.synth_chapter(["x", "• • •", "y"], "af_heart", on_progress=seen.append)
    assert calls == [("x", "af_heart"), ("y", "af_heart")]
    assert toks == [
        {"p": 0, "w": "x", "s": " ", "a": 0.0, "b": 0.5},
        {"p": 0, "w": ",", "s": "", "a": None, "b": None},
        {"p": 0, "w": "x2", "s": "\n", "a": 1.1, "b": 1.3},
        {"p": 2, "w": "y", "s": "", "a": pytest.approx(1.5 + PARA_GAP + 0.2), "b": pytest.approx(1.5 + PARA_GAP + 0.25)},  # end clamped to its 0.25 s chunk
    ]
    gap = int(PARA_GAP * SAMPLE_RATE)
    assert len(samples) == 24000 + 12000 + gap + 6000 + gap  # one gap after x, none after "• • •", trailing after y
    assert seen == [pytest.approx(1 / 3), pytest.approx(2 / 3), 1.0]


def _padded(lead, body, tail):
    sr = SAMPLE_RATE
    return np.concatenate([np.zeros(int(lead * sr)), np.full(int(body * sr), 0.5), np.zeros(int(tail * sr))]).astype(np.float32)


def test_trim_silence_keeps_at_most_edge_keep_each_side():
    out, cut = audio.trim_silence(_padded(0.5, 1.0, 0.6), keep=0.2)
    assert cut == pytest.approx(0.3, abs=0.01)
    assert len(out) / SAMPLE_RATE == pytest.approx(1.4, abs=0.01)


def test_trim_silence_leaves_short_edges_and_all_silent_audio_alone():
    short = _padded(0.1, 1.0, 0.05)
    out, cut = audio.trim_silence(short, keep=0.2)
    assert cut == 0 and len(out) == len(short)
    silent = np.zeros(SAMPLE_RATE, dtype=np.float32)
    out, cut = audio.trim_silence(silent, keep=0.2)
    assert cut == 0 and len(out) == len(silent)


def test_kokoro_synth_trims_chunk_edges_and_shifts_tokens():
    def pipe(text, voice):
        yield _Res(0, [_Tok(text, "", 0.6, 1.0)], audio=_padded(0.5, 1.0, 0.6))

    synth = tts.KokoroSynth()
    synth._pipes["a"] = pipe
    samples, toks = synth.synth_chapter(["x", "y"], "af_heart")
    # each paragraph: 0.2 lead + 1.0 speech + 0.2 tail, then PARA_GAP
    assert toks[0]["a"] == pytest.approx(0.3, abs=0.01)
    assert toks[0]["b"] == pytest.approx(0.7, abs=0.01)
    assert toks[1]["a"] == pytest.approx(1.4 + PARA_GAP + 0.3, abs=0.02)
    assert len(samples) / SAMPLE_RATE == pytest.approx(2 * (1.4 + PARA_GAP), abs=0.02)


def test_fake_synth_delay_from_env(monkeypatch):
    """KOKORO_READER_FAKE_DELAY (seconds) sleeps once per paragraph; default is no delay."""
    slept = []
    monkeypatch.setattr(tts.time, "sleep", slept.append)
    monkeypatch.setenv("KOKORO_READER_FAKE_DELAY", "0.25")
    tts.FakeSynth().synth_chapter(["One two", "• • •", "Three"], "af_heart")
    assert slept == [0.25, 0.25, 0.25]

    slept.clear()
    monkeypatch.delenv("KOKORO_READER_FAKE_DELAY")
    tts.FakeSynth().synth_chapter(["One two", "Three"], "af_heart")
    assert slept == []


@pytest.mark.parametrize("env, mps, want", [
    (None, True, "mps"),     # Apple GPU available: default to it (~1.6x faster, same timestamps)
    (None, False, "cpu"),
    ("cpu", True, "cpu"),    # KOKORO_READER_DEVICE forces a device
    ("mps", True, "mps"),
])
def test_pick_device(monkeypatch, env, mps, want):
    if env is None:
        monkeypatch.delenv("KOKORO_READER_DEVICE", raising=False)
    else:
        monkeypatch.setenv("KOKORO_READER_DEVICE", env)
    monkeypatch.setattr(tts, "_mps_available", lambda: mps)
    assert tts.pick_device() == want


def test_kokoro_synth_builds_pipeline_on_its_device(monkeypatch):
    import sys
    import types
    made = []

    class FakePipeline:
        def __init__(self, **kw):
            made.append(kw)

    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=FakePipeline))
    monkeypatch.setattr(tts, "pick_device", lambda: "mps")
    synth = tts.KokoroSynth()
    synth._pipe("af_heart")
    synth._pipe("af_bella")  # same lang: cached, not rebuilt
    assert made == [{"lang_code": "a", "repo_id": "hexgrad/Kokoro-82M", "device": "mps"}]
    assert tts.KokoroSynth(device="cpu").device == "cpu"


def test_ffmpeg_is_found_off_homebrew_too(tmp_path, monkeypatch):
    """KOKORO_READER_FFMPEG wins; then Homebrew's on Apple Silicon (launchd's PATH lacks it);
    else whatever `ffmpeg` is on PATH."""
    from kokoro_reader import audio

    fake = tmp_path / "bin" / "ffmpeg"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(audio, "HOMEBREW_FFMPEG", tmp_path / "nope" / "ffmpeg")
    monkeypatch.delenv("KOKORO_READER_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(fake.parent))
    assert audio.ffmpeg_path() == str(fake)
    monkeypatch.setenv("KOKORO_READER_FFMPEG", "/custom/ffmpeg")
    assert audio.ffmpeg_path() == "/custom/ffmpeg"
    monkeypatch.setattr(audio, "HOMEBREW_FFMPEG", fake)
    monkeypatch.delenv("KOKORO_READER_FFMPEG")
    assert audio.ffmpeg_path() == str(fake)


def _bitrate(p):
    import subprocess
    out = subprocess.run([audio.ffmpeg_path().replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
                          "format=bit_rate", "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def test_speech_is_stored_at_48k_mono(tmp_path):
    """Kokoro speaks 24 kHz mono: 48 kbps sounds the same as 96 and is half the disk (~22 MB/hour)."""
    t = np.arange(5 * SAMPLE_RATE) / SAMPLE_RATE
    src = tmp_path / "s.mp3"
    audio.write_mp3((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), src)
    audio.cut_clip(src, 1.0, 4.0, tmp_path / "c.mp3")
    for p in (src, tmp_path / "c.mp3"):
        assert 44_000 <= _bitrate(p) <= 52_000


# ---- edge-tts (Microsoft Natural voices) ----

def test_edge_tokens_rebuild_text_with_word_times():
    text = "He said, \u201cHi\u2014there.\u201d Then left."
    words = [("He", 0.1, 0.2), ("said", 0.2, 0.4), ("Hi", 0.5, 0.6), ("there", 0.6, 0.8),
             ("Then", 1.0, 1.2), ("left", 1.2, 1.4)]
    toks = tts.edge_tokens(3, text, words)
    assert "".join(t["w"] + t["s"] for t in toks) == text
    assert all(t["p"] == 3 and not re.search(r"\s", t["w"]) for t in toks)
    timed = [(t["w"], t["a"], t["b"]) for t in toks if t["a"] is not None]
    assert timed == words
    assert {t["w"] for t in toks if t["a"] is None} <= {",", "\u201c", "\u2014", ".\u201d", "."}


def test_edge_tokens_leave_unmatched_words_untimed():
    text = "Fish & chips, please."
    toks = tts.edge_tokens(0, text, [("Fish", 0.0, 0.3), ("&amp;", 0.3, 0.4), ("chips", 0.4, 0.7),
                                     ("nonsense", 0.7, 0.8), ("please", 0.8, 1.0)])
    assert "".join(t["w"] + t["s"] for t in toks) == text
    timed = [t["w"] for t in toks if t["a"] is not None]
    assert timed == ["Fish", "&", "chips", "please"]  # html-escaped boundary text is unescaped; unknown words skipped


def test_edge_synth_assembles_paragraphs_with_trim_gap_and_progress():
    calls = []

    def fetch(text, voice):
        calls.append((text, voice))
        # 0.5 s lead silence, 1 s speech, 0.6 s tail; the word sits in the speech
        return _padded(0.5, 1.0, 0.6), [(text, 0.6, 1.0)]

    seen = []
    samples, toks = tts.EdgeSynth(fetch=fetch).synth_chapter(["x", "\u2022 \u2022 \u2022", "y"], "en-US-AriaNeural",
                                                             on_progress=seen.append)
    assert sorted(calls) == [("x", "en-US-AriaNeural"), ("y", "en-US-AriaNeural")]  # no request for "• • •"
    timed = [t for t in toks if t["a"] is not None]
    assert [t["w"] for t in timed] == ["x", "y"]
    assert timed[0]["a"] == pytest.approx(0.3, abs=0.01)  # 0.6 minus the 0.3 s trimmed lead
    assert timed[1]["a"] == pytest.approx(1.4 + PARA_GAP + 0.3, abs=0.02)
    assert [t["p"] for t in toks] == [0, 2]
    assert len(samples) / SAMPLE_RATE == pytest.approx(2 * (1.4 + PARA_GAP), abs=0.02)
    assert seen[-1] == 1.0 and seen == sorted(seen)


def test_edge_synth_retries_flaky_requests(monkeypatch):
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    n = {"x": 0}

    def flaky(text, voice):
        n["x"] += 1
        if n["x"] < 3:
            raise RuntimeError("No audio was received")
        return _padded(0.1, 0.5, 0.1), [(text, 0.1, 0.6)]

    _, toks = tts.EdgeSynth(fetch=flaky).synth_chapter(["x"], "en-US-AriaNeural")
    assert n["x"] == 3 and toks[0]["w"] == "x"

    def dead(text, voice):
        raise RuntimeError("403")

    with pytest.raises(RuntimeError):
        tts.EdgeSynth(fetch=dead).synth_chapter(["x"], "en-US-AriaNeural")


def test_routing_synth_sends_edge_voices_to_edge(monkeypatch):
    monkeypatch.setenv("KOKORO_READER_EDGE", "1")
    class Rec:
        def __init__(self, name):
            self.name, self.got = name, []

        def synth_chapter(self, paragraphs, voice, on_progress=None):
            self.got.append(voice)
            return np.zeros(1, dtype=np.float32), []

    k, e = Rec("k"), Rec("e")
    r = tts.RoutingSynth(kokoro=k, edge=e)
    r.synth_chapter(["a"], "af_heart")
    r.synth_chapter(["a"], "en-GB-SoniaNeural")
    assert (k.got, e.got) == (["af_heart"], ["en-GB-SoniaNeural"])
    assert isinstance(tts.RoutingSynth().kokoro, tts.KokoroSynth)  # no-arg construction (preview synth)


def test_main_serves_both_engines(monkeypatch):
    import kokoro_reader.__main__ as entry
    seen = {}
    monkeypatch.delenv("KOKORO_READER_FAKE_TTS", raising=False)
    monkeypatch.setattr(entry, "create_app", lambda home, synth: seen.setdefault("synth", synth))
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: None)
    entry.main()
    assert isinstance(seen["synth"], tts.RoutingSynth)



def test_engine_of_voice():
    assert tts.engine_of("af_heart") == "kokoro"
    assert tts.engine_of("en-US-AvaMultilingualNeural") == "edge"



# ---- Microsoft voices are opt-in (KOKORO_READER_EDGE) ----

def _edge_ids(vs):
    return {v["id"] for v in vs if v["engine"] == "edge"}


def test_edge_voices_are_off_by_default(monkeypatch):
    assert _edge_ids(tts.voices()) == set() and not tts.edge_enabled()
    assert {v["id"] for v in tts.voices()} == {v["id"] for v in tts.VOICES if v["engine"] == "kokoro"}
    monkeypatch.setenv("KOKORO_READER_EDGE", "1")
    assert tts.edge_enabled() and _edge_ids(tts.voices()) == set(tts.EDGE_VOICE_IDS)
    monkeypatch.setenv("KOKORO_READER_EDGE", "0")
    assert not tts.edge_enabled()


def test_edge_switch_can_live_in_the_users_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=x\nKOKORO_READER_EDGE=1\n")
    monkeypatch.setenv("KOKORO_TRANSLATE_ENV", str(env))
    assert tts.edge_enabled()
    monkeypatch.setenv("KOKORO_READER_EDGE", "0")  # the process environment wins over the file
    assert not tts.edge_enabled()


def test_edge_needs_the_package(monkeypatch):
    monkeypatch.setenv("KOKORO_READER_EDGE", "1")
    monkeypatch.setattr(tts, "_edge_installed", lambda: False)
    assert not tts.edge_enabled() and _edge_ids(tts.voices()) == set()


def test_routing_synth_refuses_edge_voices_when_off():
    class Never:
        def synth_chapter(self, *a, **kw):
            raise AssertionError("should not be called")

    with pytest.raises(RuntimeError, match="KOKORO_READER_EDGE"):
        tts.RoutingSynth(kokoro=Never(), edge=Never()).synth_chapter(["a"], "en-US-AriaNeural")
