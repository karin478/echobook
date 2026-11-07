"""生词本 → Anki through AnkiConnect (Anki desktop + the AnkiConnect add-on, localhost:8765).

The reader picks an existing deck and note type once (saved in <home>/anki.json); the note's
first two fields get the front (English + sentence audio) and the back (Chinese + the 翻译/讲解
+ source). Never creates decks or note types, never syncs, never adds a duplicate.
"""

from __future__ import annotations

import base64
import html
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"
TIMEOUT = 10.0
TAG = "echobook"


class AnkiError(Exception):
    pass


def default_url() -> str:
    return os.environ.get("KOKORO_ANKI_URL") or DEFAULT_URL


class AnkiConnect:
    def __init__(self, url: str | None = None):
        self.url = url or default_url()

    def call(self, action: str, **params):
        body = json.dumps({"action": action, "version": 6, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                reply = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AnkiError(f"连不上 Anki：请打开 Anki 桌面版并装好 AnkiConnect 插件（{e}）") from e
        if reply.get("error"):
            raise AnkiError(str(reply["error"]))
        return reply.get("result")


# ---------------------------------------------------------------- config
def load_config(home: Path) -> dict | None:
    try:
        cfg = json.loads((Path(home) / "anki.json").read_text())
    except (OSError, ValueError):
        return None
    return cfg if all(cfg.get(k) for k in ("deck", "model", "front", "back")) else None


def save_config(home: Path, cfg: dict) -> None:
    (Path(home) / "anki.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- note
def _gloss_html(gloss: str) -> str:
    """A 翻译 / 讲解 as card HTML: ▸ blocks become "— <b>chunk</b>" lines."""
    out = []
    for line in (gloss or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("▸"):
            out.append(f"— <b>{html.escape(line[1:].strip())}</b>")
        else:
            out.append(html.escape(line))
    return "<br>".join(out)


def _sentence_with_bold(sentence: str, text: str) -> str:
    i = sentence.find(text) if text else -1
    if i < 0:
        return html.escape(sentence)
    return (html.escape(sentence[:i]) + f"<b>{html.escape(text)}</b>"
            + html.escape(sentence[i + len(text):]))


def media_name(rec: dict) -> str:
    return f"echobook-{rec['id']}.mp3"


def build_note(rec: dict, cfg: dict, has_audio: bool) -> dict:
    sound = f"[sound:{media_name(rec)}]" if has_audio else ""
    sentence = rec.get("sentence") or rec.get("text") or ""
    if rec.get("kind") == "sentence":
        front = html.escape(sentence)
    else:
        front = f"<b>{html.escape(rec['text'])}</b><br>{_sentence_with_bold(sentence, rec['text'])}"
    back = [html.escape(rec["zh"])] if rec.get("zh") else []
    if rec.get("gloss"):
        back.append(_gloss_html(rec["gloss"]))
    src = rec.get("book_title") or ""
    if rec.get("story_number") is not None:
        src += f" · Ch {rec['story_number']}"
    elif rec.get("chapter") is not None:
        src += f" · Ch {int(rec['chapter']) + 1}"
    back.append(f"<small>{html.escape(src)}</small>")
    return {
        "deckName": cfg["deck"],
        "modelName": cfg["model"],
        "fields": {cfg["front"]: front + sound, cfg["back"]: "<br><br>".join(back)},
        "tags": [TAG, time.strftime("%Y-%m-%d")],
        "options": {"allowDuplicate": False, "duplicateScope": "deck"},
    }


def push(client, rec: dict, cfg: dict, clip: Path | None) -> int:
    has_audio = clip is not None and clip.is_file()
    if has_audio:
        client.call("storeMediaFile", filename=media_name(rec),
                    data=base64.b64encode(clip.read_bytes()).decode())
    return client.call("addNote", note=build_note(rec, cfg, has_audio))
