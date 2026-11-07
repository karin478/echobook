"""Paragraph-level translation, then sentence-level alignment of the unchanged Chinese.

The Chinese is produced per paragraph (with neighbouring paragraphs in the same request for
context), then cut into pieces that map onto spaCy's English sentences: first by Chinese
sentence punctuation (no model call) when the counts and lengths line up, otherwise by the
model. A model cut is accepted only if the pieces concatenate back to the exact translation
(whitespace aside), and the shown pieces are then re-sliced from the original, so alignment can
never rewrite the translation; when it can't be done, the paragraph falls back to one piece.
Paragraphs without letters ("* * *") are copied through untranslated.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Protocol

from . import glossary as gl
from .sentences import split

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_BASE = "https://openrouter.ai/api/v1"
ENV_FILE_HINT = "~/.config/kokoro-reader/.env"  # where the key lives (see .env.example); never in the repo


class TranslateError(RuntimeError):
    pass


class MalformedReply(TranslateError, ValueError):
    """The model answered, but not with something usable (truncated, empty, not JSON).
    A ValueError so the batch logic retries / splits it like any other wrong-shaped reply."""


class Translator(Protocol):
    model: str

    def translate(self, paras: list[str], title: str, glossary: list[dict] | None = None) -> list[str]: ...

    def gloss(self, mode: str, text: str, sentence: str, context: str, title: str) -> str: ...

    def align(self, items: list[dict]) -> list[list[dict]]: ...


# ---------------------------------------------------------------- sentences
def sentence_spans(text: str) -> list[tuple[int, int]]:
    """spaCy sentence spans of `text` with surrounding whitespace trimmed; blank ones dropped."""
    return list(_spans_cached(text))


@functools.lru_cache(maxsize=50000)
def _spans_cached(text: str) -> tuple[tuple[int, int], ...]:
    # spaCy is the slow part of /sentences (seconds per chapter); paragraphs never change
    out = []
    for a, b in split(text):
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if b - trail > a + lead:
            out.append((a + lead, b - trail))
    return tuple(out)


def sentence_texts(text: str) -> list[str]:
    return [text[a:b] for a, b in sentence_spans(text)]


def norm(s: str) -> str:
    return " ".join(s.split())


# ---------------------------------------------------------------- pure helpers
def chunk(paras: list[str], max_paras: int = 8, max_chars: int = 1800) -> list[list[int]]:
    """Indices of non-blank paragraphs grouped into consecutive translation requests."""
    groups: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i, p in enumerate(paras):
        if not p.strip():
            continue
        if cur and (len(cur) >= max_paras or size + len(p) > max_chars):
            groups.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(p)
    if cur:
        groups.append(cur)
    return groups


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def valid_segments(n: int, zh: str, segs) -> bool:
    if not isinstance(segs, list) or not segs:
        return False
    nxt = 0
    for s in segs:
        if not isinstance(s, dict):
            return False
        a, b, z = s.get("from"), s.get("to"), s.get("zh")
        if type(a) is not int or type(b) is not int or not isinstance(z, str):
            return False
        if a != nxt or b < a or b >= n or not z.strip():
            return False
        nxt = b + 1
    if nxt != n:
        return False
    return _squash("".join(s["zh"] for s in segs)) == _squash(zh)


def rebase_segments(zh: str, segs: list[dict]) -> list[dict]:
    """Validated segments with each piece's text re-cut from the ORIGINAL `zh` (validation
    ignores whitespace, so the model's own copy may differ in spacing — never show that copy).
    Whitespace at a cut stays with the piece before it."""
    pos = [m.start() for m in re.finditer(r"\S", zh)]  # original index of each squashed char
    out, cum, start = [], 0, 0
    for k, s in enumerate(segs):
        cum += len(_squash(s["zh"]))
        end = pos[cum] if k < len(segs) - 1 else len(zh)
        out.append({"from": s["from"], "to": s["to"], "zh": zh[start:end]})
        start = end
    return out


def fallback_segments(n: int, zh: str) -> list[dict]:
    return [{"from": 0, "to": n - 1, "zh": zh}]


# sentence-final punctuation, then any closing quotes/brackets glued to it (。” ？」 ……)
_SENT_END = re.compile(r"[。！？…]+[”’」』）》】)]*")


def punct_split(zh: str) -> list[str]:
    """`zh` cut after each sentence-final punctuation run; the pieces concatenate back to `zh`
    exactly (whitespace after a cut stays with the piece before it)."""
    pieces, start = [], 0
    for m in _SENT_END.finditer(zh):
        end = m.end()
        while end < len(zh) and zh[end].isspace():
            end += 1
        if end > start:
            pieces.append(zh[start:end])
            start = end
    if start < len(zh):
        if pieces and not zh[start:].strip():
            pieces[-1] += zh[start:]
        else:
            pieces.append(zh[start:])
    return pieces


def punct_segments(sents: list[str], zh: str, strict: bool = True) -> list[dict] | None:
    """One piece per English sentence by cutting at Chinese sentence punctuation, when the
    count matches (and, if `strict`, each piece's share of the Chinese is within 3x of its
    sentence's share of the English); else None."""
    pieces = punct_split(zh)
    if len(pieces) != len(sents):
        return None
    if strict:
        zt = sum(len(_squash(x)) for x in pieces)
        et = sum(len(_squash(x)) for x in sents)
        for zp, en in zip(pieces, sents):
            ratio = (len(_squash(zp)) / zt) / (len(_squash(en)) / et)
            if not 1 / 3 <= ratio <= 3:
                return None
    return [{"from": i, "to": i, "zh": zp} for i, zp in enumerate(pieces)]


def _unfence(text: str) -> str:
    return re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())


def parse_json(text: str):
    """First JSON array/object in a model reply, tolerating ``` fences and surrounding prose."""
    text = _unfence(text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON found in model reply")


def parse_json_all(text: str) -> list:
    """Every top-level JSON array/object in a reply, in order (models sometimes emit one per line)."""
    text = _unfence(text)
    dec = json.JSONDecoder()
    out, pos = [], 0
    while True:
        m = re.compile(r"[\[{]").search(text, pos)
        if not m:
            return out
        try:
            val, end = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            pos = m.start() + 1
            continue
        out.append(val)
        pos = end


def _is_pieces(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, dict) for x in v)


def _align_shape(vals: list, n_items: int):
    """Normalise the model's alignment reply to one list of pieces per paragraph."""
    if len(vals) == 1 and isinstance(vals[0], list) and vals[0] and all(_is_pieces(x) for x in vals[0]):
        return vals[0]                       # [[...], [...]] as asked
    if len(vals) == n_items and all(_is_pieces(v) for v in vals):
        return vals                          # one array per paragraph, no wrapper
    if n_items == 1 and len(vals) == 1 and _is_pieces(vals[0]):
        return [vals[0]]                     # single paragraph sent back flat
    raise ValueError("alignment reply has an unexpected shape")


# ---------------------------------------------------------------- chapter pipeline
def _translate_checked(translator: Translator, texts: list[str], title: str,
                       glossary: list[dict] | None = None) -> list[str]:
    for _ in range(2):
        try:
            out = translator.translate(texts, title, glossary=gl.relevant(glossary or [], texts) or None)
        except ValueError:  # unparseable / truncated reply: same as a wrong shape
            continue
        if len(texts) == 1:  # one paragraph: a bare string or a split-up answer is still its text
            if isinstance(out, str):
                out = [out]
            elif isinstance(out, list) and len(out) > 1 and all(isinstance(z, str) for z in out):
                out = ["".join(z.strip() for z in out)]
        if (isinstance(out, list) and len(out) == len(texts)
                and all(isinstance(z, str) and z.strip() for z in out)):
            return [z.strip() for z in out]
    if len(texts) == 1:
        raise TranslateError("translation came back malformed")
    return [_translate_checked(translator, [t], title, glossary)[0] for t in texts]


def _align_checked(translator: Translator, items: list[dict]) -> list[list[dict] | None]:
    """Valid segments per item, or None where the model couldn't produce a valid cut."""
    try:
        res = translator.align(items)
    except Exception:
        res = None
    if not isinstance(res, list) or len(res) != len(items):
        res = [None] * len(items)
    return [rebase_segments(it["zh"], r) if valid_segments(len(it["sents"]), it["zh"], r) else None
            for r, it in zip(res, items)]


def _clean(segs: list[dict]) -> list[dict]:
    return [{"from": s["from"], "to": s["to"], "zh": s["zh"].strip()} for s in segs]


def translate_chapter(paragraphs: list[str], translator: Translator, title: str = "",
                      on_progress: Callable[[float], None] | None = None,
                      workers: int = 6, glossary: list[dict] | None = None) -> dict:
    sents = [sentence_texts(p) for p in paragraphs]
    zh = [""] * len(paragraphs)
    segs: list[list[dict]] = [[] for _ in paragraphs]
    for i, p in enumerate(paragraphs):
        if p.strip() and not any(c.isalpha() for c in p):  # "* * *", "—", "1.": nothing to translate
            zh[i] = p.strip()
            segs[i] = fallback_segments(max(1, len(sents[i])), zh[i])
    groups = chunk([p if not zh[i] else "" for i, p in enumerate(paragraphs)])
    lock = threading.Lock()
    done = [0]

    def run(idxs: list[int]) -> None:
        out = _translate_checked(translator, [paragraphs[i].strip() for i in idxs], title, glossary)
        for i, z in zip(idxs, out):
            zh[i] = z
            if len(sents[i]) <= 1:
                segs[i] = fallback_segments(max(1, len(sents[i])), z)
        multi = []
        for i in idxs:
            if len(sents[i]) > 1:
                det = punct_segments(sents[i], zh[i])
                if det:
                    segs[i] = _clean(det)  # punctuation lines up: no model call needed
                else:
                    multi.append(i)
        if multi:
            first = _align_checked(translator, [{"sents": sents[i], "zh": zh[i]} for i in multi])
            for i, r in zip(multi, first):
                for _ in range(2):  # model misses are sporadic: retry alone, then whole-paragraph fallback
                    if r is not None:
                        break
                    r = _align_checked(translator, [{"sents": sents[i], "zh": zh[i]}])[0]
                r = r or punct_segments(sents[i], zh[i], strict=False)
                segs[i] = _clean(r) if r else fallback_segments(len(sents[i]), zh[i])
        with lock:
            done[0] += 1
            if on_progress:
                on_progress(done[0] / len(groups))

    if groups:
        pool = ThreadPoolExecutor(max_workers=max(1, workers))
        futures = [pool.submit(run, g) for g in groups]
        wait(futures, return_when=FIRST_EXCEPTION)
        failed = next((f for f in futures if f.done() and not f.cancelled() and f.exception()), None)
        # first failure: drop the queued batches (no point paying for a chapter that won't save)
        pool.shutdown(wait=failed is None, cancel_futures=failed is not None)
        if failed is not None:
            raise failed.exception()
    elif on_progress:
        on_progress(1.0)
    return {
        "model": getattr(translator, "model", ""),
        "paragraphs": [{"sents": s, "zh": z, "segs": g} for s, z, g in zip(sents, zh, segs)],
    }


# ---------------------------------------------------------------- translators
class FakeTranslator:
    """Deterministic stand-in for tests / E2E: 【译】 + each sentence, trivially alignable."""

    model = "fake"

    def glossary(self, rows: str) -> str:
        return ""  # no glossary in tests / E2E unless a test supplies one

    def translate(self, paras: list[str], title: str, glossary: list[dict] | None = None) -> list[str]:
        return ["".join(f"【译】{s}" for s in sentence_texts(p)) or f"【译】{p}" for p in paras]

    def align(self, items: list[dict]) -> list[list[dict]]:
        return [[{"from": i, "to": i, "zh": f"【译】{s}"} for i, s in enumerate(it["sents"])]
                for it in items]

    def gloss(self, mode: str, text: str, sentence: str, context: str, title: str) -> str:
        return f"【翻译】{text}" if mode == "quick" else f"【讲解】{text}｜{sentence}"


def _read_env_file(path: Path) -> dict:
    env = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_config() -> dict:
    """Key/model/base: process env first, then the .env file (KOKORO_TRANSLATE_ENV, default
    ~/.config/kokoro-reader/.env)."""
    path = Path(os.environ.get("KOKORO_TRANSLATE_ENV") or ENV_FILE_HINT).expanduser()
    f = _read_env_file(path)
    return {
        "key": os.environ.get("OPENROUTER_API_KEY") or f.get("OPENROUTER_API_KEY") or None,
        "model": (os.environ.get("KOKORO_TRANSLATE_MODEL") or f.get("KOKORO_TRANSLATE_MODEL")
                  or f.get("DEFAULT_MODEL") or DEFAULT_MODEL),
        "base": os.environ.get("OPENROUTER_API_BASE") or f.get("OPENROUTER_API_BASE") or DEFAULT_BASE,
    }


TRANSLATE_SYSTEM = (
    "You are a literary translator. Translate English book paragraphs into natural, fluent "
    "Simplified Chinese that is faithful to the meaning, tone and register of the original. "
    "Translate each paragraph as a whole, using the surrounding paragraphs as context; you may "
    "restructure sentences inside a paragraph so the Chinese reads naturally. Keep names of "
    "people and places in their original English spelling. Use Chinese quotation marks “ ” for "
    "dialogue and quotes; never put an ASCII double quote (\") inside a string. "
    "Input: a JSON array of N English paragraphs. Output ONLY a JSON array of exactly N "
    "Chinese strings, one per paragraph, in the same order. No commentary."
)

GLOSSARY_SYSTEM = (
    "You build a translation glossary so a book's recurring proper nouns and special terms "
    "are rendered CONSISTENTLY in Simplified Chinese. The Chinese is read side by side with "
    "the English by a learner, so names of people and places stay in their English spelling. "
    "You get capitalised terms found across the whole book, each with its count and one line "
    "of context. Keep ONLY real proper nouns and recurring special terms; skip ordinary words "
    "that are merely capitalised, and SKIP words whose Chinese depends on context: nationalities "
    "and languages (French, English), days, months, holidays, common nouns, fragments of "
    "song titles or quotations. For each kept term output one line `source => rendering`:\n"
    "- kinship words used as names (Mom, Dad, Grandma): the natural Chinese (妈妈, 爸爸, 奶奶);\n"
    "- people (including nicknames used as names), places, streets, buildings: repeat the source "
    "exactly (keep English), e.g. `Sunny => Sunny`;\n"
    "- organisations, institutions, titles and ranks, works (books/films/shows), brands, events, "
    "foreign words and invented terms: the established Simplified Chinese rendering (works in 《》), "
    "e.g. `Metropolitan Museum of Art => 大都会艺术博物馆`; repeat the source only for brands best "
    "left in Latin script.\n"
    "Use the source spelling exactly as given. Check each rendering against its context. "
    "No JSON, no numbering, no commentary."
)

# 划词: a fragment the reader selected while reading. 翻译 = a short rendering as used here,
# 讲解 = a teacher's breakdown: the meaning here, then one ▸ block per smallest unit (phrase,
# collocation, pattern) with an 例：and an optional 注意： — reader.js lays the reply out by these markers.
GLOSS_SYSTEM = {
    "quick": (
        "A Chinese learner of English selected a fragment while reading an English book. Give "
        "only its concise Simplified Chinese rendering as it is used in this sentence (for a "
        "single word, its meaning here). No explanation, no quotes, no pinyin, no full-sentence "
        "translation."
    ),
    "explain": (
        "你是一位资深英语老师，学生是读英文原版书的中文母语者：单词大多认识，难的是短语、搭配、短语动词、习语和句子结构在具体语境"
        "里的意思。学生在书里划选了一个片段，你要像一对一精讲那样把它讲透。\n"
        "\n"
        "先把划选的片段拆成最小的意义单位，一般 1–5 个词：固定搭配、短语动词、习语、有讲究的介词短语、值得讲的句型（不连续的"
        "也算，例如 never ... anything quite like it）。每个英文块最多 5 个词（不连续的句型用"
        " ... 连接），超过就继续拆；不要把半句话当成一个单位。普通单词不单独拆，除非它在这里不是最常见的意思，或者中文母语者"
        "容易理解错。只划了一个单词或一个不可再拆的短语时，就只讲它这一个。\n"
        "\n"
        "输出（纯文本，不用 Markdown，不用 ** 或 #）：\n"
        "第一行永远是这段在本句里的意思，用自然的中文直接说一句，不加「意思是」「这句话是说」之类的前缀。\n"
        "然后每个单位一段：\n"
        "▸ 英文块（有变形时括号写原形，如 I'd encountered → encounter）\n"
        "  这里的意思；怎么用、语气或色彩、和字面意思或常见意思的区别、为什么在这个上下文里是这个意思。每段不超过 80 个汉字"
        "。\n"
        "  例：一个简短的新英文例句 —— 中文\n"
        "如果有值得注意的语法或修辞（时态、倒装、省略、比喻），最后加一行「注意：…」，不超过 60 个汉字。\n"
        "\n"
        "要求：每一条都要给学生新的、具体、准确的信息，不写「这个表达很生动」这类空话，拿不准的不说；不要翻译整段上下文；英文块必"
        "须是原文里的原样文字。\n"
        "\n"
        "示例（只示范拆分粒度和格式，内容按实际片段写）：划选 “She had long since given up on h"
        "im turning up on time.”\n"
        "她早就不指望他能准时出现了。\n"
        "▸ long since\n"
        "  早就、很久以前就；放在完成时里，比 long ago 更书面。\n"
        "  例：The shop has long since closed. —— 那家店早就关了。\n"
        "▸ given up on (give up on sb)\n"
        "  对某人不再抱希望；和 give up（放弃做某事）不同，on 后面接人或事。\n"
        "  例：Don't give up on me yet. —— 先别对我失望。\n"
        "▸ turning up (turn up)\n"
        "  出现、露面，口语里常指人到场，不是“调高”。\n"
        "  例：He turned up an hour late. —— 他晚了一小时才露面。\n"
        "▸ on time\n"
        "  准时；和 in time（及时、来得及）意思不同。\n"
        "  例：The train left on time. —— 火车准时开了。\n"
        "注意：him turning up 是动名词带逻辑主语，口语常用 him，正式文体用 his。"
    ),
}
GLOSS_MAX_TOKENS = {"quick": 150, "explain": 1500}

ALIGN_SYSTEM = (
    "You cut an existing Chinese translation into pieces that line up with the English "
    "sentences it translates. You must NOT change, add, drop or reorder any Chinese "
    "character: only choose where to cut. Each piece covers a contiguous range of English "
    "sentences given as 0-based inclusive indices \"from\" and \"to\". Prefer one piece per "
    "sentence; when the Chinese merges or reorders sentences so they cannot be separated "
    "cleanly, use one piece covering all of them. Pieces must cover every sentence, in order. "
    "Input: a JSON array of paragraphs, each {\"sents\": [English sentences], \"zh\": Chinese}. "
    "Output ONLY a JSON array with, for each input paragraph, an array of pieces "
    "[{\"from\": 0, \"to\": 0, \"zh\": \"...\"}, ...]. No commentary."
)


RETRY_AFTER_CAP = 30.0


def _retry_after(e: urllib.error.HTTPError, default: float) -> float:
    """A numeric Retry-After (seconds, capped); anything else keeps the normal backoff."""
    try:
        return min(RETRY_AFTER_CAP, max(0.0, float((e.headers or {}).get("Retry-After"))))
    except (TypeError, ValueError):
        return default


class OpenRouterTranslator:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_config()
        if not cfg.get("key"):
            raise TranslateError(f"没有找到 OPENROUTER_API_KEY：写进 {ENV_FILE_HINT}（参照 .env.example）或设成环境变量")
        self.key, self.model, self.base = cfg["key"], cfg["model"], cfg["base"].rstrip("/")

    def _chat(self, messages: list[dict], max_tokens: int) -> str:
        body = json.dumps({
            "model": self.model,
            "reasoning": {"enabled": False},  # V4 Flash: no thinking chains for batch work
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }).encode()
        attempts, last, malformed = 3, "", False
        for attempt in range(attempts):
            pause = 2 * (attempt + 1)
            req = urllib.request.Request(
                f"{self.base}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    d = json.loads(r.read())
                choice = d["choices"][0]
                content = choice["message"]["content"]
                if choice.get("finish_reason") == "length":
                    raise MalformedReply("reply was cut off at max_tokens")
                if not isinstance(content, str) or not content.strip():
                    raise MalformedReply("reply had no content")
                return content
            except MalformedReply as e:
                last, malformed = str(e), True
            except urllib.error.HTTPError as e:
                last, malformed = self._http_detail(e), False
                if e.code not in (408, 429) and e.code < 500:
                    break
                if e.code == 429:
                    pause = _retry_after(e, pause)
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as e:
                last, malformed = str(e) or type(e).__name__, False
            if attempt < attempts - 1:
                time.sleep(pause)
        if malformed:
            raise MalformedReply(f"OpenRouter reply unusable: {self._redact(last)}")
        raise TranslateError(f"OpenRouter request failed: {self._redact(last)}")

    def _redact(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

    def _http_detail(self, e: urllib.error.HTTPError) -> str:
        """`HTTP 404: <start of the response body>` (key redacted before cutting)."""
        try:
            raw = e.read() or b""
        except Exception:
            raw = b""
        snippet = self._redact(" ".join(raw.decode("utf-8", "replace").split()))[:200]
        return f"HTTP {e.code}: {snippet}" if snippet else f"HTTP {e.code} {e.reason}"

    @staticmethod
    def _budget(chars: int) -> int:
        return min(8000, 600 + chars * 2)

    def glossary(self, rows: str) -> str:
        return self._chat([{"role": "system", "content": GLOSSARY_SYSTEM},
                           {"role": "user", "content": "Terms (term ×count | context):\n" + rows}],
                          min(16000, 800 + len(rows)))

    def translate(self, paras: list[str], title: str, glossary: list[dict] | None = None) -> list[str]:
        block = gl.prompt_block(glossary or [])
        user = (f"Book: {title}\n\n" + (block + "\n\n" if block else "")
                + f"Translate these {len(paras)} paragraphs:\n" + json.dumps(paras, ensure_ascii=False))
        reply = self._chat([{"role": "system", "content": TRANSLATE_SYSTEM},
                            {"role": "user", "content": user}],
                           self._budget(sum(len(p) for p in paras)))
        try:
            return parse_json(reply)
        except ValueError:
            if len(paras) == 1:  # a lone paragraph sometimes comes back as a bare JSON string
                try:
                    bare = json.loads(_unfence(reply))
                except ValueError:
                    bare = None
                if isinstance(bare, str):
                    return bare
            raise

    def gloss(self, mode: str, text: str, sentence: str, context: str, title: str) -> str:
        if mode not in GLOSS_SYSTEM:
            raise ValueError(f"unknown gloss mode: {mode}")
        user = (f"Book: {title}\nContext: {context}\nSentence: {sentence}\nSelected: {text}")
        return self._chat([{"role": "system", "content": GLOSS_SYSTEM[mode]},
                           {"role": "user", "content": user}], GLOSS_MAX_TOKENS[mode]).strip()

    def align(self, items: list[dict]) -> list[list[dict]]:
        user = json.dumps(items, ensure_ascii=False)
        reply = self._chat([{"role": "system", "content": ALIGN_SYSTEM},
                            {"role": "user", "content": user}],
                           self._budget(sum(len(i["zh"]) for i in items) * 2))
        return _align_shape(parse_json_all(reply), len(items))


def make_translator() -> Translator:
    if os.environ.get("KOKORO_READER_FAKE_TRANSLATE") == "1":
        return FakeTranslator()
    return OpenRouterTranslator()
