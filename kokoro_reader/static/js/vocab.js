/* Echobook — vocab page: saved words/phrases and whole sentences (with their Chinese), newest first, with sentence clip playback. */
"use strict";

const $ = (id) => document.getElementById(id);
const els = { list: $("list"), count: $("count"), empty: $("empty") };

const audio = $("clip");
let playingId = null;

function pad(n) { return String(n).padStart(2, "0"); }

function fmtWhen(iso) /* epoch seconds from the API */ {
  const d = new Date(typeof iso === "number" ? iso * 1000 : iso);
  if (isNaN(d)) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Sentence HTML with the first case-sensitive occurrence of `text` wrapped in <mark>; all parts escaped. */
function sentenceHtml(rec) {
  const s = rec.sentence || "";
  const i = rec.text ? s.indexOf(rec.text) : -1;
  if (i < 0) return escapeHtml(s);
  return escapeHtml(s.slice(0, i)) + `<mark>${escapeHtml(rec.text)}</mark>` + escapeHtml(s.slice(i + rec.text.length));
}

/** "Ch <story number> · <title>" (same numbering as the book page); older records lack
 *  these fields and fall back to the raw chapter index. Returns escaped HTML. */
function chapterLabel(rec) {
  if (rec.story_number != null) {
    const title = rec.chapter_title ? ` · ${escapeHtml(rec.chapter_title)}` : "";
    return `Ch ${Number(rec.story_number)}${title}`;
  }
  if (rec.chapter_title) return `「${escapeHtml(rec.chapter_title)}」`;
  return `Ch ${Number(rec.chapter) + 1}`;
}

function itemHtml(rec) {
  return `<article class="vocab-item" data-id="${escapeHtml(rec.id)}">` +
    `<div class="v-main">` +
    (rec.kind === "sentence"
      ? `<div class="kind">收藏句子</div><p class="sentence whole">${escapeHtml(rec.sentence)}</p>`
      : `<div class="word">${escapeHtml(rec.text)}</div><p class="sentence">${sentenceHtml(rec)}</p>`) +
    (rec.zh ? `<p class="zh" lang="zh-CN">${escapeHtml(rec.zh)}</p>` : "") +
    (rec.gloss ? `<div class="gloss" lang="zh-CN"><span class="g-tag">${rec.gloss_mode === "explain" ? "讲解" : "翻译"}</span>${glossHtml(rec.gloss)}</div>` : "") +
    `<div class="v-meta"><span class="src">${escapeHtml(rec.book_title)} · ${chapterLabel(rec)}</span>` +
    `<span class="dot">·</span><span class="when">${fmtWhen(rec.created_at)}</span></div>` +
    `</div><div class="v-ops">` +
    (rec.sentence_clip ? `<button class="btn small" type="button" data-action="play" aria-label="🔈" title="播放句子片段">🔈</button>` : "") +
    ankiBtn(rec) +
    `<button class="btn small ghost" type="button" data-action="delete">删除</button>` +
    `</div></article>`;
}

function updateCount() {
  const n = els.list.querySelectorAll(".vocab-item").length;
  els.count.textContent = `共 ${n} 条`;
  els.empty.hidden = n > 0;
}

function resetPlaying() {
  if (playingId) {
    const b = els.list.querySelector(`.vocab-item[data-id="${playingId}"] [data-action="play"]`);
    if (b) b.textContent = "🔈";
  }
  playingId = null;
}

function stopAudio() {
  audio.pause();
  audio.removeAttribute("src");
  resetPlaying();
}

audio.addEventListener("ended", stopAudio);
audio.addEventListener("error", () => {
  if (!audio.getAttribute("src")) return;
  toast("片段播放失败", "error");
  stopAudio();
});

els.list.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const item = btn.closest(".vocab-item");
  const id = item.dataset.id;
  if (btn.dataset.action === "play") {
    const same = playingId === id;
    stopAudio();
    if (same) return;
    playingId = id;
    btn.textContent = "⏹";
    audio.src = `/api/vocab/${id}/clip.mp3`;
    audio.play().catch(() => {});
  } else if (btn.dataset.action === "delete") {
    const label = (item.querySelector(".word, .sentence.whole") || {}).textContent || "";
    if (!confirm(`删除「${label.length > 40 ? label.slice(0, 40) + "…" : label}」？`)) return;
    try {
      await api.del(`/api/vocab/${id}`);
    } catch (err) {
      if (err.status !== 404) { toast(err.message, "error"); return; }
    }
    if (playingId === id) stopAudio();
    item.remove();
    recs.delete(id);
    updateCount();
    renderAnkiBar();
  } else if (btn.dataset.action === "anki") {
    await pushOne(id);
  }
});

/* ---- 生词本 → Anki (AnkiConnect; the server talks to it) ---- */
const recs = new Map(); // id → record, in page order
const anki = { ok: false, error: "", config: null, decks: [], models: [], editing: false, busy: false };
const ankiBar = $("anki-bar");

function ankiBtn(rec) {
  if (rec.anki_note_id) return `<button class="btn small" type="button" data-action="anki" disabled>已在 Anki</button>`;
  const off = !anki.ok || !anki.config || anki.busy;
  return `<button class="btn small" type="button" data-action="anki"${off ? " disabled" : ""}>推到 Anki</button>`;
}

function refreshAnkiButtons() {
  for (const item of els.list.querySelectorAll(".vocab-item")) {
    const rec = recs.get(item.dataset.id), b = item.querySelector('[data-action="anki"]');
    if (rec && b) b.outerHTML = ankiBtn(rec);
  }
}

const unpushed = () => [...recs.values()].filter((r) => !r.anki_note_id);
const options = (list, pick) => list.map((x) => `<option${x === pick ? " selected" : ""}>${escapeHtml(x)}</option>`).join("");

function renderAnkiBar() {
  if (!anki.ok) {
    ankiBar.innerHTML = `<span class="anki-msg err">${escapeHtml(anki.error)}</span>` +
      `<button class="btn small ghost" type="button" data-act="retry">重试</button>`;
  } else if (!anki.config || anki.editing) {
    const c = anki.config || {};
    ankiBar.innerHTML = `<span class="anki-msg">推到 Anki 前选一次：</span>` +
      `<label>牌组 <select id="anki-deck">${options(anki.decks, c.deck)}</select></label>` +
      `<label>卡片类型 <select id="anki-model">${options(anki.models, c.model)}</select></label>` +
      `<button class="btn small primary" type="button" data-act="save">保存</button>` +
      (anki.config ? `<button class="btn small ghost" type="button" data-act="cancel">取消</button>` : "");
  } else {
    const n = unpushed().length;
    ankiBar.innerHTML = `<span class="anki-msg">Anki：<b>${escapeHtml(anki.config.deck)} · ${escapeHtml(anki.config.model)}</b></span>` +
      `<button class="btn small ghost" type="button" data-act="edit">更改</button>` +
      `<button class="btn small primary" type="button" data-act="all"${n && !anki.busy ? "" : " disabled"}>全部推到 Anki（${n} 条）</button>`;
  }
  refreshAnkiButtons();
}

async function loadAnki() {
  try {
    const o = await api.get("/api/anki/options");
    Object.assign(anki, { ok: true, error: "", config: o.config, decks: o.decks, models: o.models });
  } catch (e) {
    Object.assign(anki, { ok: false, error: e.message });
  }
  renderAnkiBar();
}

/** Push one record; true when it is in Anki now. Failures toast (a closed Anki also marks the bar). */
async function push(id, quiet) {
  try {
    const r = await api.post(`/api/vocab/${id}/anki`);
    recs.get(id).anki_note_id = r.note_id;
    return true;
  } catch (e) {
    if (e.status === 503) Object.assign(anki, { ok: false, error: e.message });
    if (!quiet || e.status !== 409) toast(e.message, "error");
    return e.status === 409 ? null : false; // null: a duplicate, skipped
  }
}

async function pushOne(id) {
  anki.busy = true;
  refreshAnkiButtons();
  if (await push(id)) toast("已推到 Anki");
  anki.busy = false;
  renderAnkiBar();
}

async function pushAll() {
  const todo = unpushed();
  anki.busy = true;
  renderAnkiBar();
  let done = 0, dup = 0;
  for (const rec of todo) {
    const ok = await push(rec.id, true);
    if (ok) done++;
    else if (ok === null) dup++;
    else break;
    refreshAnkiButtons();
  }
  anki.busy = false;
  renderAnkiBar();
  toast(`推了 ${done} 条` + (dup ? `，${dup} 条 Anki 里已有（跳过）` : ""));
}

ankiBar.addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "retry") loadAnki();
  else if (act === "edit") { anki.editing = true; renderAnkiBar(); }
  else if (act === "cancel") { anki.editing = false; renderAnkiBar(); }
  else if (act === "all") pushAll();
  else if (act === "save") {
    try {
      anki.config = await api.put("/api/anki/config", { deck: $("anki-deck").value, model: $("anki-model").value });
      anki.editing = false;
      renderAnkiBar();
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

async function init() {
  try {
    const list = await api.get("/api/vocab");
    for (const r of list) recs.set(r.id, r);
    els.list.innerHTML = list.map(itemHtml).join("");
  } catch (e) {
    toast(e.message, "error");
  }
  updateCount();
  loadAnki();
}
init();
