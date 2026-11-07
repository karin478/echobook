"""Real edge-tts (network; `-m slow`): tokens rebuild the text and carry ordered word times."""

import re

import pytest

from kokoro_reader.tts import EdgeSynth

pytestmark = pytest.mark.slow


def test_edge_tokens_cover_text_with_ordered_times():
    paras = ["“It was a bright cold day in April,” she said.", "• • •",
             "The clocks were striking thirteen—weren’t they?"]
    seen = []
    samples, toks = EdgeSynth().synth_chapter(paras, "en-US-AriaNeural", on_progress=seen.append)
    assert len(samples) > 0 and seen[-1] == 1.0
    for pi in (0, 2):
        assert "".join(t["w"] + t["s"] for t in toks if t["p"] == pi) == paras[pi]
    times = [x for t in toks for x in (t["a"], t["b"]) if x is not None]
    assert len([t for t in toks if t["a"] is not None]) >= 14
    assert times == sorted(times) and times[-1] <= len(samples) / 24000
