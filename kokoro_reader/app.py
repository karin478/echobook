"""FastAPI app wiring Store, Library, ConversionQueue, TTS and vocab together."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Callable, Literal
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from .anchors import display_paragraphs
from .audio import write_mp3
from .bilingual import TranslationJobs, sentence_view
from .epub_extract import EpubError
from .library import Library
from .queue import ConversionQueue
from .store import Store
from . import anki as ankimod
from .quick_mt import MTError, make_mt
from .translate import TranslateError, Translator, make_translator
from .tts import VOICES, Synthesizer, synth_preview
from .vocab import save_vocab, selection_context

STATIC_DIR = Path(__file__).parent / "static"
_ID = re.compile(r"^[0-9a-f]{32}$")
_VOICE_IDS = {v["id"] for v in VOICES}
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
GLOSS_CACHE = 500  # 划词 results kept in memory
log = logging.getLogger("kokoro_reader")
_COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}


class OrderBody(BaseModel):
    ids: list[str]


class VoiceBody(BaseModel):
    voice: str


class ConvertBody(BaseModel):
    mode: str
    chapter: int | None = None


class MatterBody(BaseModel):
    is_matter: bool


class ProgressBody(BaseModel):
    chapter: int
    para: int
    word: int
    time: float
    ratio: float


class AnkiConfigBody(BaseModel):
    deck: str
    model: str


class VocabBody(BaseModel):
    book_id: str
    chapter: int
    para: int
    start_char: int
    end_char: int
    whole: bool = False
    zh: str | None = None
    gloss: str | None = None
    gloss_mode: Literal["quick", "explain"] | None = None


class LookupBody(BaseModel):
    book_id: str
    chapter: int
    para: int
    start_char: int
    end_char: int
    mode: Literal["quick", "explain"]


def _valid_id(s: str) -> bool:
    return bool(_ID.match(s))


def create_app(home: Path, synth: Synthesizer, start_worker: bool = True,
               preview_synth: Synthesizer | None = None,
               translator_factory: Callable[[], Translator] | None = None,
               translate_workers: int = 4,
               mt_factory: Callable[[], object | None] | None = None,
               anki_factory: Callable[[], object] | None = None) -> FastAPI:
    home = Path(home)
    store = Store(home / "kokoro.db")
    library = Library(home, store)
    queue = ConversionQueue(store, library, synth, autostart=False)
    translations = TranslationJobs(library, translator_factory or make_translator,
                                   max_chapters=translate_workers)
    # previews get their own synth so they never share inference state with the worker
    preview_synth = preview_synth if preview_synth is not None else type(synth)()
    preview_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            queue.start()
        try:
            yield
        finally:
            queue.stop()
            translations.shutdown()

    app = FastAPI(title="Echobook", lifespan=lifespan)
    # Reject foreign Host headers (DNS rebinding: a web page on evil.example resolving to
    # 127.0.0.1 must not be able to drive the API). Other hostnames get 400.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def same_origin_writes(request, call_next):
        # A page on another site can still send "simple" POSTs (form uploads) to 127.0.0.1:
        # writes must come from our own pages (or from a non-browser client with no Origin).
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            cross = request.headers.get("sec-fetch-site") == "cross-site"
            same = f"http://{request.headers.get('host', '')}"  # Host is already 127.0.0.1/localhost
            if cross or (origin is not None and origin != same):
                return JSONResponse({"detail": "只接受本应用页面发出的请求"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # pages/scripts change with every update: always revalidate (ETag) instead of the
        # browser's heuristic caching serving a stale book.js against a new book.html
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response
    app.state.store, app.state.library, app.state.queue = store, library, queue
    app.state.translations = translations
    app.state.preview_synth = preview_synth

    def book_or_404(book_id: str) -> dict:
        book = store.get_book(book_id) if _valid_id(book_id) else None
        if book is None:
            raise HTTPException(404, "书不存在")
        return book

    def book_info(book: dict) -> dict:
        chapters = [c for c in store.list_chapters(book["id"]) if not c["is_matter"]]
        return {
            "id": book["id"], "title": book["title"], "author": book["author"],
            "has_cover": library.cover_path(book["id"]) is not None,
            "voice": book["voice"],
            "total": len(chapters),
            "converted": sum(1 for c in chapters if c["status"] == "done"),
            "translated": sum(1 for c in chapters if library.translation_path(book["id"], c["idx"]).exists()),
            "progress": store.get_progress(book["id"]),
        }

    # ---- root ----
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/shelf.html", status_code=302)

    # ---- books ----
    @app.get("/api/books")
    def list_books():
        return [book_info(b) for b in store.list_books()]

    @app.post("/api/books")
    def upload_book(file: UploadFile = File(...)):
        if not (file.filename or "").lower().endswith(".epub"):
            raise HTTPException(400, "只支持 EPUB 文件")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "upload.epub"
            size = 0
            with open(src, "wb") as out:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"文件太大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）")
                    out.write(chunk)
            try:
                book_id, created = library.import_epub(src)
            except EpubError as e:
                raise HTTPException(400, str(e) or "无法解析该 EPUB 文件")
        info = book_info(store.get_book(book_id))
        return JSONResponse({"book": info, "created": created}, status_code=201 if created else 200)

    @app.put("/api/books/order")
    def reorder(body: OrderBody):
        try:
            store.reorder_books(body.ids)
        except ValueError:
            raise HTTPException(400, "排序列表不合法")
        return {"ids": body.ids}

    @app.delete("/api/books/{book_id}", status_code=204)
    def delete_book(book_id: str):
        book_or_404(book_id)
        library.delete_book(book_id)
        return Response(status_code=204)

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str):
        book = book_or_404(book_id)
        chapters = []
        for c in store.list_chapters(book_id):
            zh = translations.status(book_id, c["idx"])
            chapters.append({**c, "zh": zh["status"], "zh_progress": zh["progress"]})
        g = library.glossary(book_id)
        return {**book_info(book), "chapters": chapters, "glossary": None if g is None else len(g)}

    def translate_chapters(book: dict, idxs: list[int], priority: str) -> int:
        """Start (or, for "high", move up) the Chinese for these chapters, first one first;
        how many were new."""
        order = list(dict.fromkeys(idxs))
        if priority == "high":
            order.reverse()  # each high start goes to the front: the last started runs first
        started = 0
        for idx in order:
            was = translations.status(book["id"], idx)["status"]
            if was == "done" or (was in ("queued", "running") and priority != "high"):
                continue
            translations.start(book["id"], idx, shown_paragraphs(book["id"], idx), book["title"],
                               book_text(book["id"]), priority=priority)
            started += was in ("none", "error")
        return started

    @app.post("/api/books/{book_id}/prepare", status_code=202)
    def prepare(book_id: str):
        """整本书一键准备: queue audio for every story chapter and translate every one."""
        book = book_or_404(book_id)
        story = [c for c in store.list_chapters(book_id) if not c["is_matter"]]
        audio = sum(1 for c in story if c["status"] in ("none", "error"))
        if audio:
            queue.enqueue_batch(book_id, "all")
        zh = translate_chapters(book, [c["idx"] for c in story], "normal")
        return {"audio": audio, "zh": zh}

    @app.get("/api/books/{book_id}/cover")
    def cover(book_id: str):
        book_or_404(book_id)
        p = library.cover_path(book_id)
        media = _COVER_TYPES.get(p.suffix.lower()) if p is not None else None
        if media is None:  # only real raster images are ever served (never SVG/HTML)
            raise HTTPException(404, "没有封面")
        return FileResponse(p, media_type=media, headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'"})

    @app.put("/api/books/{book_id}/voice")
    def set_voice(book_id: str, body: VoiceBody):
        book_or_404(book_id)
        if body.voice not in _VOICE_IDS:
            raise HTTPException(400, "不支持的音色")
        store.set_book_voice(book_id, body.voice)
        return {"voice": body.voice}

    @app.post("/api/books/{book_id}/convert")
    def convert(book_id: str, body: ConvertBody):
        """准备 chapters = their audio AND their Chinese, one trigger (a new voice redoes audio only)."""
        book = book_or_404(book_id)
        mode = body.mode
        chapters = store.list_chapters(book_id)
        if mode in ("one", "open", "reconvert"):
            if body.chapter is None:
                raise HTTPException(400, "缺少章节编号")
            if body.chapter not in {c["idx"] for c in chapters}:
                raise HTTPException(404, "章节不存在")
            if mode == "one":
                queue.enqueue(book_id, [body.chapter], "high")
                translate_chapters(book, [body.chapter], "high")
            elif mode == "open":
                translate_chapters(book, queue.enqueue_open(book_id, body.chapter), "high")
            else:
                queue.reconvert(book_id, body.chapter)
        else:
            raise HTTPException(400, "未知的转换模式")
        return queue.snapshot()

    # ---- chapters ----
    @app.put("/api/books/{book_id}/chapters/{idx}/matter")
    def set_matter(book_id: str, idx: int, body: MatterBody):
        book_or_404(book_id)
        if idx not in {c["idx"] for c in store.list_chapters(book_id)}:
            raise HTTPException(404, "章节不存在")
        store.set_chapter_matter(book_id, idx, body.is_matter)
        return {"is_matter": body.is_matter}

    @app.get("/api/books/{book_id}/chapters/{idx}")
    def get_chapter(book_id: str, idx: int):
        book_or_404(book_id)
        chapters = store.list_chapters(book_id)
        ch = next((c for c in chapters if c["idx"] == idx), None)
        if ch is None:
            raise HTTPException(404, "章节不存在")
        try:
            paragraphs = library.paragraphs(book_id, idx)
            tokens = library.tokens(book_id, idx)
        except (IndexError, FileNotFoundError):
            raise HTTPException(404, "章节不存在")
        has_mp3 = library.chapter_audio_path(book_id, idx).exists()
        if tokens is not None and has_mp3:
            # versioned: a re-recorded chapter is a new URL, never a stale cached mp3
            v = library.chapter_audio_path(book_id, idx).stat().st_mtime_ns
            audio_url = f"/api/books/{book_id}/chapters/{idx}/audio.mp3?v={v}"
        else:
            audio_url = None
            # a converted empty chapter has tokens [] and no audio; anything else is unplayable
            if not (tokens == [] and ch["status"] == "done"):
                tokens = None
        body_idxs = [c["idx"] for c in chapters if not c["is_matter"]]
        return {
            "title": ch["title"],
            "paragraphs": display_paragraphs(paragraphs, tokens),
            "tokens": tokens,
            "audio_url": audio_url,
            "duration": ch["duration"],
            "status": ch["status"],
            "prev_idx": max((i for i in body_idxs if i < idx), default=None),
            "next_idx": min((i for i in body_idxs if i > idx), default=None),
        }

    def book_text(book_id: str) -> Callable[[], list[str]]:
        """Lazily: every story chapter's paragraphs (the glossary is counted over the whole book)."""
        def read() -> list[str]:
            out: list[str] = []
            for c in store.list_chapters(book_id):
                if not c["is_matter"]:
                    try:
                        out.extend(library.paragraphs(book_id, c["idx"]))
                    except (IndexError, FileNotFoundError):
                        pass
            return out
        return read

    def shown_paragraphs(book_id: str, idx: int) -> list[str]:
        if idx not in {c["idx"] for c in store.list_chapters(book_id)}:
            raise HTTPException(404, "章节不存在")
        try:
            return display_paragraphs(library.paragraphs(book_id, idx), library.tokens(book_id, idx))
        except (IndexError, FileNotFoundError):
            raise HTTPException(404, "章节不存在")

    @app.post("/api/books/{book_id}/chapters/{idx}/translate", status_code=202)
    def translate_chapter(book_id: str, idx: int):
        book = book_or_404(book_id)
        shown = shown_paragraphs(book_id, idx)
        return translations.start(book_id, idx, shown, book["title"], book_text(book_id), priority="high")

    @app.get("/api/books/{book_id}/chapters/{idx}/sentences")
    def chapter_sentences(book_id: str, idx: int):
        book_or_404(book_id)
        shown = shown_paragraphs(book_id, idx)
        st = translations.status(book_id, idx)
        stored = library.translation(book_id, idx) if st["status"] == "done" else None
        return {**st, "paragraphs": sentence_view(shown, stored)}

    @app.get("/api/books/{book_id}/chapters/{idx}/audio.mp3")
    def chapter_audio(book_id: str, idx: int):
        book_or_404(book_id)
        p = library.chapter_audio_path(book_id, idx)
        if not p.exists():
            raise HTTPException(404, "音频尚未生成")
        return FileResponse(p, media_type="audio/mpeg")

    @app.put("/api/books/{book_id}/progress")
    def put_progress(book_id: str, body: ProgressBody):
        book_or_404(book_id)
        if body.chapter not in {c["idx"] for c in store.list_chapters(book_id)}:
            raise HTTPException(404, "章节不存在")
        store.set_progress(book_id, body.chapter, body.para, body.word, body.time)
        store.set_read_ratio(book_id, body.chapter, min(1.0, max(0.0, body.ratio)))
        return store.get_progress(book_id)

    # ---- queue / voices ----
    @app.get("/api/queue")
    def get_queue():
        return queue.snapshot()

    @app.get("/api/voices")
    def get_voices():
        return VOICES

    @app.get("/api/voices/{voice}/preview.mp3")
    def voice_preview(voice: str):
        if voice not in _VOICE_IDS:
            raise HTTPException(404, "音色不存在")
        path = home / "previews" / f"{voice}.mp3"
        with preview_lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                samples = synth_preview(preview_synth, voice)
                tmp = path.with_name(path.name + ".tmp")
                try:
                    write_mp3(samples, tmp)
                    os.replace(tmp, path)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
        return FileResponse(path, media_type="audio/mpeg")

    # ---- vocab ----
    def check_selection(book_id: str, chapter: int) -> None:
        if not _valid_id(book_id):
            raise HTTPException(400, "书不存在")
        if store.get_book(book_id) is not None and \
                chapter not in {c["idx"] for c in store.list_chapters(book_id)}:
            raise HTTPException(404, "章节不存在")

    @app.post("/api/vocab", status_code=201)
    def create_vocab(body: VocabBody):
        check_selection(body.book_id, body.chapter)
        try:
            return save_vocab(store, library, body.book_id, body.chapter, body.para,
                              body.start_char, body.end_char, whole=body.whole, zh=body.zh,
                              gloss=body.gloss, gloss_mode=body.gloss_mode)
        except (ValueError, IndexError, FileNotFoundError) as e:
            raise HTTPException(400, str(e) or "选区不合法")

    # ---- 划词: 翻译 / 讲解 of a selection ----
    glosser: list[Translator] = []          # made on first use (needs the API key)
    quick_mt: list = []                     # free machine translation for 翻译 (None = off)
    glosses: OrderedDict = OrderedDict()   # (selection, mode) → result; the same ask costs nothing
    gloss_lock = threading.Lock()

    @app.post("/api/lookup")
    def lookup(body: LookupBody):
        check_selection(body.book_id, body.chapter)
        try:
            sel = selection_context(store, library, body.book_id, body.chapter, body.para,
                                    body.start_char, body.end_char)
        except (ValueError, IndexError, FileNotFoundError) as e:
            raise HTTPException(400, str(e) or "选区不合法")
        key = (body.book_id, body.chapter, body.para, sel["sentence"], sel["text"], body.mode)
        with gloss_lock:
            hit = glosses.get(key)
        if hit is None and body.mode == "quick":
            with gloss_lock:
                if not quick_mt:
                    quick_mt.append((mt_factory or make_mt)())
                machine = quick_mt[0]
            if machine is not None:
                try:
                    hit = machine.translate(sel["text"])
                except MTError as e:
                    log.warning("划词 machine translation failed, using the model: %s", e)
        if hit is None:
            try:
                with gloss_lock:
                    if not glosser:
                        glosser.append((translator_factory or make_translator)())
                    model = glosser[0]
                hit = (model.gloss(body.mode, sel["text"], sel["sentence"], sel["context"], sel["title"]),
                       "model")
            except TranslateError as e:
                raise HTTPException(502, f"查询失败：{e}")
            with gloss_lock:
                glosses[key] = hit
                while len(glosses) > GLOSS_CACHE:
                    glosses.popitem(last=False)
        return {"mode": body.mode, "text": sel["text"], "start_char": sel["start"], "end_char": sel["end"],
                "result": hit[0], "source": hit[1]}

    @app.get("/api/vocab")
    def list_vocab():
        return store.list_vocab()

    def vocab_or_404(vocab_id: str) -> dict:
        rec = store.get_vocab(vocab_id) if _valid_id(vocab_id) else None
        if rec is None:
            raise HTTPException(404, "生词不存在")
        return rec

    @app.delete("/api/vocab/{vocab_id}", status_code=204)
    def delete_vocab(vocab_id: str):
        vocab_or_404(vocab_id)
        store.delete_vocab(vocab_id)
        (home / "vocab" / f"{vocab_id}.mp3").unlink(missing_ok=True)
        return Response(status_code=204)

    @app.get("/api/vocab/{vocab_id}/clip.mp3")
    def vocab_clip(vocab_id: str):
        vocab_or_404(vocab_id)
        p = home / "vocab" / f"{vocab_id}.mp3"
        if not p.exists():
            raise HTTPException(404, "没有音频片段")
        return FileResponse(p, media_type="audio/mpeg")

    # ---- 生词本 → Anki (AnkiConnect) ----
    anki_lock = threading.Lock()

    def anki_client():
        return (anki_factory or ankimod.AnkiConnect)()

    def anki_call(client, action, **params):
        try:
            return client.call(action, **params)
        except ankimod.AnkiError as e:
            raise HTTPException(503 if "连不上" in str(e) else 502, str(e))

    @app.get("/api/anki/options")
    def anki_options():
        client = anki_client()
        return {"decks": anki_call(client, "deckNames"), "models": anki_call(client, "modelNames"),
                "config": ankimod.load_config(home)}

    @app.put("/api/anki/config")
    def anki_config(body: AnkiConfigBody):
        client = anki_client()
        if body.deck not in anki_call(client, "deckNames"):
            raise HTTPException(400, f"Anki 里没有牌组「{body.deck}」")
        if body.model not in anki_call(client, "modelNames"):
            raise HTTPException(400, f"Anki 里没有卡片类型「{body.model}」")
        fields = anki_call(client, "modelFieldNames", modelName=body.model)
        if len(fields) < 2:
            raise HTTPException(400, f"卡片类型「{body.model}」至少要有两个字段（正面 / 背面）")
        cfg = {"deck": body.deck, "model": body.model, "front": fields[0], "back": fields[1]}
        ankimod.save_config(home, cfg)
        return cfg

    @app.post("/api/vocab/{vocab_id}/anki")
    def vocab_to_anki(vocab_id: str):
        rec = vocab_or_404(vocab_id)
        with anki_lock:  # one push at a time: a double click must not add the note twice
            rec = store.get_vocab(vocab_id) or rec
            if rec.get("anki_note_id"):
                return {"note_id": rec["anki_note_id"], "already": True}
            cfg = ankimod.load_config(home)
            if cfg is None:
                raise HTTPException(400, "先在生词本里选择 Anki 牌组和卡片类型")
            try:
                note_id = ankimod.push(anki_client(), rec, cfg, home / "vocab" / f"{vocab_id}.mp3")
            except ankimod.AnkiError as e:
                msg = str(e)
                if "duplicate" in msg:
                    raise HTTPException(409, "Anki 里已有这张卡（同一牌组里正面相同），没有重复添加")
                raise HTTPException(503 if "连不上" in msg else 502, msg)
            store.update_vocab(vocab_id, anki_note_id=note_id)
        return {"note_id": note_id, "already": False}

    # ---- static (must come last) ----
    STATIC_DIR.mkdir(exist_ok=True)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app

