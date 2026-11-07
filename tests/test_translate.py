import json
import re
from pathlib import Path

import pytest

from kokoro_reader import translate as tr


# ---------------------------------------------------------------- helpers under test
def test_sentence_spans_are_stripped_and_skip_blank():
    text = "Hello there.  How are you?   "
    spans = tr.sentence_spans(text)
    assert [text[a:b] for a, b in spans] == ["Hello there.", "How are you?"]
    assert tr.sentence_spans("   ") == []


def test_chunk_groups_consecutive_and_skips_empty():
    paras = ["a" * 10, "", "b" * 10, "c" * 10, "   ", "d" * 10]
    assert tr.chunk(paras, max_paras=2, max_chars=1000) == [[0, 2], [3, 5]]


def test_chunk_respects_char_budget_and_long_paragraph_alone():
    paras = ["x" * 600, "y" * 600, "z" * 5000, "w" * 10]
    assert tr.chunk(paras, max_paras=8, max_chars=1000) == [[0], [1], [2], [3]]


def test_valid_segments_accepts_contiguous_exact_cover():
    zh = "他敲了门。没人应。"
    segs = [{"from": 0, "to": 0, "zh": "他敲了门。"}, {"from": 1, "to": 2, "zh": "没人应。"}]
    assert tr.valid_segments(3, zh, segs)


def test_valid_segments_ignores_whitespace_differences():
    segs = [{"from": 0, "to": 0, "zh": "他敲了门。 "}, {"from": 1, "to": 1, "zh": "\n没人应。"}]
    assert tr.valid_segments(2, "他敲了门。没人应。", segs)


@pytest.mark.parametrize("segs", [
    [{"from": 0, "to": 0, "zh": "他敲了门。"}],                                          # misses sentence 1
    [{"from": 1, "to": 1, "zh": "没人应。"}, {"from": 0, "to": 0, "zh": "他敲了门。"}],     # out of order
    [{"from": 0, "to": 0, "zh": "他敲了门。"}, {"from": 1, "to": 1, "zh": "没有人应。"}],   # text changed
    [{"from": 0, "to": 1, "zh": "他敲了门。没人应。"}, {"from": 2, "to": 2, "zh": ""}],     # empty / overrun
    [{"from": 0, "to": 0, "zh": "他敲了门。"}, {"from": 1, "to": 0, "zh": "没人应。"}],     # backwards
    "not a list",
    [{"from": "0", "to": 0, "zh": "他敲了门。"}, {"from": 1, "to": 1, "zh": "没人应。"}],   # bad types
])
def test_valid_segments_rejects_bad(segs):
    assert not tr.valid_segments(2, "他敲了门。没人应。", segs)


def test_fallback_segment_covers_all():
    assert tr.fallback_segments(3, "整段。") == [{"from": 0, "to": 2, "zh": "整段。"}]


def test_parse_json_tolerates_fences_and_prose():
    assert tr.parse_json('```json\n["a", "b"]\n```') == ["a", "b"]
    assert tr.parse_json('Here you go:\n{"x": 1}\nThanks') == {"x": 1}
    with pytest.raises(ValueError):
        tr.parse_json("no json here")


# ---------------------------------------------------------------- translate_chapter
class ScriptedTranslator:
    """Translates each paragraph to 〔i〕-tagged sentences so alignment is checkable."""

    model = "fake-model"

    def __init__(self, bad_count_once=False, bad_align=False):
        self.bad_count_once = bad_count_once
        self.bad_align = bad_align
        self.translate_calls = []
        self.align_calls = []

    def translate(self, paras, title, glossary=None):
        self.translate_calls.append(list(paras))
        self.glossaries = getattr(self, "glossaries", []) + [glossary]
        out = ["".join(f"〔{s}〕" for s in tr.sentence_texts(p)) for p in paras]
        if self.bad_count_once and len(paras) > 1:
            self.bad_count_once = False
            return out[:-1]
        return out

    def align(self, items):
        self.align_calls.append(items)
        res = []
        for it in items:
            if self.bad_align:
                res.append([{"from": 0, "to": 0, "zh": "胡说"}])
            else:
                res.append([{"from": i, "to": i, "zh": f"〔{s}〕"} for i, s in enumerate(it["sents"])])
        return res


PARAS = [
    "He knocked twice. Nobody answered.",
    "",
    "She waited.",
    "The wind moved. The gate swung. Then silence.",
]


def test_translate_chapter_aligns_every_sentence():
    t = ScriptedTranslator()
    out = tr.translate_chapter(PARAS, t, title="Book", workers=2)
    assert out["model"] == "fake-model"
    p0, p1, p2, p3 = out["paragraphs"]
    assert p0["sents"] == ["He knocked twice.", "Nobody answered."]
    assert p0["segs"] == [{"from": 0, "to": 0, "zh": "〔He knocked twice.〕"},
                          {"from": 1, "to": 1, "zh": "〔Nobody answered.〕"}]
    assert p1 == {"sents": [], "zh": "", "segs": []}
    assert p2["segs"] == [{"from": 0, "to": 0, "zh": "〔She waited.〕"}]  # single sentence: no align call
    assert len(p3["segs"]) == 3
    aligned = [it["sents"] for call in t.align_calls for it in call]
    assert ["She waited."] not in aligned


def test_translate_chapter_retries_then_splits_on_wrong_count():
    t = ScriptedTranslator(bad_count_once=True)
    out = tr.translate_chapter(PARAS, t, workers=1)
    assert all(p["zh"] or not p["sents"] for p in out["paragraphs"])
    assert len(t.translate_calls) >= 2


def test_translate_chapter_falls_back_when_alignment_invalid():
    t = ScriptedTranslator(bad_align=True)
    out = tr.translate_chapter(PARAS, t, workers=1)
    p3 = out["paragraphs"][3]
    assert p3["segs"] == [{"from": 0, "to": 2, "zh": p3["zh"]}]


def test_translate_chapter_reports_progress():
    seen = []
    tr.translate_chapter(PARAS, ScriptedTranslator(), on_progress=seen.append, workers=1)
    assert seen and seen[-1] == 1.0
    assert seen == sorted(seen)


def test_translate_chapter_propagates_translate_failure():
    class Boom(ScriptedTranslator):
        def translate(self, paras, title, glossary=None):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        tr.translate_chapter(PARAS, Boom(), workers=1)


# ---------------------------------------------------------------- fake translator (E2E)
def test_fake_translator_output_aligns():
    f = tr.FakeTranslator()
    paras = ["One. Two.", "Three."]
    zh = f.translate(paras, "T")
    assert len(zh) == 2
    segs = f.align([{"sents": ["One.", "Two."], "zh": zh[0]}])[0]
    assert tr.valid_segments(2, zh[0], segs)


# ---------------------------------------------------------------- config
def test_config_prefers_env_over_env_file(tmp_path, monkeypatch):
    envfile = tmp_path / ".env"
    envfile.write_text('OPENROUTER_API_KEY="file-key"\nDEFAULT_MODEL=file/model\n'
                       "OPENROUTER_API_BASE=https://example.test/v1\n")
    monkeypatch.setenv("KOKORO_TRANSLATE_ENV", str(envfile))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KOKORO_TRANSLATE_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_BASE", raising=False)
    cfg = tr.load_config()
    assert cfg == {"key": "file-key", "model": "file/model", "base": "https://example.test/v1"}
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("KOKORO_TRANSLATE_MODEL", "env/model")
    cfg = tr.load_config()
    assert cfg["key"] == "env-key" and cfg["model"] == "env/model"


def test_config_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_TRANSLATE_ENV", str(tmp_path / "missing.env"))
    for k in ("OPENROUTER_API_KEY", "KOKORO_TRANSLATE_MODEL", "OPENROUTER_API_BASE"):
        monkeypatch.delenv(k, raising=False)
    cfg = tr.load_config()
    assert cfg["key"] is None
    assert cfg["model"] == tr.DEFAULT_MODEL
    assert cfg["base"] == "https://openrouter.ai/api/v1"


def test_default_key_file_is_the_projects_own(tmp_path, monkeypatch):
    """Open source: the key lives in ~/.config/kokoro-reader/.env (never in the repo); the
    repo ships `.env.example` only and ignores `.env`."""
    monkeypatch.delenv("KOKORO_TRANSLATE_ENV", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / ".config" / "kokoro-reader" / ".env"
    f.parent.mkdir(parents=True)
    f.write_text("OPENROUTER_API_KEY=k-from-config\nDEFAULT_MODEL=old/name\nKOKORO_TRANSLATE_MODEL=our/model\n")
    monkeypatch.delenv("KOKORO_TRANSLATE_MODEL", raising=False)
    cfg = tr.load_config()
    assert cfg["key"] == "k-from-config"
    assert cfg["model"] == "our/model"  # the name .env.example uses beats the legacy DEFAULT_MODEL
    repo = Path(tr.__file__).resolve().parents[1]
    example = (repo / ".env.example").read_text()
    assert re.search(r"^OPENROUTER_API_KEY=$", example, re.M)  # a slot, no value
    assert ".env" in (repo / ".gitignore").read_text().split()


def test_missing_key_says_where_to_put_it(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_TRANSLATE_ENV", str(tmp_path / "missing.env"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(tr.TranslateError, match=r"~/\.config/kokoro-reader/\.env"):
        tr.OpenRouterTranslator()


# ---------------------------------------------------------------- OpenRouter request shape
def test_openrouter_translate_builds_request_and_parses(monkeypatch):
    sent = {}

    def fake_post(self, messages, max_tokens):
        sent["messages"] = messages
        return json.dumps(["他敲了两下门。", "她等着。"], ensure_ascii=False)

    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", fake_post)
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    assert t.translate(["He knocked twice.", "She waited."], "Book") == ["他敲了两下门。", "她等着。"]
    user = sent["messages"][-1]["content"]
    assert "He knocked twice." in user and "Book" in user


def test_openrouter_align_parses_per_paragraph(monkeypatch):
    payload = [[{"from": 0, "to": 1, "zh": "全部。"}]]
    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat",
                        lambda self, m, max_tokens: json.dumps(payload, ensure_ascii=False))
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    assert t.align([{"sents": ["A.", "B."], "zh": "全部。"}]) == payload


def test_openrouter_align_accepts_one_array_per_line(monkeypatch):
    # seen from deepseek-v4.1-flash: each paragraph's pieces on its own line, no outer array
    reply = ('[{"from":0,"to":0,"zh":"甲。"},{"from":1,"to":1,"zh":"乙。"}]\n'
             '[{"from":0,"to":1,"zh":"丙丁。"}]')
    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", lambda self, m, max_tokens: reply)
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    items = [{"sents": ["A.", "B."], "zh": "甲。乙。"}, {"sents": ["C.", "D."], "zh": "丙丁。"}]
    assert t.align(items) == [[{"from": 0, "to": 0, "zh": "甲。"}, {"from": 1, "to": 1, "zh": "乙。"}],
                              [{"from": 0, "to": 1, "zh": "丙丁。"}]]


def test_openrouter_align_single_paragraph_flat_array(monkeypatch):
    reply = '[{"from":0,"to":0,"zh":"甲。"},{"from":1,"to":1,"zh":"乙。"}]'
    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", lambda self, m, max_tokens: reply)
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    assert t.align([{"sents": ["A.", "B."], "zh": "甲。乙。"}]) == [
        [{"from": 0, "to": 0, "zh": "甲。"}, {"from": 1, "to": 1, "zh": "乙。"}]]


def test_parse_json_all_reads_consecutive_values():
    assert tr.parse_json_all('[1]\n[2, 3]\n```') == [[1], [2, 3]]


def test_openrouter_requires_key():
    with pytest.raises(tr.TranslateError):
        tr.OpenRouterTranslator({"key": None, "model": "m", "base": "b"})


def test_sentence_spans_are_memoized(monkeypatch):
    calls = []
    real = tr.split
    monkeypatch.setattr(tr, "split", lambda t: calls.append(t) or real(t))
    tr._spans_cached.cache_clear()
    text = "A fresh one. Another here."
    assert tr.sentence_spans(text) == tr.sentence_spans(text) == [(0, 12), (13, 26)]
    assert calls == [text]


def test_translate_chapter_passes_only_relevant_glossary_per_batch():
    t = ScriptedTranslator()
    entries = [{"source": "Nobody", "zh": "无名氏"}, {"source": "Zed", "zh": "泽德"},
               {"source": "The wind", "zh": "风"}]
    tr.translate_chapter(PARAS, t, workers=1, glossary=entries)
    seen = [g for g in t.glossaries]
    assert seen == [[{"source": "Nobody", "zh": "无名氏"}, {"source": "The wind", "zh": "风"}]]


def test_openrouter_translate_includes_glossary(monkeypatch):
    sent = {}

    def fake_post(self, messages, max_tokens):
        sent["messages"] = messages
        return json.dumps(["他敲了门。"], ensure_ascii=False)

    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", fake_post)
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    t.translate(["The Met was closed."], "Book", glossary=[{"source": "The Met", "zh": "大都会博物馆"}])
    user = sent["messages"][-1]["content"]
    assert "The Met => 大都会博物馆" in user
    t.translate(["Plain."], "Book")
    assert "=>" not in sent["messages"][-1]["content"]


def test_openrouter_glossary_call(monkeypatch):
    sent = {}

    def fake_post(self, messages, max_tokens):
        sent["messages"] = messages
        return "Sunny => Sunny"

    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", fake_post)
    t = tr.OpenRouterTranslator({"key": "k", "model": "m", "base": "https://x/v1"})
    assert t.glossary("Sunny ×9 | Sunny waved.") == "Sunny => Sunny"
    assert "Sunny ×9" in sent["messages"][-1]["content"]
    assert "English spelling" in sent["messages"][0]["content"]  # names stay in English


# ---------------------------------------------------------------- unparseable replies (review #1)
def _or(key="k"):
    return tr.OpenRouterTranslator({"key": key, "model": "m", "base": "https://x/v1"})


def test_unparseable_batch_reply_retries_then_splits_to_single_paragraphs(monkeypatch):
    replies = []

    def fake_chat(self, messages, max_tokens):
        paras = json.loads(messages[-1]["content"].split(":\n", 1)[1])
        replies.append(len(paras))
        if len(paras) > 1:
            return '["他敲了两下门。", "她说："你好"。"]'  # ASCII quotes break the JSON
        return json.dumps([f"译{paras[0]}"], ensure_ascii=False)

    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", fake_chat)
    out = tr._translate_checked(_or(), ["He knocked twice.", "She said hi."], "Book")
    assert out == ["译He knocked twice.", "译She said hi."]
    assert replies == [2, 2, 1, 1]


def test_unparseable_single_paragraph_reply_is_retried(monkeypatch):
    replies = iter(["Sorry, I cannot.", '["他敲了门。"]'])
    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", lambda self, m, max_tokens: next(replies))
    assert tr._translate_checked(_or(), ["He knocked."], "Book") == ["他敲了门。"]


def test_network_failure_still_propagates_without_split():
    class Down:
        model = "m"
        calls = 0

        def translate(self, paras, title, glossary=None):
            Down.calls += 1
            raise tr.TranslateError("OpenRouter request failed: down")

    with pytest.raises(tr.TranslateError):
        tr._translate_checked(Down(), ["A.", "B."], "Book")
    assert Down.calls == 1


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _choice(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def test_chat_retries_truncated_and_empty_content(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    seq = iter([_choice('["半', "length"), _choice(None), _choice('["好。"]')])
    monkeypatch.setattr(tr.urllib.request, "urlopen", lambda req, timeout=None: _Resp(next(seq)))
    assert _or()._chat([{"role": "user", "content": "x"}], 10) == '["好。"]'
    assert len(sleeps) == 2


def test_chat_gives_up_on_truncation_with_a_malformed_reply_error(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    monkeypatch.setattr(tr.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(_choice('["半', "length")))
    with pytest.raises(tr.MalformedReply) as ei:
        _or()._chat([{"role": "user", "content": "x"}], 10)
    assert isinstance(ei.value, tr.TranslateError) and isinstance(ei.value, ValueError)


def test_truncated_batch_is_split_into_single_paragraphs(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)

    def fake_urlopen(req, timeout=None):
        user = json.loads(req.data)["messages"][-1]["content"]
        paras = json.loads(user.split(":\n", 1)[1])
        if len(paras) > 1:
            return _Resp(_choice('["半', "length"))
        return _Resp(_choice(json.dumps([f"译{paras[0]}"], ensure_ascii=False)))

    monkeypatch.setattr(tr.urllib.request, "urlopen", fake_urlopen)
    assert tr._translate_checked(_or(), ["A.", "B."], "Book") == ["译A.", "译B."]


def test_translate_prompt_demands_chinese_quotation_marks():
    assert "“" in tr.TRANSLATE_SYSTEM and "”" in tr.TRANSLATE_SYSTEM
    assert "ASCII" in tr.TRANSLATE_SYSTEM


# ---------------------------------------------------------------- stubborn paragraphs (review #2)
class _Fixed:
    model = "m"

    def __init__(self, reply):
        self.reply = reply

    def translate(self, paras, title, glossary=None):
        return self.reply


def test_single_paragraph_joins_several_strings():
    assert tr._translate_checked(_Fixed(["他敲了门。", "没人应。"]), ["He knocked. Nobody."], "B") == \
        ["他敲了门。没人应。"]


def test_single_paragraph_accepts_bare_string():
    assert tr._translate_checked(_Fixed(" 他敲了门。 "), ["He knocked."], "B") == ["他敲了门。"]


def test_batch_still_rejects_wrong_count_before_splitting():
    class Counting:
        model = "m"
        calls = []

        def translate(self, paras, title, glossary=None):
            Counting.calls.append(len(paras))
            return ["甲。", "乙。", "丙。"] if len(paras) > 1 else [f"译{paras[0]}"]

    assert tr._translate_checked(Counting(), ["A.", "B."], "B") == ["译A.", "译B."]
    assert Counting.calls == [2, 2, 1, 1]


def test_openrouter_single_paragraph_bare_json_string(monkeypatch):
    monkeypatch.setattr(tr.OpenRouterTranslator, "_chat", lambda self, m, max_tokens: '"他敲了门。"')
    assert _or().translate(["He knocked."], "Book") == "他敲了门。"


def test_chapter_failure_cancels_queued_batches():
    class Broken:
        model = "m"

        def __init__(self):
            self.calls = 0

        def translate(self, paras, title, glossary=None):
            self.calls += 1
            raise tr.TranslateError("OpenRouter request failed: 402")

    paras = [f"Paragraph number {i} is here." for i in range(48)]  # 6 batches of 8
    t = Broken()
    with pytest.raises(tr.TranslateError):
        tr.translate_chapter(paras, t, workers=1)
    assert t.calls <= 2  # the failed batch, at most one already picked up; the rest cancelled


# ---------------------------------------------------------------- deterministic alignment (review #3)
@pytest.mark.parametrize("zh,pieces", [
    ("他敲了门。没人应。", ["他敲了门。", "没人应。"]),
    ("“你好！”她说。真的吗？」他问……然后走了",
     ["“你好！”", "她说。", "真的吗？」", "他问……", "然后走了"]),
    ("他来了。 她也来了。", ["他来了。 ", "她也来了。"]),
    ("没有句号", ["没有句号"]),
    ("什么？！”", ["什么？！”"]),
])
def test_punct_split(zh, pieces):
    assert tr.punct_split(zh) == pieces
    assert "".join(tr.punct_split(zh)) == zh


class PunctTranslator:
    """Chinese with one 。 per English sentence, length roughly proportional."""

    model = "m"

    def __init__(self, reply=None, align_ok=False):
        self.reply, self.align_ok = reply, align_ok
        self.align_calls = []

    def translate(self, paras, title, glossary=None):
        if self.reply is not None:
            return [self.reply(p) for p in paras]
        return ["".join("字" * max(1, len(s) // 3) + "。" for s in tr.sentence_texts(p)) for p in paras]

    def align(self, items):
        self.align_calls.append(items)
        if self.align_ok:
            return [[{"from": 0, "to": len(it["sents"]) - 1, "zh": it["zh"]}] for it in items]
        return [[{"from": 0, "to": 0, "zh": "胡说"}] for _ in items]


def test_punctuation_alignment_skips_the_model():
    t = PunctTranslator()
    out = tr.translate_chapter(["He knocked twice on the door. Nobody answered it."], t, workers=1)
    p = out["paragraphs"][0]
    assert [s["zh"] for s in p["segs"]] == ["字" * 9 + "。", "字" * 6 + "。"]
    assert [(s["from"], s["to"]) for s in p["segs"]] == [(0, 0), (1, 1)]
    assert t.align_calls == []


def test_disproportionate_split_goes_to_the_model():
    t = PunctTranslator(reply=lambda p: "嗯。" + "字" * 60 + "。", align_ok=True)
    out = tr.translate_chapter(["Hello there, my good friend, how are you. No."], t, workers=1)
    assert len(t.align_calls) == 1
    assert out["paragraphs"][0]["segs"] == [{"from": 0, "to": 1, "zh": "嗯。" + "字" * 60 + "。"}]


def test_failed_model_alignment_falls_back_to_equal_count_punctuation_split():
    t = PunctTranslator(reply=lambda p: "嗯。" + "字" * 60 + "。")
    out = tr.translate_chapter(["Hello there, my good friend, how are you. No."], t, workers=1)
    assert t.align_calls  # tried the model first
    assert out["paragraphs"][0]["segs"] == [{"from": 0, "to": 0, "zh": "嗯。"},
                                            {"from": 1, "to": 1, "zh": "字" * 60 + "。"}]


# ---------------------------------------------------------------- pieces come from the original (review #4)
class _AlignReply:
    model = "m"

    def __init__(self, pieces):
        self.pieces = pieces

    def align(self, items):
        return [self.pieces]


def test_aligned_pieces_are_sliced_from_the_original_translation():
    zh = "Eddie Triplett来了，\n他 笑了"
    t = _AlignReply([{"from": 0, "to": 0, "zh": "EddieTriplett来了，"},
                     {"from": 1, "to": 1, "zh": "他笑了"}])
    res = tr._align_checked(t, [{"sents": ["Eddie Triplett came.", "He laughed."], "zh": zh}])
    assert res == [[{"from": 0, "to": 0, "zh": "Eddie Triplett来了，\n"},
                    {"from": 1, "to": 1, "zh": "他 笑了"}]]
    assert "".join(s["zh"] for s in res[0]) == zh


# ---------------------------------------------------------------- letterless paragraphs (review #5)
def test_paragraphs_without_letters_are_copied_not_translated():
    t = ScriptedTranslator()
    paras = ["* * *", "She waited.", "—", "  1.  ", ""]
    out = tr.translate_chapter(paras, t, workers=1)
    sent = [p for call in t.translate_calls for p in call]
    assert sent == ["She waited."]
    ps = out["paragraphs"]
    assert ps[0]["zh"] == "* * *" and ps[0]["segs"] == [{"from": 0, "to": len(ps[0]["sents"]) - 1, "zh": "* * *"}]
    assert ps[2]["zh"] == "—" and ps[2]["segs"][0]["zh"] == "—"
    assert ps[3]["zh"] == "1." and len(ps[3]["segs"]) == 1
    assert ps[4] == {"sents": [], "zh": "", "segs": []}


def test_chapter_of_only_letterless_paragraphs_needs_no_model():
    t = ScriptedTranslator()
    seen = []
    out = tr.translate_chapter(["* * *"], t, on_progress=seen.append, workers=1)
    assert t.translate_calls == [] and out["paragraphs"][0]["zh"] == "* * *"
    assert seen == [1.0]


# ---------------------------------------------------------------- _chat retry details (review #7)
import email.message
import io
import urllib.error


def _http_error(code, body=b"", retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x/v1/chat/completions", code, "err", hdrs, io.BytesIO(body))


def _raising(errors):
    it = iter(errors)

    def fake(req, timeout=None):
        e = next(it)
        if isinstance(e, Exception):
            raise e
        return e
    return fake


def test_chat_does_not_sleep_after_the_last_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    monkeypatch.setattr(tr.urllib.request, "urlopen",
                        _raising([urllib.error.URLError("down")] * 3))
    with pytest.raises(tr.TranslateError):
        _or()._chat([{"role": "user", "content": "x"}], 10)
    assert len(sleeps) == 2


def test_chat_honours_numeric_retry_after_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    monkeypatch.setattr(tr.urllib.request, "urlopen", _raising([
        _http_error(429, retry_after="7"), _http_error(429, retry_after="999"), _Resp(_choice("ok"))]))
    assert _or()._chat([{"role": "user", "content": "x"}], 10) == "ok"
    assert sleeps == [7, 30]


def test_chat_non_numeric_retry_after_uses_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    monkeypatch.setattr(tr.urllib.request, "urlopen", _raising([
        _http_error(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), _Resp(_choice("ok"))]))
    assert _or()._chat([{"role": "user", "content": "x"}], 10) == "ok"
    assert sleeps == [2]


def test_chat_error_includes_body_snippet_and_never_the_key(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    key = "sk-or-v1-SECRET123"
    body = ('{"error": {"message": "No endpoints found for model m. key=' + key + '"}}' + " pad" * 200).encode()
    monkeypatch.setattr(tr.urllib.request, "urlopen", _raising([_http_error(404, body)]))
    with pytest.raises(tr.TranslateError) as ei:
        _or(key)._chat([{"role": "user", "content": "x"}], 10)
    msg = str(ei.value)
    assert "No endpoints found" in msg and "404" in msg
    assert key not in msg
    assert len(msg) < 400


def test_chat_network_error_text_is_redacted(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    key = "sk-or-v1-SECRET123"
    monkeypatch.setattr(tr.urllib.request, "urlopen",
                        _raising([urllib.error.URLError(f"proxy said Bearer {key}")] * 3))
    with pytest.raises(tr.TranslateError) as ei:
        _or(key)._chat([{"role": "user", "content": "x"}], 10)
    assert key not in str(ei.value)
