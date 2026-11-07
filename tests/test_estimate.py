"""预计时间 / 大小: how long converting takes and how much disk the audio needs, learned from this
machine's own past conversions (a sensible default until there are some)."""

import json

import pytest

from kokoro_reader.estimate import AUDIO_CPS, CONV_CPS, SpeedLog, eta_by_book


def test_defaults_until_something_was_measured(tmp_path):
    r = SpeedLog(tmp_path / "speed.json").rates()
    assert r == {"conv_cps": CONV_CPS, "audio_cps": AUDIO_CPS, "audio_bytes_per_sec": 6000, "measured": 0}


def test_rates_follow_the_recent_conversions_and_survive_a_restart(tmp_path):
    p = tmp_path / "speed.json"
    log = SpeedLog(p)
    log.record(chars=10_000, seconds=200, audio_seconds=500)
    log.record(chars=30_000, seconds=200, audio_seconds=1500)
    r = SpeedLog(p).rates()
    assert r["conv_cps"] == pytest.approx(40_000 / 400)
    assert r["audio_cps"] == pytest.approx(40_000 / 2000)
    assert r["measured"] == 2
    log.record(chars=0, seconds=3, audio_seconds=0)   # empty chapters tell nothing
    log.record(chars=100, seconds=0.0, audio_seconds=5)
    assert SpeedLog(p).rates()["measured"] == 2


def test_only_the_last_runs_count(tmp_path):
    log = SpeedLog(tmp_path / "speed.json", keep=3)
    for _ in range(5):
        log.record(chars=100, seconds=100, audio_seconds=10)
    for _ in range(3):
        log.record(chars=900, seconds=100, audio_seconds=50)
    assert log.rates()["conv_cps"] == pytest.approx(9.0)
    assert len(json.loads((tmp_path / "speed.json").read_text())) == 3


def test_a_broken_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "speed.json"
    p.write_text("{nope")
    assert SpeedLog(p).rates()["measured"] == 0


def test_eta_by_book_walks_the_queue_in_order():
    chars = {("a", 0): 1000, ("a", 1): 2000, ("b", 0): 500, ("a", 2): 1000}
    snap = {"current": {"book_id": "a", "idx": 0, "progress": 0.5},
            "pending": [["a", 1], ["b", 0], ["a", 2]]}
    eta = eta_by_book(snap, lambda b, i: chars[(b, i)], conv_cps=100)
    # a: 5 s left of ch0, 20 s ch1, (b waits 5 s), 10 s ch2 → a done after 40 s; b after 30 s
    assert eta == {"a": pytest.approx(40), "b": pytest.approx(30)}
    assert eta_by_book({"current": None, "pending": []}, lambda b, i: 0, 100) == {}
