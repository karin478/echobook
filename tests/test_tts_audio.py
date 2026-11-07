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


def test_voices_default_first_english_only():
    assert tts.VOICES[0]["id"] == DEFAULT_VOICE
    assert len(tts.VOICES) == 28
    assert all(v["id"][0] in "ab" for v in tts.VOICES)
    assert {v["accent"] for v in tts.VOICES} == {"美式", "英式"}
    assert {v["gender"] for v in tts.VOICES} == {"女", "男"}
    assert all(set(v) == {"id", "label", "accent", "gender"} for v in tts.VOICES)


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
