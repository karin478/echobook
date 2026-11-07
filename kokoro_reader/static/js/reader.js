/* Echobook — read-along page. Ported from the prototype (reader_template.html):
   word highlight via requestAnimationFrame + binary search, autoscroll paused for 4 s after a
   manual scroll, click-to-seek, Space / ← / →. Adds speed, resume, auto-continue, text mode and
   vocab capture. Plain script, no build step; needs api.js. */
"use strict";

(() => {
  const params = new URLSearchParams(location.search);
  const bookId = params.get("book") || "";
  let chIdx = Number.parseInt(params.get("ch"), 10);

  const $ = (id) => document.getElementById(id);
  const au = $("au"), box = $("text"), bar = $("bar"), playBtn = $("play"), prog = $("prog"), timeEl = $("time");
  const rateInput = $("rate"), rateLabel = $("rate-label"), star = $("star");
  const pop = $("gloss-pop"), popBody = $("gloss-body");
  const banner = $("banner"), bannerMsg = $("banner-msg"), bannerBtn = $("banner-btn");
  const dock = $("dock"), kbdHint = $("kbd-hint");

  const RATE_KEY = "kokoro-reader:rate";
  const SAVE_EVERY_MS = 10000;
  const USER_SCROLL_HOLD_MS = 4000;
  const EMPTY_SKIP_MS = 2000;
  const SCROLL_KEYS = new Set(["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"]);

  let bookTitle = "";
  let resume = null;        // server progress {chapter, para, word, time}; applied once
  let ch = null;            // chapter payload from the API
  let gen = 0;              // bumped on every chapter switch; stale async work checks it
  let playable = false;
  let metaReady = false;    // audio metadata loaded and the initial seek applied
  let pendingSeek = null;
  let lastSaved = null;     // {chapter, time} of the last progress PUT
  let poller = null;
  let skipTimer = null;
  let rate = 1;

  // highlight state (prototype): spans = timed tokens in time order
  let paras = [], spans = [], paraTimed = [], paraStart = [];
  let cur = -1, curP = -1, lastScroll = 0, userScroll = 0, rafId = 0, barH = 0;
  let clickSeek = null;     // {time, paused, at}: undone if the click turns into a double-click
  let pendingVocab = null;
  let savingVocab = false;  // a ⭐ POST is in flight; further clicks are ignored
  let actedSinceEnd = false; // reader seeked/played after 'ended': don't auto-jump to the next chapter

  // 逐句 (the only mode): one unit (a Chinese piece's sentence range) at a time
  let stopMode = false;     // 一句一停: pause at each unit end; else 连续 (follow the audio)
  let sents = null;         // /sentences payload + {idx}; null = (re)load pending
  let units = [], unitsIdx = null, u = -1;
  let zhOn = true;           // show the current unit's Chinese (sticky, remembered)
  let unitEnd = null;       // pause playback when currentTime reaches this
  let sentTimer = null, sentGen = 0;

  // ---------------------------------------------------------------- anchors (mirror anchors.py)
  /** Index of the last span with a <= t, or -1. */
  function find(t) {
    let lo = 0, hi = spans.length - 1, r = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (spans[m].a <= t) { r = m; lo = m + 1; } else hi = m - 1;
    }
    return r;
  }

  /** anchor_for_time: (para, word) of the last timed token with a <= t; before the first, the first. */
  function anchorForTime(t) {
    if (!spans.length) return [0, 0];
    const s = spans[Math.max(0, find(t))];
    return [s.p, s.wi];
  }

  /** time_for_anchor: a of the paragraph's word-th timed token (clamped); null if it has none. */
  function timeForAnchor(para, word) {
    const list = paraTimed[para];
    if (!list || !list.length) return null;
    return spans[list[Math.max(0, Math.min(word, list.length - 1))]].a;
  }

  // ---------------------------------------------------------------- rendering
  /** Paragraph text must equal `paragraphs[i]` exactly: tokens' w + s, trailing whitespace dropped. */
  function render(data) {
    const tokens = data.tokens || [];
    const byPara = data.paragraphs.map(() => []);
    tokens.forEach((t, i) => { if (byPara[t.p]) byPara[t.p].push(i); });
    paras = []; spans = []; paraTimed = []; paraStart = [];
    cur = -1; curP = -1;
    const frag = document.createDocumentFragment();
    data.paragraphs.forEach((text, pi) => {
      const p = document.createElement("p");
      p.dataset.p = pi;
      const local = [];
      let budget = text.length; // display text is a prefix of the joined tokens
      for (const i of byPara[pi]) {
        if (budget <= 0) break;
        const t = tokens[i];
        const el = document.createElement("w");
        const c = text.length - budget; // char offset of this word in the paragraph
        el.dataset.i = i;
        el.dataset.c = c;
        const w = t.w.slice(0, budget);
        el.textContent = w;
        budget -= w.length;
        p.appendChild(el);
        if (t.a !== null && t.a !== undefined) {
          local.push({ a: t.a, b: t.b ?? t.a, el, p: pi, wi: local.length, c });
        }
        const s = (t.s || "").slice(0, Math.max(0, budget));
        if (s) { p.appendChild(document.createTextNode(s)); budget -= s.length; }
      }
      if (p.textContent !== text) { // never show text that differs from the server's contract
        console.warn(`paragraph ${pi}: tokens don't rebuild the display text; showing it untimed`);
        p.textContent = text;
        local.length = 0;
      }
      paraTimed.push(local.map((_, k) => spans.length + k));
      paraStart.push(local.length ? local[0].a : null);
      spans.push(...local);
      paras.push(p);
      frag.appendChild(p);
    });
    if (spans.some((s, k) => k && s.a < spans[k - 1].a)) { // binary search needs time order
      const order = spans.map((s, k) => k).sort((x, y) => spans[x].a - spans[y].a || x - y);
      const pos = new Map(order.map((k, j) => [k, j]));
      spans = order.map((k) => spans[k]);
      paraTimed = paraTimed.map((list) => list.map((k) => pos.get(k)));
    }
    box.replaceChildren(frag);
  }

  // ---------------------------------------------------------------- playback + highlight
  function fmt(s) {
    s = Math.floor(s || 0);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(r).padStart(2, "0");
  }

  function duration() {
    return Number.isFinite(au.duration) && au.duration > 0 ? au.duration : (ch && ch.duration) || 0;
  }

  let lastTimeText = "";
  function tick() {
    const t = au.currentTime, i = find(t);
    // 一句一停: stop at the unit end (not mid-seek: "seeked" picks the unit we land in)
    if (unitEnd !== null && !au.paused && !inSeek && t >= unitEnd) { unitEnd = null; au.pause(); }
    if (i !== cur) {
      if (cur >= 0) spans[cur].el.classList.remove("on");
      cur = i;
      const p = i >= 0 ? spans[i].p : -1;
      if (p !== curP) {
        if (curP >= 0) paras[curP].classList.remove("cur");
        if (p >= 0) paras[p].classList.add("cur");
        curP = p;
      }
      followAudio(i);
      if (i >= 0) {
        const el = spans[i].el;
        el.classList.add("on");
        const now = Date.now();
        // the only layout read per word change; no autoscroll while the reader is selecting text
        if (pop.hidden && now - userScroll > USER_SCROLL_HOLD_MS && now - lastScroll > 400) {
          const r = el.getBoundingClientRect();
          if (r.top < 80 || r.bottom > innerHeight - barH - 40) {
            lastScroll = now;
            if (units[u]) scrollUnit(); // keeps the sentence and its Chinese in view together
            else el.scrollIntoView({ block: "center" });
          }
        }
      }
    }
    const d = duration();
    const text = fmt(t) + " / " + fmt(d);
    if (text !== lastTimeText) { timeEl.textContent = text; lastTimeText = text; }
    if (d) prog.value = (t / d) * 1000;
  }

  function loop() {
    rafId = 0;
    tick();
    if (!au.paused) rafId = requestAnimationFrame(loop);
  }

  function kick() {
    if (!rafId) rafId = requestAnimationFrame(loop);
  }

  function play() {
    const p = au.play();
    if (p) p.catch(() => {});
  }

  function toggle() {
    drillToggle();
  }

  function seek(t) {
    if (au.readyState >= 1) au.currentTime = t;
    else pendingSeek = t;
  }

  /** Paragraph jump from the playhead — only until the sentence units have loaded. */
  function jumpPara(d) {
    const i = find(pendingSeek !== null ? pendingSeek : au.currentTime);
    const p = i >= 0 ? spans[i].p : 0;
    let q = p + d;
    while (q >= 0 && q < paras.length && paraStart[q] === null) q += d;
    if (q >= 0 && q < paras.length) seek(paraStart[q]);
  }

  function setPlayIcon(on) {
    playBtn.classList.toggle("playing", on);
    playBtn.setAttribute("aria-label", on ? "暂停" : "播放");
  }
  au.addEventListener("play", () => {
    setPlayIcon(true);
    // 一句一停 started from outside our own controls (media key, headphones): stop at this unit's end
    if (stopMode && unitEnd === null) {
      const x = units[u];
      if (x && x.b !== null && au.currentTime < x.b - 0.05) unitEnd = x.b;
    }
    kick();
  });
  // rAF does not run in a hidden tab: timeupdate still stops 一句一停 at the unit end
  au.addEventListener("timeupdate", () => {
    if (unitEnd !== null && !au.paused && !inSeek && au.currentTime >= unitEnd) { unitEnd = null; au.pause(); }
  });
  au.addEventListener("pause", () => { setPlayIcon(false); tick(); saveProgress(); });
  // any jump of the playhead (slider, resume, chapter end) moves the current sentence with it
  // between "seeking" and our "seeked" handler the old stop point must not fire (timeupdate comes first)
  let inSeek = false;
  au.addEventListener("seeking", () => { inSeek = true; });
  au.addEventListener("seeked", () => {
    inSeek = false;
    syncUnit(find(au.currentTime));
    if (stopMode && !au.paused) { const x = units[u]; unitEnd = x && x.b !== null ? x.b : null; } // the unit we landed in
    tick();
    if (!au.paused) kick();
  });
  // O1: an mp3 that fails to load (service restarted, file re-recorded) says so instead of staying silent
  au.addEventListener("error", () => {
    if (!playable || !au.getAttribute("src")) return;
    const t = au.currentTime;
    showBanner("音频加载失败（服务可能刚重启）", {
      button: { label: "重试", onClick: () => { hideBanner(); pendingSeek = t; au.load(); } },
    });
  });
  if (navigator.mediaSession) { // media keys / headphones go through the same controls as Space and ←/→
    const ms = navigator.mediaSession;
    try {
      ms.setActionHandler("play", () => drillToggle());
      ms.setActionHandler("pause", () => au.pause());
      ms.setActionHandler("nexttrack", () => goUnit(u + 1));
      ms.setActionHandler("previoustrack", () => goUnit(u - 1));
    } catch (e) { /* unsupported action */ }
  }
  au.addEventListener("loadedmetadata", () => {
    if (pendingSeek !== null) { au.currentTime = pendingSeek; pendingSeek = null; }
    metaReady = true;
    lastSaved = { chapter: chIdx, time: au.currentTime };
    tick();
  });
  au.addEventListener("ended", onEnded);
  au.addEventListener("seeking", () => { actedSinceEnd = true; });
  au.addEventListener("play", () => { actedSinceEnd = true; });

  playBtn.addEventListener("click", toggle);
  $("prev").addEventListener("click", () => goUnit(u - 1));
  $("next").addEventListener("click", () => goUnit(u + 1));
  prog.addEventListener("input", () => { const d = duration(); if (d) seek((prog.value / 1000) * d); });
  for (const r of [prog, rateInput]) r.addEventListener("change", () => r.blur()); // ←/→ go back to sentences
  // bar buttons never take focus, so Space can't also "click" a focused button
  for (const b of bar.querySelectorAll("button")) b.addEventListener("mousedown", (e) => e.preventDefault());

  addEventListener("wheel", () => { userScroll = Date.now(); }, { passive: true });
  addEventListener("touchmove", () => { userScroll = Date.now(); }, { passive: true });
  // ---------------------------------------------------------------- dock (sentence strip + transport)
  const HINT = "空格 播放/暂停 · ←/→ 上/下一句 · ↑ 重听本句 · ↓ 中文开/关 · F 收藏本句 · S 连续/一句一停 · 点单词从那里读 · 划选出翻译/讲解（T 切换）· [ ] 语速";
  function syncDock() {
    kbdHint.textContent = HINT;
    barH = dock.hidden ? 0 : dock.offsetHeight;
    document.body.style.setProperty("--dock-h", barH + "px");
  }
  if (typeof ResizeObserver === "function") new ResizeObserver(() => syncDock()).observe(dock);
  addEventListener("resize", syncDock);

  // click: pick that sentence and read from the clicked word; drag-selections / double-clicks don't move playback
  box.addEventListener("click", (e) => drillClick(e));

  /** The token itself if timed, else the nearest timed token of its paragraph (e.g. an opening quote). */
  function timedNear(i) {
    const toks = ch.tokens || [];
    const p = toks[i] && toks[i].p;
    for (let j = i; j < toks.length && toks[j].p === p; j++) if (toks[j].a !== null) return toks[j];
    for (let j = i - 1; j >= 0 && toks[j].p === p; j--) if (toks[j].a !== null) return toks[j];
    return null;
  }

  function undoClickSeek() {
    if (clickSeek && performance.now() - clickSeek.at < 800) {
      seek(clickSeek.time);
      if (clickSeek.paused) au.pause();
    }
    clickSeek = null;
  }

  // ---------------------------------------------------------------- speed
  function setRate(r, persist = true) {
    r = Math.min(2, Math.max(0.5, Math.round(r * 20) / 20));
    rate = r;
    au.defaultPlaybackRate = r; // survives src changes (in-place chapter switch)
    au.playbackRate = r;
    rateInput.value = String(r);
    rateLabel.textContent = r.toFixed(2) + "×";
    if (persist) {
      try { localStorage.setItem(RATE_KEY, String(r)); } catch (e) { /* private mode */ }
    }
  }

  function storedRate() {
    try {
      const v = Number.parseFloat(localStorage.getItem(RATE_KEY));
      return Number.isFinite(v) ? v : 1;
    } catch (e) {
      return 1;
    }
  }

  rateInput.addEventListener("input", () => setRate(Number(rateInput.value)));

  // ---------------------------------------------------------------- keyboard
  addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target, tag = t.tagName;
    const barRange = tag === "INPUT" && t.type === "range" && bar.contains(t);
    if ((tag === "INPUT" && !barRange) || tag === "SELECT" || tag === "TEXTAREA" || t.isContentEditable) return;
    if (e.key === "Escape") {
      if (!pop.hidden) { hideStar(); getSelection().removeAllRanges(); }
      return;
    }
    // e.code, so the keys also work while a Chinese input method is active
    if (e.code === "BracketLeft" || e.code === "BracketRight") {
      e.preventDefault();
      setRate(rate + (e.code === "BracketRight" ? 0.05 : -0.05));
      return;
    }
    if (e.code === "KeyS") {
      e.preventDefault();
      if (!e.repeat) setStopMode(!stopMode);
      return;
    }
    if (e.code === "KeyT") {
      e.preventDefault();
      if (!e.repeat) setGlossMode(glossMode === "quick" ? "explain" : "quick");
      return;
    }
    if (drillKey(e, tag, barRange)) return;
    if (SCROLL_KEYS.has(e.code)) userScroll = Date.now();
  });

  // ---------------------------------------------------------------- progress
  function saveProgress({ keepalive = false, force = false } = {}) {
    if (!playable || !metaReady || !ch) return;
    const d = duration();
    const t = au.ended ? d || au.currentTime : au.currentTime;
    if (!force && lastSaved && lastSaved.chapter === chIdx && Math.abs(lastSaved.time - t) < 0.05) return;
    const [para, word] = anchorForTime(t);
    const body = {
      chapter: chIdx, para, word, time: Math.round(t * 1000) / 1000,
      ratio: au.ended ? 1 : d ? Math.min(1, t / d) : 0,
    };
    lastSaved = { chapter: chIdx, time: t };
    const req = fetch(`/api/books/${bookId}/progress`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive,
    });
    if (keepalive) { req.catch(() => {}); return; } // page is going away: fire-and-forget
    req.then((res) => { if (!res.ok) console.warn("progress save failed:", res.status, body); })
      .catch((e) => console.warn("progress save failed:", e, body));
  }

  setInterval(() => { if (playable && !au.paused) saveProgress(); }, SAVE_EVERY_MS);
  document.addEventListener("visibilitychange", () => { if (document.hidden) saveProgress({ keepalive: true }); });
  addEventListener("pagehide", () => saveProgress({ keepalive: true }));

  // ---------------------------------------------------------------- banner + polling
  function showBanner(msg, { busy = false, button = null } = {}) {
    bannerMsg.replaceChildren();
    if (busy) {
      const dot = document.createElement("span");
      dot.className = "busy-dot";
      bannerMsg.appendChild(dot);
    }
    bannerMsg.appendChild(document.createTextNode(msg));
    if (button) {
      bannerBtn.textContent = button.label;
      bannerBtn.onclick = button.onClick;
      bannerBtn.disabled = false;
      bannerBtn.hidden = false;
    } else {
      bannerBtn.hidden = true;
      bannerBtn.onclick = null;
    }
    banner.hidden = false;
  }

  function hideBanner() {
    banner.hidden = true;
    bannerBtn.onclick = null;
  }

  function stopPoll() {
    if (poller) { poller.alive = false; clearTimeout(poller.timer); poller = null; }
  }

  /** Run fn now and then every second while it returns true; stops on chapter switch. */
  function startPoll(fn) {
    stopPoll();
    const me = { alive: true, timer: null };
    const my = gen;
    poller = me;
    const run = async () => {
      if (!me.alive || my !== gen) return;
      let again = true;
      if (!document.hidden) {
        try { again = await fn(); } catch (e) { console.warn("poll failed", e); }
      }
      if (again && me.alive && my === gen) me.timer = setTimeout(run, 1000);
    };
    run();
  }

  /** "生成音频 42%" / "排队中" for a chapter, from the queue snapshot; null when it isn't queued. */
  function queueLabel(q, idx) {
    if (q.current && q.current.book_id === bookId && q.current.idx === idx) {
      return `生成音频 ${Math.round((q.current.progress || 0) * 100)}%`;
    }
    if ((q.pending || []).some(([b, i]) => b === bookId && i === idx)) return "排队中";
    return null;
  }

  function statusLabel(status) {
    if (status === "converting") return "生成音频 0%";
    if (status === "queued") return "排队中";
    return null;
  }

  function isReady(d) {
    return !!d.audio_url || (d.status === "done" && Array.isArray(d.tokens) && d.tokens.length === 0);
  }

  const chapterUrl = (idx) => `/api/books/${bookId}/chapters/${idx}`;
  const convertNow = (idx) => api.post(`/api/books/${bookId}/convert`, { mode: "one", chapter: idx });

  // ---------------------------------------------------------------- chapter modes
  function resetAudio() {
    playable = false;
    metaReady = false;
    pendingSeek = null;
    bar.hidden = true;
    syncDock();
    if (au.getAttribute("src")) {
      au.pause();
      au.removeAttribute("src");
      au.load();
    }
  }

  function enterPlayable(d, autoplay) {
    playable = true;
    metaReady = false;
    lastSaved = null;
    bar.hidden = false;
    syncDock();
    let start = 0;
    if (resume && resume.chapter === chIdx) {
      const t = timeForAnchor(resume.para, resume.word);
      start = t !== null ? t : resume.time || 0;
      resume = null;
    }
    pendingSeek = start > 0 ? start : null;
    lastTimeText = "";
    prog.value = 0;
    au.src = d.audio_url;
    au.playbackRate = rate;
    tick();
    if (autoplay) play();
  }

  /** No audio yet: plain text, banner with live status, switch in place once audio exists. */
  function enterTextMode(d) {
    resetAudio();
    const idx = chIdx;
    const offer = (msg) => showBanner(msg, {
      button: {
        label: "准备此章",
        onClick: async () => {
          bannerBtn.disabled = true;
          try { await convertNow(idx); } catch (e) { toast(e.message, "error"); }
          watchCurrent();
        },
      },
    });
    const watchCurrent = () => startPoll(async () => {
      const q = await api.get("/api/queue");
      if (idx !== chIdx) return false;
      const label = queueLabel(q, idx);
      if (label) { showBanner(`本章音频还没好 · ${label}`, { busy: true }); return true; }
      const fresh = await api.get(chapterUrl(idx));
      if (idx !== chIdx) return false;
      if (isReady(fresh)) { showChapter(fresh, { keepScroll: true }); return false; }
      if (fresh.status === "queued" || fresh.status === "converting") return true;
      offer(fresh.status === "error" ? "本章音频生成失败" : "本章音频还没好");
      return false;
    });
    const label = statusLabel(d.status);
    if (label) showBanner(`本章音频还没好 · ${label}`, { busy: true });
    else offer(d.status === "error" ? "本章音频生成失败" : "本章音频还没好");
    watchCurrent();
  }

  function enterEmpty(d, autoplay) {
    resetAudio();
    if (d.next_idx === null || d.next_idx === undefined) {
      showBanner("本章没有可朗读的文字");
      return;
    }
    showBanner("本章没有可朗读的文字 · 2 秒后进入下一章");
    const my = gen;
    skipTimer = setTimeout(() => { if (my === gen) loadChapter(d.next_idx, { autoplay }); }, EMPTY_SKIP_MS);
  }

  function updateNav(d) {
    const link = (el, idx) => {
      el.hidden = idx === null || idx === undefined;
      if (!el.hidden) el.href = `reader.html?book=${bookId}&ch=${idx}`;
    };
    link($("prev-ch"), d.prev_idx);
    link($("next-ch"), d.next_idx);
    $("ch-nav").hidden = $("prev-ch").hidden && $("next-ch").hidden;
  }

  function showChapter(d, { autoplay = false, keepScroll = false } = {}) {
    stopPoll();
    clearTimeout(skipTimer);
    hideStar();
    hideBanner();
    ch = d;
    if (resume && resume.chapter !== chIdx) resume = null; // only the first chapter shown may resume
    $("ch-title").textContent = d.title;
    document.title = bookTitle ? `${d.title} · ${bookTitle}` : d.title;
    history.replaceState(history.state, "", `reader.html?book=${bookId}&ch=${chIdx}`);
    updateNav(d);
    render(d);
    if (!keepScroll) { lastScroll = Date.now(); scrollTo({ top: 0, behavior: "instant" }); }
    if (d.audio_url) enterPlayable(d, autoplay);
    else if (isReady(d)) enterEmpty(d, autoplay);
    else enterTextMode(d);
    units = []; unitsIdx = null; u = -1; // never paint/save the previous chapter's sentences on this one
    sents = null; unitEnd = null; paint(); renderCard(); loadSentences();
  }

  async function loadChapter(idx, { autoplay = false, data = null } = {}) {
    const my = ++gen;
    stopPoll();
    clearTimeout(skipTimer);
    chIdx = idx;
    const open = api.post(`/api/books/${bookId}/convert`, { mode: "open", chapter: idx });
    if (data) { // continuing in place: play first, queue the following chapter in the background
      open.catch(() => {});
      showChapter(data, { autoplay });
      return;
    }
    try {
      await open;
      const d = await api.get(chapterUrl(idx));
      if (my === gen) showChapter(d, { autoplay });
    } catch (e) {
      if (my !== gen) return;
      if (e.status === 404) showMissing();
      else toast(e.message, "error");
    }
  }

  /** A chapter finished: continue in place (keeps Chrome's autoplay permission) or wait for the next. */
  async function onEnded() {
    actedSinceEnd = false;
    saveProgress({ force: true });
    if (stopMode) return; // 一句一停 stays on this chapter; 连续 moves on to the next one
    const next = ch ? ch.next_idx : null;
    if (next === null || next === undefined) {
      showBanner("全书读完了 · 这是最后一章");
      return;
    }
    const my = gen;
    let d;
    try { d = await api.get(chapterUrl(next)); } catch (e) { toast(e.message, "error"); return; }
    if (my !== gen) return;
    if (isReady(d)) { loadChapter(next, { autoplay: true, data: d }); return; }
    waitForNext(next, d);
  }

  function waitForNext(next, d) {
    const button = {
      label: "准备",
      onClick: async () => {
        bannerBtn.disabled = true;
        try { await convertNow(next); } catch (e) { toast(e.message, "error"); }
        watch();
      },
    };
    const say = (label) => showBanner(`下一章音频还没好${label ? " · " + label : ""}`, { busy: !!label, button });
    const watch = () => startPoll(async () => {
      const q = await api.get("/api/queue");
      const label = queueLabel(q, next);
      if (label) { say(label); return true; }
      const fresh = await api.get(chapterUrl(next));
      if (isReady(fresh)) {
        if (actedSinceEnd) { hideBanner(); return false; } // reader moved on (seek/play); don't yank them
        loadChapter(next, { autoplay: true, data: fresh });
        return false;
      }
      if (fresh.status === "queued" || fresh.status === "converting") return true;
      say(fresh.status === "error" ? "音频生成失败" : null);
      return false;
    });
    say(statusLabel(d.status));
    watch();
  }

  function showMissing() {
    stopPoll();
    resetAudio();
    hideBanner();
    box.replaceChildren();
    $("ch-title").textContent = "";
    $("ch-nav").hidden = true;
    $("missing").hidden = false;
  }

  // ---------------------------------------------------------------- vocab selection
  // Offsets count only the book's own text: text nodes directly in the <p> or in its <w> words.
  // Anything else inside the <p> (e.g. a translation extension's injected 译文) is ignored.
  function ownTextNodes(p) {
    const out = [];
    for (const c of p.childNodes) {
      if (c.nodeType === 3) out.push(c);
      else if (c.nodeName === "W" && c.firstChild?.nodeType === 3) out.push(c.firstChild);
    }
    return out;
  }
  const ownText = (p) => ownTextNodes(p).map((n) => n.data).join("");

  /** Own-text offset of the boundary point (container, offset) inside paragraph p. */
  function ownOffset(p, container, offset) {
    const at = document.createRange();
    at.setStart(container, offset);
    let n = 0;
    for (const t of ownTextNodes(p)) {
      if (t === container) return n + offset;
      if (at.comparePoint(t, t.length) > 0) break; // this node ends after the point
      n += t.length;
    }
    return n;
  }

  /** Selection → {para, start, end} relative to the start paragraph's own text (clipped to it). */
  function selectionInfo() {
    const sel = getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const r = sel.getRangeAt(0);
    let node = r.startContainer;
    let p = (node.nodeType === 1 ? node : node.parentElement)?.closest("p[data-p]");
    let start;
    if (p && box.contains(p)) {
      start = ownOffset(p, r.startContainer, r.startOffset);
    } else if (node === box && box.children[r.startOffset]) {
      p = box.children[r.startOffset];
      start = 0;
    } else {
      return null;
    }
    const text = ownText(p);
    const end = p.contains(r.endContainer)
      ? ownOffset(p, r.endContainer, r.endOffset)
      : text.length; // crosses paragraphs: keep only the start paragraph's part
    if (end <= start || !text.slice(start, end).trim()) return null;
    return { para: Number(p.dataset.p), start, end, range: r };
  }

  // 划词: a selection pops up its 翻译 or AI 讲解 at once (the mode is the sticky switch next to
  // 中文, T toggles); ⭐ 收藏 in the popup saves the word with that result.
  const GLOSS_KEY = "kokoro-reader:gloss";
  const GLOSS_WAIT = { quick: "翻译中…", explain: "讲解中…" };
  let glossMode = "quick";
  let gloss = null;          // {mode, result} for the open selection, once it arrived
  let glossReq = null;       // in-flight lookup (⭐ waits for it so the result is saved too)
  let glossSeq = 0, glossTimer = 0;
  try { if (localStorage.getItem(GLOSS_KEY) === "explain") glossMode = "explain"; } catch (e) { /* private mode */ }

  function syncGlossMode() {
    $("gloss-quick").setAttribute("aria-pressed", String(glossMode === "quick"));
    $("gloss-explain").setAttribute("aria-pressed", String(glossMode === "explain"));
  }

  function setGlossMode(mode) {
    if (mode === glossMode) return;
    glossMode = mode;
    try { localStorage.setItem(GLOSS_KEY, mode); } catch (e) { /* private mode */ }
    syncGlossMode();
    if (!pop.hidden && pendingVocab && !savingVocab) lookup(pendingVocab);
  }

  function lookup(v) {
    const seq = ++glossSeq, mode = glossMode;
    gloss = null;
    popBody.textContent = GLOSS_WAIT[mode];
    popBody.className = "g-body wait";
    placePop();
    glossReq = api.post("/api/lookup", {
      book_id: bookId, chapter: v.chapter, para: v.para, start_char: v.start, end_char: v.end, mode,
    }).then((r) => {
      if (seq !== glossSeq) return;
      if (r.start_char != null && (r.start_char !== v.start || r.end_char !== v.end)) grownTo(v, r.start_char, r.end_char);
      gloss = { mode, result: r.result };
      const SRC = { google: "Google", mymemory: "MyMemory" }; // free machine translation, not the model
      popBody.innerHTML = glossHtml(r.result) + (SRC[r.source] ? `<span class="g-src">${SRC[r.source]}</span>` : "");
      popBody.className = "g-body" + (popBody.querySelector(".g-unit") ? " units" : "");
      placePop();
    }, (e) => {
      if (seq !== glossSeq) return;
      popBody.textContent = e.message;
      popBody.className = "g-body err";
      placePop();
    });
  }

  /** 智能扩选: the server grew the selection (whole word / phrasal verb) — show and keep that range. */
  function grownTo(v, start, end) {
    v.start = start;
    v.end = end;
    const r = paras[v.para] ? rangeIn(paras[v.para], start, end) : null;
    if (!r) return;
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    const rects = [...r.getClientRects()].filter((x) => x.width > 0 && x.height > 0);
    const page = (q) => ({ top: q.top + scrollY, bottom: q.bottom + scrollY, left: q.left + scrollX, width: q.width });
    if (rects.length) popAnchor = { first: page(rects[0]), last: page(rects[rects.length - 1]) };
  }

  function hideStar() {
    if (savingVocab) return; // stays up (inert) until the save finishes, so a 2nd click can't fall through
    clearTimeout(glossTimer);
    glossSeq++;
    pop.hidden = true;
    pendingVocab = null;
    gloss = glossReq = null;
  }

  let popAnchor = null; // the selection's first/last line boxes, in page coordinates
  function placePop() {
    if (pop.hidden || !popAnchor) return;
    const w = pop.offsetWidth, h = pop.offsetHeight;
    const { first, last } = popAnchor;
    let top = first.top - h - 8;
    if (top < scrollY + 8) top = last.bottom + 8; // no room above: put it under the selection
    const mid = first.left + first.width / 2;
    const left = Math.min(scrollX + innerWidth - w - 8, Math.max(scrollX + 8, mid - w / 2));
    pop.style.top = `${top}px`;
    pop.style.left = `${left}px`;
  }

  function updateStar() {
    const info = selectionInfo();
    if (!info || !ch) { hideStar(); return; }
    const v = { chapter: chIdx, para: info.para, start: info.start, end: info.end };
    const p = pendingVocab;
    if (!pop.hidden && p && p.chapter === v.chapter && p.para === v.para && p.start === v.start && p.end === v.end) return;
    if (savingVocab) return;
    pendingVocab = v;
    const rects = [...info.range.getClientRects()].filter((x) => x.width > 0 && x.height > 0);
    const page = (r) => ({ top: r.top + scrollY, bottom: r.bottom + scrollY, left: r.left + scrollX, width: r.width });
    const f = rects[0] || info.range.getBoundingClientRect();
    popAnchor = { first: page(f), last: page(rects[rects.length - 1] || f) };
    pop.hidden = false;
    // a drag or shift-arrows settles first: one lookup per selection, not one per step
    clearTimeout(glossTimer);
    glossSeq++;
    gloss = glossReq = null;
    popBody.textContent = GLOSS_WAIT[glossMode];
    popBody.className = "g-body wait";
    placePop();
    glossTimer = setTimeout(() => { if (pendingVocab === v) lookup(v); }, 120);
  }

  document.addEventListener("mouseup", (e) => {
    if (pop.contains(e.target)) return; // clicking inside the popup must not re-show it
    setTimeout(updateStar, 0);
  });
  document.addEventListener("keyup", (e) => { if (e.shiftKey) updateStar(); });
  document.addEventListener("selectionchange", () => {
    if (!pop.hidden && getSelection().isCollapsed) hideStar();
  });
  pop.addEventListener("mousedown", (e) => e.preventDefault()); // keep the selection alive
  for (const [id, mode] of [["gloss-quick", "quick"], ["gloss-explain", "explain"]]) {
    $(id).addEventListener("mousedown", (e) => e.preventDefault());
    $(id).addEventListener("click", () => setGlossMode(mode));
  }
  syncGlossMode();
  star.addEventListener("click", async () => {
    const v = pendingVocab;
    if (savingVocab || !v) return;
    savingVocab = true;
    star.classList.add("busy");
    try {
      clearTimeout(glossTimer);
      if (!glossReq && !gloss) lookup(v); // clicked before the lookup even started
      await glossReq;
      const j = v.chapter === unitsIdx ? unitAt(v.para, v.start) : -1;
      const x = j >= 0 ? units[j] : null;
      const zh = x && x.p === v.para && x.start <= v.start && v.start < x.end ? x.zh : null;
      const rec = await api.post("/api/vocab", {
        book_id: bookId, chapter: v.chapter, para: v.para, start_char: v.start, end_char: v.end, zh,
        gloss: gloss ? gloss.result : null, gloss_mode: gloss ? gloss.mode : null,
      });
      toast(`已收藏：${rec.text}`);
      getSelection().removeAllRanges();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      savingVocab = false;
      star.classList.remove("busy");
      hideStar();
    }
  });

  // ---------------------------------------------------------------- 逐句: sentence units, Chinese, 连续 / 一句一停
  const STOP_KEY = "kokoro-reader:stop";
  const zhPos = $("zh-pos"), zhHint = $("zh-hint"), zhText = $("zh-text"), zhBody = $("zh-body");
  const zhRetry = $("zh-retry"), zhToggle = $("zh-toggle"), modeCont = $("mode-cont"), modeStop = $("mode-stop");
  const ZH_KEY = "kokoro-reader:zh";
  const canHighlight = typeof Highlight === "function" && !!(window.CSS && CSS.highlights);

  function storedZh() {
    try { return localStorage.getItem(ZH_KEY) !== "0"; } catch (e) { return true; }
  }

  function storedStop() {
    try { return localStorage.getItem(STOP_KEY) === "1"; } catch (e) { return false; }
  }

  /** 连续 ⇄ 一句一停; takes effect on the sentence that is playing right now. */
  function setStopMode(on, persist = true) {
    stopMode = on;
    modeCont.setAttribute("aria-pressed", String(!on));
    modeStop.setAttribute("aria-pressed", String(on));
    if (persist) { try { localStorage.setItem(STOP_KEY, on ? "1" : "0"); } catch (e) { /* private mode */ } }
    const x = units[u];
    unitEnd = on && x && x.b !== null && !au.paused ? x.b : null;
  }

  for (const [b, on] of [[modeCont, false], [modeStop, true]]) {
    b.addEventListener("click", () => setStopMode(on));
    b.addEventListener("mousedown", (e) => e.preventDefault()); // keep Space for playback
  }
  zhRetry.addEventListener("click", async () => {
    try { await api.post(`/api/books/${bookId}/chapters/${chIdx}/translate`); } catch (e) { toast(e.message, "error"); }
    sents = null;
    loadSentences();
  });

  /** Fetch sentence spans + segments; start translating if needed; poll until it settles. */
  async function loadSentences() {
    const my = ++sentGen, idx = chIdx;
    clearTimeout(sentTimer);
    let d;
    try {
      d = await api.get(`/api/books/${bookId}/chapters/${idx}/sentences`);
    } catch (e) {
      if (my !== sentGen) return;
      renderCard("加载失败：" + e.message + "（几秒后自动重试）");
      sentTimer = setTimeout(() => { if (my === sentGen) loadSentences(); }, 3000);
      return;
    }
    if (my !== sentGen || idx !== chIdx) return;
    applySentences(d);
    if (d.status === "none") {
      try { await api.post(`/api/books/${bookId}/chapters/${idx}/translate`); } catch (e) { toast(e.message, "error"); }
    }
    if (d.status === "none" || d.status === "queued" || d.status === "running") {
      sentTimer = setTimeout(() => { if (my === sentGen) loadSentences(); }, 1000);
    }
  }

  function applySentences(d) {
    const rebuild = !sents || sents.status !== d.status || unitsIdx !== chIdx || !units.length;
    const keep = unitsIdx === chIdx && units[u] ? { p: units[u].p, c: units[u].start } : null;
    sents = { ...d, idx: chIdx };
    if (rebuild) {
      buildUnits();
      u = keep ? unitAt(keep.p, keep.c) : initialUnit();
      paint();
    }
    renderCard();
    if (rebuild && !keep) scrollUnit();
  }

  function buildUnits() {
    units = [];
    unitsIdx = chIdx;
    (sents.paragraphs || []).forEach((sp, pi) => {
      if (!paras[pi]) return;
      const ss = sp.spans || [];
      const groups = sp.segs ? sp.segs : ss.map((_, k) => ({ from: k, to: k, zh: null }));
      for (const g of groups) {
        if (!ss[g.from] || !ss[g.to]) continue;
        const start = ss[g.from][0], end = ss[g.to][1];
        let a = null, b = null;
        for (const k of paraTimed[pi] || []) {
          const s = spans[k];
          if (s.c < start || s.c >= end) continue;
          a = a === null ? s.a : Math.min(a, s.a);
          b = b === null ? s.b : Math.max(b, s.b);
        }
        units.push({ p: pi, start, end, from: g.from, to: g.to, zh: g.zh ?? null, a, b });
      }
    });
  }

  /** Unit containing char c of paragraph p (else the nearest one at or after it). */
  function unitAt(p, c) {
    let best = -1;
    for (let i = 0; i < units.length; i++) {
      const x = units[i];
      if (x.p < p) continue;
      if (x.p > p) return best >= 0 ? best : i;
      if (x.start <= c) best = i;
      if (c < x.end && x.start <= c) return i;
      if (x.start > c) return best >= 0 ? best : i;
    }
    return best >= 0 ? best : units.length - 1;
  }

  function initialUnit() {
    if (!units.length) return -1;
    if (playable && au.currentTime > 0 && spans.length) {
      const s = spans[Math.max(0, find(au.currentTime))];
      return unitAt(s.p, s.c);
    }
    const top = paras.findIndex((p) => p.getBoundingClientRect().bottom > 80);
    return top > 0 ? unitAt(top, 0) : 0;
  }

  /** DOM range over chars [start, end) of paragraph p's own text. */
  function rangeIn(p, start, end) {
    const r = document.createRange();
    let n = 0, started = false;
    for (const t of ownTextNodes(p)) {
      const len = t.length;
      if (!started && start < n + len) { r.setStart(t, start - n); started = true; }
      if (started && end <= n + len) { r.setEnd(t, end - n); return r; }
      n += len;
    }
    return null;
  }

  function paint() {
    const x = units[u] || null;
    paras.forEach((p, i) => p.classList.toggle("focus", !!x && x.p === i));
    if (!canHighlight) return;
    const r = x && paras[x.p] ? rangeIn(paras[x.p], x.start, x.end) : null;
    if (r) CSS.highlights.set("drill", new Highlight(r));
    else CSS.highlights.delete("drill");
  }

  /** Keep the current sentence AND its Chinese (under the paragraph) on screen, sentence first. */
  function scrollUnit() {
    const x = units[u];
    const r = x && paras[x.p] ? rangeIn(paras[x.p], x.start, x.end) : null;
    if (!r) return;
    const rect = r.getBoundingClientRect();
    const zhBottom = zhText.hidden || !zhText.isConnected ? rect.bottom : zhText.getBoundingClientRect().bottom;
    const topLimit = 72, bottomLimit = innerHeight - barH - 16;
    let delta = 0;
    if (rect.top < topLimit) delta = rect.top - Math.max(topLimit + 24, (bottomLimit - topLimit) * 0.3);
    else if (Math.max(rect.bottom, zhBottom) > bottomLimit) {
      delta = Math.max(rect.bottom, zhBottom) - bottomLimit + 24;
      delta = Math.min(delta, rect.top - topLimit - 12); // never push the sentence itself off the top
    }
    if (delta) {
      lastScroll = Date.now();
      scrollBy({ top: delta, behavior: "instant" });
    }
  }

  function renderCard(msg) {
    const x = units[u];
    zhRetry.hidden = true;
    zhToggle.setAttribute("aria-pressed", String(zhOn));
    zhPos.textContent = x ? `${u + 1} / ${units.length}` : "";
    let hint;
    if (msg) hint = msg;
    else if (!sents) hint = "载入中…";
    else if (!x) hint = "本章没有可练的句子";
    else if (x.zh === null) {
      if (sents.status === "error") {
        hint = "翻译失败：" + (sents.error || "未知错误");
        zhRetry.hidden = false;
      } else {
        hint = `翻译中 ${Math.round((sents.progress || 0) * 100)}%`;
      }
    } else {
      hint = (x.to > x.from ? `中文对应 ${x.to - x.from + 1} 句` : "") + (zhOn ? "" : (x.to > x.from ? " · " : "") + "中文已隐藏");
    }
    zhHint.textContent = hint;
    const show = !!(x && x.zh !== null && zhOn && paras[x.p]);
    if (show) {
      if (zhText.previousElementSibling !== paras[x.p] || zhText.parentNode !== box) paras[x.p].after(zhText);
      if (zhBody.textContent !== x.zh) zhBody.textContent = x.zh;
    }
    zhText.hidden = !show;
    renderFav();
  }

  /** Make unit i current. 一句一停 always plays it; 连续 keeps playing if it was playing (paused stays paused). */
  function goUnit(i) {
    if (!units.length) { if (playable) jumpPara(i < u ? -1 : 1); return; }
    if (i >= units.length && ch && ch.next_idx !== null && ch.next_idx !== undefined) {
      loadChapter(ch.next_idx, { autoplay: playable && (stopMode || !au.paused) });
      return;
    }
    if (i < 0 || i >= units.length) { toast(i < 0 ? "已经是本章第一句" : "已经是全书最后一句"); return; }
    const wasPlaying = playable && !au.paused;
    u = i;
    unitEnd = null;
    paint();
    renderCard();
    scrollUnit();
    const x = units[u];
    if (!playable || x.a === null) { if (wasPlaying) au.pause(); return; }
    if (stopMode || wasPlaying) playUnit();
    else seek(x.a);
  }

  /** Play the current unit from its start (一句一停: up to its end). */
  function playUnit(from) {
    const x = units[u];
    if (!playable || !x || x.a === null) return;
    unitEnd = stopMode ? x.b : null;
    seek(from ?? x.a);
    play();
  }

  function drillToggle() {
    if (!playable) return;
    if (!au.paused) { au.pause(); return; }
    const x = units[u];
    if (!stopMode && x && x.a !== null && x.b !== null &&
        (au.currentTime < x.a - 0.05 || au.currentTime > x.b + 0.05)) seek(x.a); // read from where you are
    if (!stopMode || !x || x.a === null) { play(); return; }
    const t = au.currentTime;
    if (t < x.a - 0.05 || t >= x.b - 0.05) playUnit();
    else { unitEnd = x.b; play(); }
  }

  /** 连续: while playing, the current unit follows the audio (called when the highlighted word changes). */
  function followAudio(i) {
    if (stopMode || au.paused) return;
    syncUnit(i);
  }

  /** Make the unit holding timed span i current (no-op if it already is). */
  function syncUnit(i) {
    if (i < 0 || !units.length || !spans[i]) return;
    const s = spans[i], x = units[u];
    if (x && x.p === s.p && x.start <= s.c && s.c < x.end) return;
    const j = unitAt(s.p, s.c);
    if (j < 0 || j === u) return;
    u = j;
    paint();
    renderCard();
    if (pop.hidden && Date.now() - userScroll > USER_SCROLL_HOLD_MS) scrollUnit(); // not while selecting
  }

  function toggleZh() {
    zhOn = !zhOn;
    try { localStorage.setItem(ZH_KEY, zhOn ? "1" : "0"); } catch (e) { /* private mode */ }
    renderCard();
  }
  zhToggle.addEventListener("click", toggleZh);
  zhToggle.addEventListener("mousedown", (e) => e.preventDefault()); // keep Space for playback

  // ---------------------------------------------------------------- 收藏本句: English + Chinese → 生词本
  const favBtn = $("fav");
  const favs = new Map(); // "chapter:para:start" → vocab id of a saved sentence unit
  const favKey = (c, p, s) => `${c}:${p}:${s}`;
  let favBusy = false;

  async function loadFavs() {
    try {
      const all = await api.get("/api/vocab");
      favs.clear();
      for (const v of all) {
        if (v.kind === "sentence" && v.book_id === bookId) favs.set(favKey(v.chapter, v.para, v.sentence_start_char), v.id);
      }
    } catch (e) { /* marks are a nicety */ }
    renderFav();
  }

  /** The star at the end of the Chinese line (F works whether or not the line is showing). */
  function renderFav() {
    const x = units[u];
    const on = !!x && favs.has(favKey(chIdx, x.p, x.start));
    favBtn.setAttribute("aria-pressed", String(on));
    favBtn.title = on ? "已收藏本句（再点取消，F）" : "收藏本句：英文 + 中文进生词本（F）";
  }

  /** Save the current unit (English + its Chinese, with its audio clip); again → remove it. */
  async function toggleFav() {
    const x = units[u];
    if (!x || favBusy) return;
    const key = favKey(chIdx, x.p, x.start);
    favBusy = true;
    try {
      if (favs.has(key)) {
        await api.del(`/api/vocab/${favs.get(key)}`);
        favs.delete(key);
        toast("已取消收藏");
      } else {
        const rec = await api.post("/api/vocab", {
          book_id: bookId, chapter: chIdx, para: x.p, start_char: x.start, end_char: x.end, whole: true, zh: x.zh,
        });
        favs.set(key, rec.id);
        toast("已收藏这句" + (x.zh ? "（含中文）" : ""));
      }
    } catch (e) {
      toast(e.message, "error");
    } finally {
      favBusy = false;
      renderFav();
    }
  }
  favBtn.addEventListener("click", toggleFav);
  favBtn.addEventListener("mousedown", (e) => e.preventDefault());

  /** Drill-mode keys; true when handled. */
  function drillKey(e, tag, barRange) {
    if (e.code === "Space") {
      if (tag === "BUTTON") return true;
      e.preventDefault();
      if (!e.repeat) drillToggle();
    } else if (e.code === "ArrowUp" || e.code === "KeyR") {
      e.preventDefault();
      if (!e.repeat) playUnit();
    } else if (e.code === "KeyF") {
      e.preventDefault();
      if (!e.repeat) toggleFav();
    } else if (e.code === "ArrowDown" || (e.code === "Enter" && tag !== "BUTTON" && tag !== "A")) {
      e.preventDefault();
      if (!e.repeat) toggleZh();
    } else if ((e.code === "ArrowRight" || e.code === "ArrowLeft") && !barRange) {
      e.preventDefault();
      goUnit(u + (e.code === "ArrowRight" ? 1 : -1));
    } else {
      return false;
    }
    return true;
  }

  function drillClick(e) {
    if (e.detail >= 2) { undoClickSeek(); return; }
    const sel = getSelection();
    if (sel && !sel.isCollapsed) return;
    const p = e.target.closest("p[data-p]");
    if (!p) return;
    const el = e.target.closest("w");
    const word = el && playable ? timedNear(Number(el.dataset.i)) : null;
    let c = null;
    if (el && el.dataset.c !== undefined) c = Number(el.dataset.c);
    else if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (r && p.contains(r.startContainer)) c = ownOffset(p, r.startContainer, r.startOffset);
    }
    const i = c === null || !units.length ? -1 : unitAt(Number(p.dataset.p), c);
    if (i >= 0) { u = i; paint(); renderCard(); }
    if (!playable) return;
    const x = units[u];
    const from = word ? word.a : i >= 0 && x ? x.a : null;
    if (from === null || from === undefined) return;
    clickSeek = { time: au.currentTime, paused: au.paused, at: performance.now() };
    if (i >= 0) playUnit(from);
    else { unitEnd = null; seek(from); play(); }
  }

  addEventListener("resize", () => renderCard());

  // ---------------------------------------------------------------- start
  async function init() {
    history.scrollRestoration = "manual";
    setRate(storedRate(), false);
    zhOn = storedZh();
    syncDock();
    setStopMode(storedStop(), false);
    if (!/^[0-9a-f]{32}$/.test(bookId) || !Number.isInteger(chIdx)) { showMissing(); return; }
    $("back").href = `book.html?id=${bookId}`;
    try {
      const book = await api.get(`/api/books/${bookId}`);
      bookTitle = book.title;
      resume = book.progress;
      $("book-name").textContent = book.title;
    } catch (e) {
      if (e.status === 404) { showMissing(); return; }
      toast(e.message, "error");
    }
    loadFavs();
    await loadChapter(chIdx);
  }

  init();
})();
