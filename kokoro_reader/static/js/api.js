/* Echobook — shared helpers: API client, toasts, polling, small formatters. Plain script, no build step. */
"use strict";

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function apiRequest(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("连不上 Echobook 服务，请确认它正在运行", 0);
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = text; }
  }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `请求失败（${res.status}）`;
    throw new ApiError(detail, res.status);
  }
  return { status: res.status, data };
}

const api = {
  get: (p) => apiRequest("GET", p).then((r) => r.data),
  put: (p, b) => apiRequest("PUT", p, b).then((r) => r.data),
  post: (p, b) => apiRequest("POST", p, b).then((r) => r.data),
  del: (p) => apiRequest("DELETE", p).then((r) => r.data),
  /** Upload one EPUB; resolves to {book, created}. */
  upload(file) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return apiRequest("POST", "/api/books", fd).then((r) => r.data);
  },
};

/* ---- toasts ---- */
function toast(message, kind = "info", ms = 3600) {
  let box = document.querySelector(".toasts");
  if (!box) {
    box = document.createElement("div");
    box.className = "toasts";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 320);
  }, ms);
  return el;
}

/* ---- polling: every `ms` while the page is visible, paused while hidden ---- */
function startPolling(fn, ms = 1000) {
  let timer = null;
  let running = false;
  async function tick() {
    timer = null;
    if (document.hidden || running) return;
    running = true;
    try { await fn(); } catch (e) { console.warn("poll failed", e); }
    running = false;
    if (!document.hidden && timer === null) timer = setTimeout(tick, ms);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else if (timer === null) {
      tick();
    }
  });
  timer = setTimeout(tick, ms);
}

/* ---- formatting ---- */
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** A 划词 reply as escaped HTML. A 讲解 comes as: the meaning line, then "▸ <English chunk>" blocks
 *  (explanation lines, an "例：" example), then an optional "注意：" line — laid out unit by unit.
 *  Anything without "▸" (翻译, older saves) stays plain text. */
function glossHtml(text) {
  const lines = String(text ?? "").split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.some((l) => l.startsWith("▸"))) return escapeHtml(lines.join("\n"));
  let html = "", unit = null, seenUnit = false;
  const close = () => { if (unit !== null) { html += `<div class="g-unit">${unit}</div>`; unit = null; } };
  for (const l of lines) {
    if (l.startsWith("▸")) {
      close();
      seenUnit = true;
      unit = `<div class="g-head" lang="en">${escapeHtml(l.slice(1).trim())}</div>`;
    } else if (l.startsWith("注意：")) {
      close();
      html += `<div class="g-note">${escapeHtml(l)}</div>`;
    } else if (unit !== null) {
      unit += `<div class="${l.startsWith("例：") ? "g-ex" : "g-line"}">${escapeHtml(l)}</div>`;
    } else {
      html += `<div class="${seenUnit || html ? "g-line" : "g-mean"}">${escapeHtml(l)}</div>`;
    }
  }
  close();
  return html;
}

/** The ▸ blocks of a 讲解 as {head, block}: block = the head line + its own lines (no 注意). */
function glossUnits(text) {
  const out = [];
  let cur = null;
  for (const raw of String(text ?? "").split("\n")) {
    const l = raw.trim();
    if (!l) continue;
    if (l.startsWith("▸")) { cur = { head: l.slice(1).trim(), lines: [l] }; out.push(cur); }
    else if (l.startsWith("注意：")) cur = null;
    else if (cur) cur.lines.push(l);
  }
  return out.map((u) => ({ head: u.head, block: u.lines.join("\n") }));
}

/** 161 MB · 4.3 GB · 820 KB */
function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1e6) return `${Math.max(0, Math.round(v / 1e3))} KB`;
  if (v < 1e9) return `${v < 1e7 ? (v / 1e6).toFixed(1) : Math.round(v / 1e6)} MB`;
  return `${(v / 1e9).toFixed(1)} GB`;
}

/** 预计 / 还剩 time: 不到 1 分钟 · 约 12 分钟 · 约 1.5 小时 */
function fmtEta(sec) {
  const s = Number(sec) || 0;
  if (s < 60) return "不到 1 分钟";
  if (s < 3600) return `约 ${Math.round(s / 60)} 分钟`;
  return `约 ${(s / 3600).toFixed(1).replace(/\.0$/, "")} 小时`;
}

function fmtDuration(sec) {
  if (sec == null) return "";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(r).padStart(2, "0");
}

function pct(ratio) {
  return Math.round(Math.min(1, Math.max(0, ratio || 0)) * 100);
}

/* ---- covers ---- */
const COVER_COLORS = ["#7b3f2e", "#1f4e5f", "#4b5a2a", "#5a3a6b", "#8a5a2b", "#2f4858", "#6b2f3f", "#3d5a4a", "#5b4636", "#2e3f63"];

function coverColor(title) {
  let h = 0;
  for (const ch of String(title || "")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return COVER_COLORS[h % COVER_COLORS.length];
}

/** Inner HTML for a `.cover-art` element: the real cover, or a coloured title block. */
function coverInner(book) {
  if (book.has_cover) {
    return `<img src="/api/books/${book.id}/cover" alt="${escapeHtml(book.title)}" draggable="false" loading="lazy">`;
  }
  return `<div class="placeholder" style="background:${coverColor(book.title)}">
      <div><div class="ph-title">${escapeHtml(book.title)}</div><div class="ph-rule"></div></div>
      <div class="ph-author">${escapeHtml(book.author || "")}</div>
    </div>`;
}

/** Fraction of a book's story text read, from per-chapter read ratios (≥ 0.98 counts as finished). */
function bookReadFraction(chapters) {
  const story = chapters.filter((c) => !c.is_matter);
  if (!story.length) return 0;
  const sum = story.reduce((acc, c) => acc + (c.read_ratio >= 0.98 ? 1 : Math.max(0, c.read_ratio || 0)), 0);
  return sum / story.length;
}
