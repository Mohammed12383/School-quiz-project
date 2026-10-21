/* ضفاف العربية — shared browser helpers (no build step, no globals except Difaf). */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** Safe element builder: strings become text nodes, never HTML. */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, String(v));
      }
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function render(el, ...children) { clear(el); return append(el, children); }

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
    json(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } },
  };

  const fmt = {
    int: (n) => Math.round(Number(n) || 0).toLocaleString("en-US"),
    dec: (n, d = 1) => (Number(n) || 0).toFixed(d),
    pct: (n) => Math.round((Number(n) || 0) * 100) + "%",
    sec: (n) => (n == null ? "—" : (Number(n) || 0).toFixed(1) + " ث"),
    date: (ts) => { try { return new Date(ts * 1000).toLocaleString("ar-OM-u-nu-latn", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return ""; } },
  };

  /* ---------- toasts */
  let toastBox = null;
  function toast(text, kind, ms) {
    if (!toastBox) { toastBox = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.appendChild(toastBox); }
    const t = h("div", { class: "toast " + (kind || "") , text: text });
    toastBox.appendChild(t);
    setTimeout(() => t.remove(), ms || 3600);
  }

  /* ---------- modal confirm */
  function confirmBox(message, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      let close = (v) => { back.remove(); resolve(v); };
      const input = opts.typeToConfirm ? h("input", { class: "input", placeholder: opts.typeToConfirm, dir: "ltr" }) : null;
      const okBtn = h("button", { class: "btn " + (opts.danger ? "btn-bad" : ""), text: opts.ok || "تأكيد", onclick: () => {
        if (input && input.value.trim() !== opts.typeToConfirm) { input.classList.add("bad"); input.focus(); return; }
        close(true);
      } });
      const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(false); } },
        h("div", { class: "card modal stack", role: "dialog", "aria-modal": "true" },
          h("h3", { text: opts.title || "تأكيد" }),
          h("p", { text: message, style: { whiteSpace: "pre-line" } }),
          input ? h("p", { class: "muted", text: "للتأكيد اكتب: " + opts.typeToConfirm }) : null,
          input,
          h("div", { class: "row", style: { justifyContent: "flex-end" } },
            h("button", { class: "btn btn-light", text: "إلغاء", onclick: () => close(false) }), okBtn)));
      document.body.appendChild(back);
      (input || okBtn).focus();
      const onKey = (e) => { if (e.key === "Escape") close(false); };
      document.addEventListener("keydown", onKey);
      const done = close;
      close = (v) => { document.removeEventListener("keydown", onKey); done(v); };
    });
  }

  /* ---------- sound (synthesised, works offline) */
  const Sound = {
    ctx: null,
    muted: store.get("difaf:muted") === "1",
    unlock() {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!this.ctx) this.ctx = new AC();
      if (this.ctx.state === "suspended") this.ctx.resume();
      return this.ctx;
    },
    setMuted(m) { this.muted = m; store.set("difaf:muted", m ? "1" : "0"); },
    tone(freq, dur, o) {
      o = o || {};
      if (this.muted) return;
      const ctx = this.unlock();
      if (!ctx) return;
      const t0 = ctx.currentTime + (o.at || 0);
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = o.type || "sine";
      osc.frequency.setValueAtTime(freq, t0);
      if (o.slide) osc.frequency.exponentialRampToValueAtTime(o.slide, t0 + dur);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(o.vol || 0.18, t0 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(g).connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.05);
    },
    tick() { this.tone(1200, 0.05, { type: "square", vol: 0.035 }); },
    count() { this.tone(660, 0.18, { type: "triangle", vol: 0.2 }); },
    go() { [523, 659, 784, 1047].forEach((f, i) => this.tone(f, 0.25, { type: "triangle", at: i * 0.07, vol: 0.18 })); },
    ok() { [660, 880, 1320].forEach((f, i) => this.tone(f, 0.22, { type: "sine", at: i * 0.09, vol: 0.2 })); },
    bad() { this.tone(220, 0.45, { type: "sawtooth", slide: 110, vol: 0.08 }); },
    reveal() { [392, 523, 659].forEach((f, i) => this.tone(f, 0.3, { type: "triangle", at: i * 0.1, vol: 0.16 })); },
    fanfare() {
      const notes = [[523, 0], [523, .15], [523, .3], [659, .45], [784, .75], [659, 1.0], [784, 1.15], [1047, 1.4]];
      notes.forEach(([f, at]) => { this.tone(f, 0.35, { type: "triangle", at, vol: 0.22 }); this.tone(f / 2, 0.35, { type: "sine", at, vol: 0.12 }); });
    },
  };

  /* ---------- confetti in the logo colours */
  function confetti(opts) {
    opts = opts || {};
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const canvas = h("canvas", { class: "confetti", "aria-hidden": "true" });
    document.body.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const W = canvas.width = innerWidth * dpr;
    const H = canvas.height = innerHeight * dpr;
    const colors = ["#b38841", "#ddc08b", "#192552", "#304485", "#ffffff", "#0d7471", "#c9a25f"];
    const n = opts.count || 180;
    const parts = [];
    for (let i = 0; i < n; i++) {
      const fromSides = opts.sides !== false && i % 2 === 0;
      parts.push({
        x: fromSides ? (i % 4 === 0 ? 0 : W) : Math.random() * W,
        y: fromSides ? H * (0.55 + Math.random() * 0.3) : -20 * dpr - Math.random() * H * 0.4,
        vx: fromSides ? (i % 4 === 0 ? 1 : -1) * (6 + Math.random() * 10) * dpr : (Math.random() - 0.5) * 3 * dpr,
        vy: fromSides ? -(12 + Math.random() * 12) * dpr : (2 + Math.random() * 3) * dpr,
        w: (6 + Math.random() * 8) * dpr, h: (8 + Math.random() * 10) * dpr,
        r: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.3,
        c: colors[i % colors.length], round: Math.random() < 0.25,
      });
    }
    const end = performance.now() + (opts.duration || 4200);
    function frame(t) {
      ctx.clearRect(0, 0, W, H);
      for (const p of parts) {
        p.vy += 0.32 * dpr; p.vx *= 0.99; p.x += p.vx; p.y += p.vy; p.r += p.vr;
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.c;
        if (p.round) { ctx.beginPath(); ctx.arc(0, 0, p.w / 2, 0, Math.PI * 2); ctx.fill(); }
        else ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.cos(p.r * 2)) + 2);
        ctx.restore();
      }
      if (t < end) requestAnimationFrame(frame); else canvas.remove();
    }
    requestAnimationFrame(frame);
  }

  /* ---------- countdown driven by a server-provided remaining time */
  function countdown(remainingMs, onFrame, onDone) {
    const end = performance.now() + Math.max(0, remainingMs || 0);
    let raf = 0, stopped = false;
    function step() {
      if (stopped) return;
      const left = Math.max(0, end - performance.now());
      onFrame(left);
      if (left <= 0) { if (onDone) onDone(); return; }
      raf = requestAnimationFrame(step);
    }
    step();
    return () => { stopped = true; cancelAnimationFrame(raf); };
  }

  /* ---------- Socket.IO with a visible reconnecting badge */
  function connect(onConnect) {
    const socket = window.io({ reconnectionDelay: 500, reconnectionDelayMax: 3000, timeout: 8000 });
    let badge = null;
    let shown = false;
    const showBadge = () => {
      if (!badge) { badge = h("div", { class: "conn", text: "جارٍ الاتصال بالخادم…" }); }
      if (!shown) { document.body.appendChild(badge); shown = true; }
    };
    const hideBadge = () => { if (badge && shown) { badge.remove(); shown = false; } };
    let timer = setTimeout(showBadge, 1500);
    socket.on("connect", () => { clearTimeout(timer); hideBadge(); if (onConnect) onConnect(socket); });
    socket.on("disconnect", () => { timer = setTimeout(showBadge, 800); });
    socket.on("connect_error", () => showBadge());
    socket.on("notice", (n) => n && toast(n.text, n.kind));
    socket.request = (event, data) => new Promise((resolve) => {
      if (!socket.connected) { resolve({ ok: false, error: "لا يوجد اتصال بالخادم حاليًا" }); return; }
      socket.timeout(10000).emit(event, data || {}, (err, res) => {
        resolve(err ? { ok: false, error: "انتهت مهلة الاتصال، حاول مجددًا" } : (res || { ok: false, error: "لا يوجد رد" }));
      });
    });
    return socket;
  }

  /* ---------- join link: LAN address when the page is opened on localhost */
  async function joinUrl() {
    const local = ["localhost", "127.0.0.1", "::1", "[::1]"].includes(location.hostname);
    if (!local) return location.origin + "/play";
    try {
      const r = await fetch("/api/info", { cache: "no-store" });
      const info = await r.json();
      if (info.lanUrls && info.lanUrls.length) return info.lanUrls[0] + "/play";
    } catch (e) { /* offline */ }
    return location.origin + "/play";
  }
  const qrSrc = (url) => "/api/qr?u=" + encodeURIComponent(url);

  const GRADE_NAMES = { 9: "التاسع", 10: "العاشر", 11: "الحادي عشر", 12: "الثاني عشر" };

  function plural(n, one, two, few, many) {
    n = Number(n) || 0;
    if (n === 1) return one;
    if (n === 2) return two;
    if (n >= 3 && n <= 10) return n + " " + few;
    return n + " " + many;
  }

  window.Difaf = { $, $$, h, append, clear, render, store, fmt, toast, confirmBox, Sound, confetti, countdown, connect, joinUrl, qrSrc, GRADE_NAMES, plural };
})();
