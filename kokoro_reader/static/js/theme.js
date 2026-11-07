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

  const MOON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.6A8.2 8.2 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6z"/></svg>';
  const SUN = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></svg>';
  const current = () => root.dataset.theme || (dark.matches ? "dark" : "light");

  function paint(btn) {
    const isDark = current() === "dark";
    btn.innerHTML = isDark ? SUN : MOON;
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
