/* Echobook — 阅读设置 (Aa): font size, typeface, line spacing and column width of the text
   only (browser zoom would also blow up the bars). Loaded in <head> so a stored choice applies
   before the first paint; the button goes into the reader's .typo-slot. */
"use strict";
(() => {
  const KEY = "kokoro-reader:reading";
  const root = document.documentElement;
  const SIZE = { min: 15, max: 32, def: 20 };
  const WIDTHS = [["窄", 600], ["标准", 700], ["宽", 900], ["很宽", 1120]];
  const FONTS = [
    ["衬线", 'Georgia, "Iowan Old Style", "Songti SC", serif'],
    ["书籍", '"Iowan Old Style", "Palatino", "Book Antiqua", "Songti SC", serif'],
    ["无衬线", '-apple-system, BlinkMacSystemFont, "Helvetica Neue", "PingFang SC", sans-serif'],
  ];
  const LEADING = [["紧凑", 1.6], ["标准", 1.8], ["宽松", 2.0]];
  const DEF = { size: SIZE.def, width: 700, font: "衬线", leading: 1.8 };

  let cur = { ...DEF };
  try { cur = { ...DEF, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch (e) { /* defaults */ }

  function apply() {
    const s = root.style;
    const font = FONTS.find(([n]) => n === cur.font);
    // only what differs from the default is set inline, so the stylesheet (and its phone sizes) still rule otherwise
    cur.size === DEF.size ? s.removeProperty("--read-size") : s.setProperty("--read-size", cur.size + "px");
    cur.width === DEF.width ? s.removeProperty("--read-width") : s.setProperty("--read-width", cur.width + "px");
    cur.leading === DEF.leading ? s.removeProperty("--read-lh") : s.setProperty("--read-lh", String(cur.leading));
    font && cur.font !== DEF.font ? s.setProperty("--read-font", font[1]) : s.removeProperty("--read-font");
  }
  apply();

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(cur)); } catch (e) { /* applied, not remembered */ }
    apply();
    window.dispatchEvent(new Event("resize")); // the reader re-places the Chinese line / dock
  }

  function seg(label, items, key) {
    const row = document.createElement("div");
    row.className = "typo-row";
    row.innerHTML = `<span class="typo-lbl">${label}</span><div class="seg"></div>`;
    for (const [name, value] of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = name;
      b.dataset.v = String(value);
      b.addEventListener("click", () => { cur[key] = key === "font" ? name : value; save(); paint(); });
      row.querySelector(".seg").appendChild(b);
    }
    return row;
  }

  let panel, btn;
  function paint() {
    panel.querySelector(".typo-size").textContent = String(cur.size);
    panel.querySelector("[data-act='-']").disabled = cur.size <= SIZE.min;
    panel.querySelector("[data-act='+']").disabled = cur.size >= SIZE.max;
    for (const b of panel.querySelectorAll(".seg button")) {
      const key = b.closest(".typo-row").dataset.key;
      const on = key === "font" ? b.textContent === cur.font : Number(b.dataset.v) === cur[key];
      b.setAttribute("aria-pressed", String(on));
    }
  }

  function open(show) {
    panel.hidden = !show;
    btn.setAttribute("aria-expanded", String(show));
    if (show) paint();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const slot = document.querySelector(".typo-slot");
    if (!slot) return;
    const wrap = document.createElement("div");
    wrap.className = "typo";
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-btn typo-btn";
    btn.setAttribute("aria-label", "阅读设置");
    btn.setAttribute("aria-haspopup", "dialog");
    btn.title = "阅读设置：字号 / 字体 / 行距 / 宽度";
    btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 19 8 5l5.5 14M4.6 14h6.8M14 19l3.5-8.5L21 19m-5.6-3h4.2"/></svg>';

    panel = document.createElement("div");
    panel.className = "typo-pop";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "阅读设置");
    panel.hidden = true;
    const size = document.createElement("div");
    size.className = "typo-row";
    size.innerHTML = '<span class="typo-lbl">字号</span><div class="typo-step">' +
      '<button type="button" data-act="-" aria-label="字号减小">A−</button>' +
      '<span class="typo-size"></span>' +
      '<button type="button" data-act="+" aria-label="字号加大">A+</button></div>';
    size.querySelector("[data-act='-']").addEventListener("click", () => { cur.size = Math.max(SIZE.min, cur.size - 1); save(); paint(); });
    size.querySelector("[data-act='+']").addEventListener("click", () => { cur.size = Math.min(SIZE.max, cur.size + 1); save(); paint(); });
    panel.appendChild(size);
    const rows = [seg("字体", FONTS, "font"), seg("行距", LEADING, "leading"), seg("宽度", WIDTHS, "width")];
    rows[0].dataset.key = "font"; rows[1].dataset.key = "leading"; rows[2].dataset.key = "width";
    rows.forEach((r) => panel.appendChild(r));
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "typo-reset";
    reset.textContent = "恢复默认";
    reset.addEventListener("click", () => { cur = { ...DEF }; save(); paint(); });
    panel.appendChild(reset);

    // keep keyboard shortcuts (space, arrows) and any text selection where they were
    for (const el of [btn, panel]) el.addEventListener("mousedown", (e) => { if (e.target.closest("button")) e.preventDefault(); });
    btn.addEventListener("click", () => open(panel.hidden));
    document.addEventListener("mousedown", (e) => { if (!panel.hidden && !wrap.contains(e.target)) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) open(false); }, true);

    wrap.append(btn, panel);
    slot.replaceWith(wrap);
  });
})();
