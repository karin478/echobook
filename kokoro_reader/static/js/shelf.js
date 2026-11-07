/* Echobook — shelf page: cover grid, upload (button / drag files anywhere), reorder, delete. */
"use strict";

const grid = document.getElementById("grid");
const fileInput = document.getElementById("file-input");
const overlay = document.getElementById("drop-overlay");
const emptyNote = document.getElementById("empty-note");

const state = {
  books: [],
  queue: { current: null, pending: [] },
  readFrac: {},        // book id -> fraction of story text read
  uploads: [],         // file names currently being imported
  dragId: null,        // book id being dragged for reordering
  dragStartOrder: null,
};

/* ---------- rendering ---------- */
function isBusy(id) {
  const q = state.queue;
  return (q.current && q.current.book_id === id) || q.pending.some(([b]) => b === id);
}

function metaHtml(book) {
  const frac = state.readFrac[book.id] || 0;
  const busy = isBusy(book.id);
  const dot = busy ? `<span class="busy-dot" title="正在准备"></span>` : "";
  const pctText = frac > 0 ? `<span class="pct">${frac >= 0.995 ? "读完" : pct(frac) + "%"}</span>` : "";
  const zh = book.translated ? `<div class="line sub">中文 ${book.translated}/${book.total} 章</div>` : "";
  return `<div class="line"><span class="conv">${dot}音频 ${book.converted}/${book.total} 章</span>${pctText}</div>${zh}
    <div class="read-bar"><i style="width:${(frac * 100).toFixed(1)}%"></i></div>`;
}

function cardHtml(book) {
  const label = book.author ? `${book.title} — ${book.author}` : book.title;
  return `<article class="book-card" draggable="true" data-id="${book.id}" title="${escapeHtml(label)}">
    <a class="cover cover-art" href="book.html?id=${book.id}" draggable="false">${coverInner(book)}</a>
    <button class="del-btn" type="button" aria-label="删除" title="删除这本书">×</button>
    <div class="card-meta">${metaHtml(book)}</div>
  </article>`;
}

function render() {
  const ghosts = state.uploads.map((name) => `<div class="ghost-card" title="${escapeHtml(name)}">
      <div class="cover cover-art"><span>导入中…</span></div>
      <div class="card-meta"><div class="line"><span>${escapeHtml(name)}</span></div></div>
    </div>`).join("");
  grid.innerHTML = state.books.map(cardHtml).join("") + ghosts +
    `<button class="add-tile" type="button" id="add-tile"><span class="plus">＋</span>拖 EPUB 到这里<br>或点击上传</button>`;
  emptyNote.hidden = state.books.length > 0 || state.uploads.length > 0;
}

function updateMetas() {
  for (const book of state.books) {
    const meta = grid.querySelector(`.book-card[data-id="${book.id}"] .card-meta`);
    if (!meta) continue;
    const html = metaHtml(book);
    if (meta.dataset.sig !== html) {
      meta.innerHTML = html;
      meta.dataset.sig = html;
    }
  }
}

/* ---------- data ---------- */
async function fetchShelf() {
  const [books, queue] = await Promise.all([api.get("/api/books"), api.get("/api/queue")]);
  state.books = books;
  state.queue = queue;
}

async function loadReadProgress() {
  const withProgress = state.books.filter((b) => b.progress);
  await Promise.all(withProgress.map(async (b) => {
    try {
      const detail = await api.get(`/api/books/${b.id}`);
      state.readFrac[b.id] = bookReadFraction(detail.chapters);
    } catch (e) { /* book vanished; next load fixes it */ }
  }));
  updateMetas();
}

async function load() {
  try {
    await fetchShelf();
  } catch (e) {
    toast(e.message, "error");
    return;
  }
  render();
  loadReadProgress();
  loadStorage();
}

function sameOrder(ids) {
  const shown = [...grid.querySelectorAll(".book-card[data-id]")].map((el) => el.dataset.id);
  return shown.length === ids.length && shown.every((id, i) => id === ids[i]);
}

async function poll() {
  if (state.dragId || state.uploads.length) return;
  await fetchShelf();
  if (state.dragId || state.uploads.length) return;
  if (sameOrder(state.books.map((b) => b.id))) updateMetas();
  else { render(); loadReadProgress(); }
}

/* ---------- upload ---------- */
async function uploadOne(file) {
  state.uploads.push(file.name);
  render();
  try {
    const res = await api.upload(file);
    if (res.created) toast(`已添加《${res.book.title}》`);
    else toast("这本书已经在书架上了");
  } catch (e) {
    toast(e.message, "error", 5000);
  } finally {
    state.uploads.splice(state.uploads.indexOf(file.name), 1);
  }
}

// uploads run strictly one after another, even across separate picks / drops
let uploadChain = Promise.resolve();
function uploadFiles(files) {
  for (const f of files) {
    uploadChain = uploadChain.then(() => uploadOne(f)).then(load);
  }
  return uploadChain;
}

document.getElementById("upload-btn").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  const files = [...fileInput.files];
  fileInput.value = "";
  if (files.length) uploadFiles(files);
});

/* ---------- drag files anywhere → overlay → upload ---------- */
let fileDragDepth = 0;
const hasFiles = (e) => !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");

document.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  fileDragDepth++;
  overlay.hidden = false;
});
document.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  fileDragDepth = Math.max(0, fileDragDepth - 1);
  if (fileDragDepth === 0) overlay.hidden = true;
});
document.addEventListener("dragover", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
});
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  fileDragDepth = 0;
  overlay.hidden = true;
  const files = [...e.dataTransfer.files];
  if (files.length) uploadFiles(files);
});

/* ---------- drag covers to reorder ---------- */
const shownIds = () => [...grid.querySelectorAll(".book-card[data-id]")].map((el) => el.dataset.id);

grid.addEventListener("dragstart", (e) => {
  const card = e.target.closest && e.target.closest(".book-card[data-id]");
  if (!card) return;
  state.dragId = card.dataset.id;
  state.dragStartOrder = shownIds();
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", card.dataset.id);
  hideActions();
  requestAnimationFrame(() => card.classList.add("dragging"));
});

grid.addEventListener("dragover", (e) => {
  if (!state.dragId) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  const over = e.target.closest && e.target.closest(".book-card[data-id]");
  const dragged = grid.querySelector(`.book-card[data-id="${state.dragId}"]`);
  if (!over || !dragged || over === dragged) return;
  const r = over.getBoundingClientRect();
  if (e.clientX < r.left + r.width / 2) {
    if (over.previousElementSibling !== dragged) over.before(dragged);
  } else if (over.nextElementSibling !== dragged) {
    over.after(dragged);
  }
});

async function finishDrag() {
  if (!state.dragId) return;
  const dragged = grid.querySelector(`.book-card[data-id="${state.dragId}"]`);
  if (dragged) dragged.classList.remove("dragging");
  const before = state.dragStartOrder || [];
  const ids = shownIds();
  state.dragId = null;
  state.dragStartOrder = null;
  if (ids.join() === before.join()) return;
  try {
    await api.put("/api/books/order", { ids });
    const byId = Object.fromEntries(state.books.map((b) => [b.id, b]));
    state.books = ids.map((id) => byId[id]).filter(Boolean);
  } catch (err) {
    toast(err.message, "error");
    load();
  }
}

grid.addEventListener("drop", (e) => {
  if (!state.dragId) return;
  e.preventDefault();
  finishDrag();
});
grid.addEventListener("dragend", () => finishDrag());

/* ---------- delete (hover or right-click) ---------- */
function hideActions(except) {
  grid.querySelectorAll(".book-card.show-actions").forEach((el) => {
    if (el !== except) el.classList.remove("show-actions");
  });
}

grid.addEventListener("contextmenu", (e) => {
  const card = e.target.closest(".book-card[data-id]");
  if (!card) return;
  e.preventDefault();
  hideActions(card);
  card.classList.toggle("show-actions");
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".del-btn")) hideActions();
});

grid.addEventListener("click", async (e) => {
  if (e.target.closest("#add-tile")) {
    fileInput.click();
    return;
  }
  const del = e.target.closest(".del-btn");
  if (!del) return;
  e.preventDefault();
  const card = del.closest(".book-card");
  const book = state.books.find((b) => b.id === card.dataset.id);
  if (book) openDelete(book);
});

/* 删除: only the audio, or the whole book — the 生词本 stays either way */
const delDlg = document.getElementById("del-dlg");
let delBook = null;

async function openDelete(book) {
  delBook = book;
  delDlg.setAttribute("aria-label", `删除《${book.title}》`);
  delDlg.querySelector(".del-title").textContent = `删除《${book.title}》`;
  const use = delDlg.querySelector(".del-use");
  const audioBtn = delDlg.querySelector('[data-del="audio"]');
  use.textContent = "";
  audioBtn.hidden = true;
  delDlg.showModal();
  try {
    const u = (await api.get("/api/storage")).books[book.id];
    if (delBook !== book || !u) return;
    use.textContent = u.audio ? `音频 ${fmtBytes(u.audio)} · 文字和中文 ${fmtBytes(u.other)}` : `文字和中文 ${fmtBytes(u.other)}（还没有生成音频）`;
    audioBtn.hidden = !u.audio;
  } catch (e) { /* sizes are a nicety; deleting still works */ }
}

delDlg.addEventListener("click", async (e) => {
  const what = e.target === delDlg ? "cancel" : e.target.closest("[data-del]")?.dataset.del; // backdrop = cancel
  if (!what) return;
  const book = delBook;
  delDlg.close();
  if (what === "cancel" || !book) return;
  try {
    if (what === "audio") {
      await api.del(`/api/books/${book.id}/audio`);
      toast(`已删除音频：《${book.title}》（文字和中文还在）`);
    } else {
      await api.del(`/api/books/${book.id}`);
      toast(`已删除《${book.title}》`);
    }
  } catch (err) {
    toast(err.message, "error");
  }
  load();
});

async function loadStorage() {
  try {
    const s = await api.get("/api/storage");
    const el = document.getElementById("storage");
    el.textContent = `占用 ${fmtBytes(s.total)}`;
  } catch (e) { /* not shown */ }
}

/* ---------- go ---------- */
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadReadProgress(); });
load();
startPolling(poll, 1000);
