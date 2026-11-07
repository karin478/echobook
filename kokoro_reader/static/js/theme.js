/* Echobook — 浅色 / 深色. Loaded in <head> so a stored pick applies before the first paint;
   with no pick the page follows the system. The toggle goes into the page's .theme-slot. */
"use strict";
(() => {
  const KEY = "kokoro-reader:theme";
  const root = document.documentElement;
  const dark = matchMedia("(prefers-color-scheme: dark)");
  let stored = null;
  try { stored = localStorage.getItem(KEY); } catch (e) { /* storage blocked: follow the system */ }
  if (stored === "light" || stored === "dark") root.dataset.theme = stored;

  const current = () => root.dataset.theme || (dark.matches ? "dark" : "light");

  function paint(btn) {
    const isDark = current() === "dark";
    btn.textContent = isDark ? "☀︎" : "☾";
    btn.title = isDark ? "换成浅色" : "换成深色";
  }

  document.addEventListener("DOMContentLoaded", () => {
    const slot = document.querySelector(".theme-slot");
    if (!slot) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-btn";
    btn.setAttribute("aria-label", "切换浅色 / 深色");
    btn.addEventListener("mousedown", (e) => e.preventDefault()); // keep keyboard focus / selection where it was
    btn.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem(KEY, next); } catch (e) { /* not remembered, still applied */ }
      paint(btn);
    });
    dark.addEventListener("change", () => paint(btn));
    paint(btn);
    slot.replaceWith(btn);
  });
})();
