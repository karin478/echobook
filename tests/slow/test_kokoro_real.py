import re
import pytest

from kokoro_reader.tts import KokoroSynth


@pytest.mark.slow
def test_kokoro_tokens_cover_text():
    paras = ["It was a bright cold day in April.", "The clocks were striking thirteen, wasn't it?"]
    seen = []
    samples, toks = KokoroSynth().synth_chapter(paras, "af_heart", on_progress=seen.append)
    assert len(samples) > 0
    joined = "".join(t["w"] + t["s"] for t in toks)
    strip = lambda s: re.sub(r"\s+", "", s)
    assert strip(joined) == strip("".join(paras))
    times = [x for t in toks for x in (t["a"], t["b"]) if x is not None]
    assert times and times == sorted(times)
    assert seen[-1] == 1.0
