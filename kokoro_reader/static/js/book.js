/* Echobook — book page: info, continue reading, voice, conversion controls, chapter list. */
"use strict";

const bookId = new URLSearchParams(location.search).get("id") || "";
const $ = (id) => document.getElementById(id);
const els = {
  book: $("book"), missing: $("missing"), cover: $("cover"), title: $("title"), meta: $("meta"),
  cont: $("continue"), voice: $("voice"), voiceName: $("voice-name"), prepare: $("prepare"),
  list: $("chapters"), prep: $("prep"),
};

const VOICE_GROUPS = ["美式女声", "美式男声", "英式女声", "英式男声",
  "微软 美式女声", "微软 美式男声", "微软 英式女声", "微软 英式男声"]; // 微软 = Edge Natural voices (online)
const GENERIC_TITLE = /^\s*(chapter|ch\.?)?\s*([0-9]+|[ivxlcdm]+)\s*[.:]?\s*$/i;

const state = {
  book: null,
  queue: { current: null, pending: [] },
  voices: [],
  matterOpen: false,
  openMenu: null,        // idx of the story row whose "⋯" menu is open
  structSig: "",
  rowHtml: new Map(),    // idx -> last rendered html
  voiceSaving: false,
  coverDone: false,
};

/* ---------- derived ---------- */
/** Time to make the audio for these chapters, and its size — this machine's measured speed. */
function estimateFor(chs) {
  const e = state.book.estimate;
  const chars = chs.reduce((n, c) => n + (c.chars || 0), 0);
  if (!e || !chars) return null;
  return { secs: chars / e.conv_cps, bytes: (chars / e.audio_cps) * e.audio_bytes_per_sec };
}
const estText = (x) => `预计 ${fmtEta(x.secs)} · 音频约 ${fmtBytes(x.bytes)}`;

function storyChapters() { return state.book.chapters.filter((c) => !c.is_matter); }
function matterChapters() { return state.book.chapters.filter((c) => c.is_matter); }

function numbering() {
  const map = new Map();
  storyChapters().forEach((c, i) => map.set(c.idx, i + 1));
  return map;
}

function chapterLabel(idx, nums) {
  if (nums.has(idx)) return `Ch ${nums.get(idx)}`;
  const ch = state.book.chapters.find((c) => c.idx === idx);
  return ch && ch.title ? `「${ch.title}」` : `第 ${idx + 1} 部分`;
}

function readerHref(idx) { return `reader.html?book=${bookId}&ch=${idx}`; }

function voiceName(id) {
  const v = state.voices.find((x) => x.id === id);
  return v ? v.label.split("（")[0] : id;
}

/** One status per chapter for its audio + Chinese together (the 中文 column shows translation detail). */
function chapterState(ch) {
  const cur = state.queue.current;
  if (cur && cur.book_id === bookId && cur.idx === ch.idx) return { cls: "run", text: `音频 ${pct(cur.progress)}%` };
  if (ch.status === "converting") return { cls: "run", text: "音频 0%" };
  if (ch.status === "queued") return { cls: "q", text: "排队中" };
  if (ch.status === "error") return { cls: "err", text: "出错" };
  if (ch.zh === "error") return { cls: "err", text: "翻译失败" };
  if (ch.read_ratio >= 0.98) return { cls: "ok", text: "✓ 已读完" };
  if (ch.read_ratio > 0) return { cls: "ok", text: `读到 ${pct(ch.read_ratio)}%` };
  if (ch.status === "done" && ch.zh === "done") return { cls: "ok", text: "已准备" };
  if (ch.status === "done" && ch.zh === "running") return { cls: "run", text: `翻译 ${pct(ch.zh_progress || 0)}%` };
  if (ch.status === "done" && ch.zh === "queued") return { cls: "q", text: "排队翻译" };
  return { cls: "none", text: "未准备" };
}

/* ---------- header ---------- */
function renderHeader() {
  const b = state.book;
  const nums = numbering();
  document.title = `${b.title} · Echobook`;
  els.title.textContent = b.title;
  if (!state.coverDone) {
    els.cover.innerHTML = coverInner(b);
    state.coverDone = true;
  }

  const parts = [];
  if (b.author) parts.push(escapeHtml(b.author));
  parts.push(`${b.total} 章`, `音频 ${b.converted}/${b.total}`, `中文 ${b.translated}/${b.total}`);
  if (b.glossary) parts.push(`<span title="全书统一译名的人名和术语">术语表 ${b.glossary} 条</span>`);
  if (b.disk) {
    parts.push(`<span title="音频 ${fmtBytes(b.disk.audio)} · 文字和中文 ${fmtBytes(b.disk.other)}（删除在书架上：只删音频或整本书）">占用 ${fmtBytes(b.disk.audio + b.disk.other)}</span>`);
  }
  const prog = b.progress;
  if (prog) {
    const ch = b.chapters.find((c) => c.idx === prog.chapter);
    const ratio = ch ? ch.read_ratio : 0;
    parts.push(ratio >= 0.98 ? `读完 ${escapeHtml(chapterLabel(prog.chapter, nums))}`
      : `读到 ${escapeHtml(chapterLabel(prog.chapter, nums))} 的 ${pct(ratio)}%`);
  }
  const metaHtml = parts.join('<span class="dot">·</span>');
  if (els.meta.innerHTML !== metaHtml) els.meta.innerHTML = metaHtml;

  const first = storyChapters()[0] || b.chapters[0];
  if (prog) {
    els.cont.textContent = `▶ 继续阅读 ${chapterLabel(prog.chapter, nums)}`;
    els.cont.href = readerHref(prog.chapter);
  } else {
    els.cont.textContent = "▶ 开始阅读";
    els.cont.href = first ? readerHref(first.idx) : "#";
  }
  els.cont.hidden = !first && !prog;

  if (!state.voiceSaving) showVoice(b.voice);
  renderPrep();
}

/** Two thin progress bars while the book is being prepared (anything queued / converting / translating). */
function renderPrep() {
  const b = state.book;
  const story = storyChapters();
  const busy = story.some((c) => c.status === "queued" || c.status === "converting" ||
    c.zh === "queued" || c.zh === "running");
  els.prep.hidden = !busy;
  const done = b.total ? story.length : 0;
  els.prepare.hidden = !story.length || (b.converted === b.total && b.translated === b.total);
  const todo = estimateFor(story.filter((c) => c.status !== "done"));
  $("prep-est").textContent = !busy && !els.prepare.hidden && todo ? estText(todo) : "";
  const left = (state.queue.eta || {})[bookId];
  $("prep-eta").textContent = busy && left ? `音频还剩${fmtEta(left)}` : "";
  if (!busy || !done) return;
  const cur = state.queue.current;
  const audioPart = story.reduce((n, c) => n + (c.status === "done" ? 1
    : cur && cur.book_id === bookId && cur.idx === c.idx ? cur.progress || 0 : 0), 0);
  const zhPart = story.reduce((n, c) => n + (c.zh === "done" ? 1 : c.zh === "running" ? c.zh_progress || 0 : 0), 0);
  $("prep-audio").style.width = `${(audioPart / done) * 100}%`;
  $("prep-zh").style.width = `${(zhPart / done) * 100}%`;
  $("prep-audio-n").textContent = `${b.converted} / ${b.total}`;
  $("prep-zh-n").textContent = `${b.translated} / ${b.total}`;
}

/* ---------- chapter list ---------- */
/** The title is the way in: any chapter opens in the reader (an unprepared one reads as text first). */
function titleHtml(ch) {
  const title = (ch.title || "").trim();
  const first = (ch.first_line || "").trim();
  const inner = !title || GENERIC_TITLE.test(title) ? escapeHtml(first || title || "（无标题）")
    : escapeHtml(title) + (first && first !== title ? ` <span class="first">· ${escapeHtml(first)}</span>` : "");
  return `<a href="${readerHref(ch.idx)}">${inner}</a>`;
}

function opsHtml(ch) {
  const out = [];
  const failed = ch.status === "error" || ch.zh === "error";
  const busy = ch.status === "queued" || ch.status === "converting" || ch.zh === "queued" || ch.zh === "running";
  if (failed && !busy) out.push(`<button class="btn small" type="button" data-action="convert" title="重新准备这一章的音频和中文">重试</button>`);
  else if (!busy && (ch.status === "none" || ch.zh === "none")) {
    const x = estimateFor([ch]);
    out.push(`<button class="btn small hover-only" type="button" data-action="convert" title="生成这一章的朗读音频 + 逐句中文${x ? "（" + estText(x) + "）" : ""}">准备</button>`);
  }
  if (ch.status === "done" && ch.voice && ch.voice !== state.book.voice) {
    out.push(`<button class="btn small" type="button" data-action="reconvert" title="用 ${escapeHtml(voiceName(state.book.voice))} 重新生成这一章的音频">用新音色重录</button>`);
  }
  if (ch.is_matter) {
    out.push(`<button class="btn small ghost switch-btn hover-only" type="button" data-action="set-story" title="把它当作正文（会计入章节和「准备整本书」）">设为正文</button>`);
  } else {
    const open = state.openMenu === ch.idx;
    out.push(`<button class="row-menu-btn" type="button" data-action="menu" aria-label="更多操作" aria-expanded="${open}">⋯</button>`);
    if (open) out.push(`<div class="row-menu" role="menu"><button type="button" data-action="set-matter">设为非正文</button></div>`);
  }
  return out.join("");
}

/** The pieces of one chapter row: its classes and each cell's inner HTML. */
function rowParts(ch, nums) {
  const st = chapterState(ch);
  const num = nums.get(ch.idx);
  const prog = state.book.progress;
  const cls = ["chapter-row", ch.is_matter ? "matter" : "story"];
  if (prog && prog.chapter === ch.idx) cls.push("current");
  const showDur = ch.duration != null && ch.status !== "none";
  return {
    cls: cls.join(" "), num,
    cells: {
      num: num ? `Ch ${num}` : "",
      ttl: titleHtml(ch),
      dur: showDur ? fmtDuration(ch.duration) : "",
      "tag-cell": `<span class="tag ${st.cls}">${st.text}</span>`,
      ops: opsHtml(ch),
    },
  };
}

function rowHtml(ch, nums) {
  const r = rowParts(ch, nums);
  const tip = [ch.title, ch.first_line].filter(Boolean).join(" — ");
  const c = r.cells;
  return `<div class="${r.cls}" data-idx="${ch.idx}"${r.num ? ` data-num="${r.num}"` : ""}>` +
    `<span class="num">${c.num}</span>` +
    `<span class="ttl" title="${escapeHtml(tip)}">${c.ttl}</span>` +
    `<span class="dur">${c.dur}</span>` +
    `<span class="ops">${c.ops}</span>` +
    `<span class="tag-cell">${c["tag-cell"]}</span></div>`;
}

/** Poll updates touch only the cells that changed, so a button or link under the pointer (or
 *  with keyboard focus) is not replaced mid-click while a percentage ticks. */
function patchRow(el, ch, nums) {
  const r = rowParts(ch, nums);
  if (el.className !== r.cls) el.className = r.cls;
  const last = el._cells || (el._cells = {}); // what we last wrote (innerHTML is browser-normalised)
  for (const [k, html] of Object.entries(r.cells)) {
    const cell = el.querySelector(`:scope > .${k}`);
    if (cell && last[k] !== html && cell.innerHTML !== html) cell.innerHTML = html;
    last[k] = html;
  }
}

function structureSig() {
  return JSON.stringify([state.matterOpen,
    state.book.chapters.map((c) => [c.idx, c.is_matter, c.part])]);
}

function renderList() {
  const nums = numbering();
  const sig = structureSig();
  if (sig !== state.structSig) {
    state.structSig = sig;
    state.rowHtml.clear();
    const html = [];
    let part = null;
    for (const ch of storyChapters()) {
      if (ch.part && ch.part !== part) html.push(`<div class="part-head">${escapeHtml(ch.part)}</div>`);
      part = ch.part;
      const r = rowHtml(ch, nums);
      state.rowHtml.set(ch.idx, r);
      html.push(r);
    }
    const matter = matterChapters();
    if (matter.length) {
      html.push(`<button class="matter-toggle" type="button" aria-expanded="${state.matterOpen}">` +
        `已隐藏 ${matter.length} 个非正文部分 <span class="arrow">▸</span></button>`);
      html.push(`<div class="matter-list"${state.matterOpen ? "" : " hidden"}>` + matter.map((ch) => {
        const r = rowHtml(ch, nums);
        state.rowHtml.set(ch.idx, r);
        return r;
      }).join("") + "</div>");
    }
    els.list.innerHTML = html.join("");
    return;
  }
  for (const ch of state.book.chapters) {
    const r = rowHtml(ch, nums);
    if (state.rowHtml.get(ch.idx) === r) continue;
    const el = els.list.querySelector(`.chapter-row[data-idx="${ch.idx}"]`);
    if (el) patchRow(el, ch, nums);
    state.rowHtml.set(ch.idx, r);
  }
}

function render() {
  renderHeader();
  renderList();
}

/* ---------- data ---------- */
let seq = 0;
async function refresh() {
  const my = ++seq;
  let book, queue;
  try {
    [book, queue] = await Promise.all([api.get(`/api/books/${bookId}`), api.get("/api/queue")]);
  } catch (e) {
    if (e.status === 404) showMissing();
    throw e;
  }
  if (my !== seq) return;
  state.book = book;
  state.queue = queue;
  els.book.hidden = false;
  render();
}

function showMissing() {
  els.book.hidden = true;
  els.missing.hidden = false;
  document.title = "找不到这本书 · Echobook";
}

async function act(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message, "error");
  }
  await refresh().catch(() => {});
}

/* ---------- chapter actions ---------- */
els.list.addEventListener("click", (e) => {
  if (e.target.closest(".matter-toggle")) {
    state.matterOpen = !state.matterOpen;
    renderList();
    return;
  }
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const idx = Number(btn.closest(".chapter-row").dataset.idx);
  const action = btn.dataset.action;
  const base = `/api/books/${bookId}`;
  if (action === "menu") {
    state.openMenu = state.openMenu === idx ? null : idx;
    renderList();
  } else if (action === "convert") {
    const ch = state.book.chapters.find((c) => c.idx === idx);
    const x = ch && ch.status !== "done" ? estimateFor([ch]) : null;
    act(async () => {
      await api.post(`${base}/convert`, { mode: "one", chapter: idx });
      toast(x ? `开始准备这一章：${estText(x)}` : "开始准备这一章的中文");
    });
  } else if (action === "reconvert") {
    act(() => api.post(`${base}/convert`, { mode: "reconvert", chapter: idx }));
  } else if (action === "set-matter") {
    state.openMenu = null;
    act(async () => {
      await api.put(`${base}/chapters/${idx}/matter`, { is_matter: true });
      toast("已移到「非正文部分」，需要时可以展开改回来");
    });
  } else if (action === "set-story") {
    act(() => api.put(`${base}/chapters/${idx}/matter`, { is_matter: false }));
  }
});

document.addEventListener("click", (e) => {
  if (state.openMenu !== null && !e.target.closest(".row-menu, .row-menu-btn")) {
    state.openMenu = null;
    renderList();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.openMenu !== null) {
    state.openMenu = null;
    renderList();
  }
});

/* ---------- 准备整本书: audio + Chinese for every story chapter ---------- */
els.prepare.addEventListener("click", () => act(async () => {
  els.prepare.disabled = true;
  try {
    const x = estimateFor(storyChapters().filter((c) => c.status !== "done"));
    const r = await api.post(`/api/books/${bookId}/prepare`);
    if (!r.audio && !r.zh) toast("正文章节都已经准备好或在处理中了");
    else toast(`开始准备：${r.audio} 章音频、${r.zh} 章中文${r.audio && x ? "（" + estText(x) + "）" : ""}，会在后台慢慢做完，可以先读已好的章节`);
  } finally {
    els.prepare.disabled = false;
  }
}));

/* ---------- voice picker: a dialog where a click plays that voice's 试听 and 确定 keeps it ---------- */
const voiceDlg = $("voice-dlg"), voiceBody = $("voice-body"), voiceCur = $("voice-cur");
let picked = null;        // the voice chosen in the open dialog (saved only on 确定)
let previewing = null;    // the voice whose 试听 is loading / playing

const groupOf = (v) => `${v.engine === "edge" ? "微软 " : ""}${v.accent}${v.gender}声`;

function showVoice(id) {
  const name = voiceName(id);
  if (els.voiceName.textContent !== name) els.voiceName.textContent = name;
}

function fillVoices() {
  const groups = new Map(VOICE_GROUPS.map((g) => [g, []]));
  for (const v of state.voices) {
    const g = groupOf(v);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(`<button type="button" class="voice-opt" data-voice="${escapeHtml(v.id)}" aria-pressed="false">` +
      `<span class="nm">${escapeHtml(v.label.split("（")[0])}</span>${v.default ? "<small>默认</small>" : ""}</button>`);
  }
  voiceBody.innerHTML = [...groups].filter(([, opts]) => opts.length)
    .map(([g, opts]) => `<section class="voice-group"><h3>${g}</h3><div class="voice-grid">${opts.join("")}</div></section>`)
    .join("");
}

function voiceOpts() { return voiceBody.querySelectorAll(".voice-opt"); }

function markVoices() {
  for (const b of voiceOpts()) {
    b.setAttribute("aria-pressed", String(b.dataset.voice === picked));
    b.classList.toggle("loading", b.dataset.voice === previewing && previewAudio.paused);
    b.classList.toggle("playing", b.dataset.voice === previewing && !previewAudio.paused);
  }
  const v = state.voices.find((x) => x.id === picked);
  voiceCur.textContent = v ? `已选：${voiceName(v.id)}（${groupOf(v)}）` : "";
}

const previewAudio = new Audio();
function playPreview(id) {
  previewing = id;
  previewAudio.src = `/api/voices/${encodeURIComponent(id)}/preview.mp3`;
  previewAudio.play().catch(() => {});
  markVoices();
}
function stopPreview() {
  previewing = null;
  previewAudio.pause();
  previewAudio.removeAttribute("src");
  markVoices();
}
previewAudio.addEventListener("playing", markVoices);
previewAudio.addEventListener("ended", stopPreview);
previewAudio.addEventListener("error", () => {
  if (!previewAudio.getAttribute("src")) return;
  toast("试听生成失败，请稍后再试", "error");
  stopPreview();
});

voiceBody.addEventListener("click", (e) => {
  const b = e.target.closest(".voice-opt");
  if (!b) return;
  const id = b.dataset.voice;
  if (id === picked && id === previewing) { stopPreview(); return; } // a second click stops it
  picked = id;
  playPreview(id);
});

els.voice.addEventListener("click", () => {
  if (!state.book) return;
  picked = state.book.voice;
  markVoices();
  voiceDlg.showModal();
  const cur = voiceBody.querySelector('.voice-opt[aria-pressed="true"]');
  if (cur) cur.scrollIntoView({ block: "nearest" });
});
voiceDlg.addEventListener("close", stopPreview); // 取消 / Esc / 确定 all end the 试听
voiceDlg.addEventListener("click", (e) => { // a click on the backdrop = 取消
  if (e.target !== voiceDlg) return;
  const r = voiceDlg.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) voiceDlg.close();
});
$("voice-cancel").addEventListener("click", () => voiceDlg.close());
$("voice-ok").addEventListener("click", async () => {
  const voice = picked;
  voiceDlg.close();
  if (!voice || voice === state.book.voice) return;
  state.voiceSaving = true;
  showVoice(voice);
  try {
    await api.put(`/api/books/${bookId}/voice`, { voice });
    toast(`音色已换成 ${voiceName(voice)}，之后准备的章节会用它`);
  } catch (e) {
    toast(e.message, "error");
  }
  state.voiceSaving = false;
  await refresh().catch(() => {});
});

/* ---------- go ---------- */
async function init() {
  if (!/^[0-9a-f]{32}$/.test(bookId)) {
    showMissing();
    return;
  }
  try {
    state.voices = await api.get("/api/voices");
    fillVoices();
    await refresh();
  } catch (e) {
    if (e.status !== 404) toast(e.message, "error");
    if (!state.book) return;
  }
  startPolling(() => refresh(), 1000);
}
init();
