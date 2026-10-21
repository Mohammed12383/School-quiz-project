/* Projector screen. Server pushes `display:state`; this file decides what to show. */
(function () {
  "use strict";
  const D = window.Difaf;
  const { $, h, render, fmt, Sound } = D;
  const stage = $("#stage");
  const overlay = $("#overlay");
  const SVGNS = "http://www.w3.org/2000/svg";
  const LETTERS = ["أ", "ب", "ج", "د"];

  let st = null;
  let joinUrl = location.origin + "/play";
  let cur = null;            // { name, key, refs, stop: [] }
  let opening = false;
  let localAuto = false;
  let lastServerMode = null;

  /* ---------------------------------------------------------------- socket */
  const socket = D.connect((s) => s.emit("display:join"));
  socket.on("display:state", (s) => {
    if (lastServerMode !== null && s.mode !== lastServerMode) localAuto = false;
    lastServerMode = s.mode;
    st = s;
    paint();
  });
  socket.on("display:opening", () => runOpening());
  socket.on("display:champion", (c) => showChampion(c));

  D.joinUrl().then((u) => {
    joinUrl = u;
    $("#qrSmall").src = D.qrSrc(u);
    $("#joinUrlSmall").textContent = u.replace(/^https?:\/\//, "");
    if (cur && ["idle", "join", "lobby"].includes(cur.name)) { cur = null; paint(); }
  });

  /* -------------------------------------------------------------- controls */
  const controls = $("#controls");
  let fadeTimer = 0;
  function wake() {
    controls.classList.remove("faded");
    document.body.classList.remove("hide-cursor");
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => { controls.classList.add("faded"); document.body.classList.add("hide-cursor"); }, 3500);
  }
  ["mousemove", "mousedown", "keydown", "touchstart"].forEach((e) => document.addEventListener(e, wake, { passive: true }));
  wake();

  function soundLabel() { $("#btnSound").textContent = Sound.muted ? "الصوت: مكتوم" : "الصوت: مفعّل"; }
  soundLabel();
  $("#btnSound").addEventListener("click", () => { Sound.setMuted(!Sound.muted); Sound.unlock(); soundLabel(); if (!Sound.muted) Sound.count(); });
  $("#btnFull").addEventListener("click", () => {
    Sound.unlock();
    if (document.fullscreenElement) document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
  });
  document.addEventListener("fullscreenchange", () => { $("#btnFull").textContent = document.fullscreenElement ? "إنهاء ملء الشاشة" : "ملء الشاشة"; });
  $("#btnOpening").addEventListener("click", () => runOpening());
  document.addEventListener("click", () => Sound.unlock(), { once: true });

  /* ---------------------------------------------------------------- helpers */
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function svgEl(tag, attrs, ...kids) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    kids.forEach((k) => e.appendChild(k));
    return e;
  }
  function cup() {
    return svgEl("svg", { viewBox: "0 0 64 64", class: "cup", "aria-hidden": "true" },
      svgEl("path", { d: "M18 6h28v16a14 14 0 0 1-28 0z", fill: "#ddc08b" }),
      svgEl("path", { d: "M18 10H8v5a11 11 0 0 0 11 11M46 10h10v5a11 11 0 0 1-11 11", fill: "none", stroke: "#ddc08b", "stroke-width": 4 }),
      svgEl("path", { d: "M27 36h10v10H27zM18 48h28v9H18z", fill: "#b38841" }));
  }
  function teamBadge(name) { return h("span", { class: "team-badge num", text: name || "؟" }); }
  function joinCard(big) {
    return h("div", { class: "join-card" },
      h("h2", { text: "انضم من هاتفك" }),
      h("img", { class: "qr", src: D.qrSrc(joinUrl), alt: "رمز QR للانضمام", width: 300, height: 300 }),
      h("div", { class: "url", text: joinUrl.replace(/^https?:\/\//, "") }),
      big ? h("ol", { class: "join-steps" },
        h("li", { "data-n": "1", text: "اتصل بشبكة المدرسة (Wi‑Fi)" }),
        h("li", { "data-n": "2", text: "امسح الرمز بكاميرا هاتفك" }),
        h("li", { "data-n": "3", text: "اختر صفك وفصلك واكتب اسمك" })) : null);
  }
  function ring() {
    const bar = svgEl("circle", { class: "bar", cx: 50, cy: 50, r: 45 });
    const span = h("span", { class: "num" });
    const wrap = h("div", { class: "ring" }, svgEl("svg", { viewBox: "0 0 100 100" }, svgEl("circle", { class: "track", cx: 50, cy: 50, r: 45 }), bar), span);
    return { el: wrap, set(left, total) {
      const f = total ? Math.max(0, Math.min(1, left / total)) : 0;
      bar.style.strokeDashoffset = String(283 * (1 - f));
      span.textContent = Math.ceil(left / 1000);
      wrap.classList.toggle("low", left < 5000);
    } };
  }

  /* -------------------------------------------------------------- deciding */
  function decide(s) {
    const L = s.live, t = s.tournament;
    let mode = s.mode;
    if (localAuto && mode === "idle") mode = "auto";
    if (mode === "idle") return ["idle", ""];
    if (mode === "join") return L && L.phase === "lobby" ? ["lobby", L.matchId] : ["join", ""];
    if (mode === "leaderboard") return ["leaders", ""];
    if (mode === "bracket") return t ? [t.type === "separate" ? "contests" : "bracket", t.id] : ["idle", ""];
    if (L) {
      if (L.phase === "lobby") return ["lobby", L.matchId];
      if (L.phase === "countdown") return ["countdown", L.matchId + ":" + L.qIndex];
      if (L.phase === "question" || L.phase === "reveal") return ["question", L.matchId + ":" + L.qIndex];
      if (L.phase === "done") return ["results", L.matchId];
    }
    if (t && t.type === "separate") return ["contests", t.id];
    if (t) return ["bracket", t.id];
    return ["idle", ""];
  }

  function paint() {
    if (!st) return;
    const [name, key] = decide(st);
    const L = st.live, t = st.tournament;
    render($("#dTitle"),
      t ? h("b", { text: t.name }) : "مسابقات اللغة العربية بين الفصول",
      L && L.phase !== "done" && ["lobby", "countdown", "question"].includes(name) ? " — " + L.roundName : "");
    $("#dJoin").classList.toggle("hidden", ["idle", "join", "lobby"].includes(name));
    if (cur && cur.name === name && cur.key === key) {
      if (SCREENS[name].update) SCREENS[name].update(st, cur.refs);
      return;
    }
    if (cur) cur.stop.forEach((f) => f());
    const refs = { stop: [] };
    const node = SCREENS[name].mount(st, refs);
    render(stage, node);
    cur = { name, key, refs, stop: refs.stop };
    if (SCREENS[name].after) SCREENS[name].after(st, refs);
  }

  /* ---------------------------------------------------------------- screens */
  const SCREENS = {};

  SCREENS.idle = {
    mount() {
      return h("section", { class: "scr" }, h("div", { class: "idle" },
        h("div", { class: "idle-brand" },
          h("img", { src: "/static/img/logo-mark-light.png", alt: "شعار المدرسة" }),
          h("h1", { text: "ضفاف العربية" }),
          h("p", { text: "مسابقات اللغة العربية بين الفصول" }),
          h("div", { class: "idle-verse" }, "أنا البحرُ في أحشائه الدرُّ كامنٌ", h("br"), "فهل سألوا الغوّاصَ عن صدَفاتي", h("small", { text: "حافظ إبراهيم" }))),
        joinCard(true)));
    },
  };
  SCREENS.join = SCREENS.idle;

  SCREENS.lobby = {
    mount(s, refs) {
      const L = s.live;
      const side = (sd) => {
        const names = h("div", { class: "names" });
        const count = h("b", { class: "num" });
        refs[sd.side] = { names, count, last: "" };
        return h("div", { class: "lobby-side side-" + sd.side },
          h("header", {}, teamBadge(sd.name), h("span", { class: "lobby-count" }, "انضم ", count, " طالبًا")),
          names);
      };
      const node = h("section", { class: "scr" }, h("div", { class: "lobby" },
        side(L.sides[0]),
        h("div", { class: "lobby-mid" },
          h("span", { class: "round", text: L.roundName }),
          h("span", { class: "vs-big", text: "ضد" }),
          joinCard(false),
          h("span", { class: "waiting-note dots", text: "بانتظار بدء المباراة" })),
        side(L.sides[1])));
      this.update(s, refs);
      return node;
    },
    update(s, refs) {
      for (const sd of s.live.sides) {
        const r = refs[sd.side];
        if (!r) continue;
        r.count.textContent = sd.online;
        const sig = (sd.names || []).join("|");
        if (sig !== r.last) {
          r.last = sig;
          render(r.names, (sd.names || []).map((n) => h("span", { text: n })));
        }
      }
    },
  };

  SCREENS.countdown = {
    mount(s, refs) {
      const L = s.live;
      const num = h("div", { class: "count-num num", text: "3" });
      let shown = null;
      refs.stop.push(D.countdown(L.remainingMs, (left) => {
        const v = Math.max(1, Math.ceil(left / 1000));
        if (v !== shown) { shown = v; num.textContent = v; num.style.animation = "none"; void num.offsetWidth; num.style.animation = ""; Sound.count(); }
      }));
      return h("section", { class: "scr" }, h("div", { class: "count-scr" },
        h("div", { class: "label", text: "السؤال " + (L.qIndex + 1) + " من " + L.qTotal }),
        h("div", { class: "teams" }, h("span", { class: "num", text: L.sides[0].name }), "  ضد  ", h("span", { class: "num", text: L.sides[1].name })),
        num));
    },
  };

  SCREENS.question = {
    mount(s, refs) {
      const L = s.live;
      refs.phase = null;
      refs.panels = {};
      const phaseLabel = h("span", { class: "phase" });
      refs.phaseLabel = phaseLabel;
      const panels = L.sides.map((sd) => {
        const q = sd.question || { text: "", options: [] };
        const rg = ring();
        const answered = h("b", { class: "num" });
        const expected = h("span", { class: "num" });
        const opts = q.options.map((text, i) => {
          const fill = h("i", { class: "fill" });
          const cnt = h("span", { class: "cnt num" });
          const el = h("div", { class: "dopt" }, fill, h("span", { class: "letter", text: LETTERS[i] || i + 1 }), h("span", { class: "t", text }), cnt);
          return { el, fill, cnt };
        });
        const box = h("div", { class: "dopts" }, opts.map((o) => o.el));
        refs.panels[sd.side] = { rg, answered, expected, opts, box, q, done: false };
        return h("div", { class: "panel side-" + sd.side },
          h("div", { class: "panel-head" }, teamBadge(sd.name), rg.el,
            h("span", { class: "answered" }, "أجاب ", answered, " من ", expected)),
          h("p", { class: "qtext" + (q.text.length > 110 ? " long" : ""), text: q.text }),
          box);
      });
      refs.strip = h("div", { class: "strip", hidden: true });
      const node = h("section", { class: "scr" },
        h("div", { class: "q-head" }, h("span", { class: "qnum", text: "السؤال " + (L.qIndex + 1) + " من " + L.qTotal }), phaseLabel),
        h("div", { class: "split" }, panels),
        refs.strip);
      this.update(s, refs);
      return node;
    },
    update(s, refs) {
      const L = s.live;
      for (const sd of L.sides) {
        const p = refs.panels[sd.side];
        if (!p) continue;
        p.answered.textContent = sd.answered || 0;
        p.expected.textContent = sd.expected || 0;
      }
      if (L.phase === refs.phase) return;
      refs.phase = L.phase;
      if (L.phase === "question") {
        refs.phaseLabel.textContent = "أجيبوا الآن!";
        Sound.go();
        for (const sd of L.sides) {
          const p = refs.panels[sd.side];
          const q = sd.question;
          if (!p || !q) continue;
          let lastSec = null;
          refs.stop.push(D.countdown(q.remainingMs, (left) => {
            p.rg.set(left, q.durationMs);
            const sec = Math.ceil(left / 1000);
            if (sd === L.sides[0] && sec !== lastSec) { lastSec = sec; if (sec <= 5 && sec > 0) Sound.tick(); }
          }));
        }
      } else if (L.phase === "reveal") {
        refs.stop.forEach((f) => f());
        refs.stop.length = 0;
        refs.phaseLabel.textContent = "الإجابة الصحيحة";
        Sound.reveal();
        for (const sd of L.sides) {
          const p = refs.panels[sd.side];
          if (!p) continue;
          p.rg.set(0, 1);
          p.box.classList.add("revealed");
          const counts = sd.counts || [];
          const total = counts.reduce((a, b) => a + b, 0) || 1;
          p.opts.forEach((o, i) => {
            o.el.classList.toggle("correct", i === sd.correctIndex);
            o.cnt.textContent = counts[i] || 0;
            requestAnimationFrame(() => { o.fill.style.width = Math.round(((counts[i] || 0) / total) * 100) + "%"; });
          });
        }
        render(refs.strip,
          h("div", { class: "scorebox" },
            h("div", { class: "s" }, h("small", { text: "متوسط " + L.sides[0].name }), h("b", { class: "num", text: fmt.int(L.sides[0].avg) })),
            h("span", { class: "vs", text: "ضد" }),
            h("div", { class: "s" }, h("small", { text: "متوسط " + L.sides[1].name }), h("b", { class: "num", text: fmt.int(L.sides[1].avg) }))),
          h("div", { class: "toplist" }, (L.top || []).map((p, i) => h("div", {},
            h("span", { class: "n", text: (i + 1) + ". " + p.name }),
            h("small", { class: "num", text: p.className }),
            h("span", { class: "p num", text: fmt.int(p.points) })))));
        refs.strip.hidden = false;
      }
    },
  };

  SCREENS.results = {
    mount(s) {
      const L = s.live;
      const R = L.result || {};
      const how = { score: "بفارق متوسط النقاط", accuracy: "بفارق الدقة (تساوى متوسط النقاط)", speed: "بفارق سرعة الإجابة", forfeit: "لعدم حضور طلاب الفريق المنافس", manual: "بقرار لجنة المسابقة" }[R.decidedBy] || "";
      if (R.winner) { Sound.fanfare(); D.confetti({ count: 240 }); }
      const card = (sd, key) => {
        const r = R[key] || {};
        const win = R.winnerId && R.winnerId === sd.classId;
        return h("div", { class: "cmp" + (win ? " win" : "") },
          teamBadge(sd.name),
          h("div", { class: "avg num" }, h("small", { text: "متوسط النقاط" }), fmt.int(r.avg)),
          h("div", { class: "meta" },
            h("span", {}, "الدقة ", h("b", { class: "num", text: fmt.pct(r.accuracy) })),
            h("span", {}, "متوسط الزمن ", h("b", { class: "num", text: fmt.dec(r.avgTime || 0, 1) }), " ث"),
            h("span", {}, "الطلاب ", h("b", { class: "num", text: r.players || 0 }))));
      };
      return h("section", { class: "scr" }, h("div", { class: "results" },
        h("div", { class: "winner-banner" },
          h("small", { text: R.winner ? (L.isFinal ? "بطل الدوري" : "الفائز في " + L.roundName) : "انتهت المباراة" }),
          h("h1", { class: R.winner ? "num" : "", text: R.winner || "تعادل!" }),
          h("p", { text: R.winner ? how : "النتيجة متساوية تمامًا — بانتظار قرار لجنة المسابقة" })),
        h("div", { class: "compare" }, card(L.sides[0], "A"), card(L.sides[1], "B")),
        h("div", { class: "toplist" }, (L.top || []).map((p, i) => h("div", {},
          h("span", { class: "n", text: (i + 1) + ". " + p.name }),
          h("small", { class: "num", text: p.className }),
          h("span", { class: "p num", text: fmt.int(p.points) }))))));
    },
  };

  SCREENS.bracket = {
    sig(s) { return JSON.stringify((s.tournament && s.tournament.rounds || []).map((r) => r.map((m) => [m.A.id, m.B.id, m.status, m.winner, m.isLive]))); },
    mount(s, refs) {
      const t = s.tournament;
      refs.sig = this.sig(s);
      refs.cards = {};
      const cols = (t.rounds || []).map((r) => h("div", { class: "b-round" },
        h("h3", { text: r[0] ? r[0].roundName : "" }),
        h("div", { class: "b-col" }, r.map((m) => {
          const team = (x, other) => {
            const cls = !x.id ? "tbd" : m.winner ? (m.winner === x.id ? "win" : "lose") : "";
            return h("div", { class: "b-team " + cls },
              h("span", { class: "num", text: x.name || (m.bye && other.id ? "تأهل مباشر" : "بانتظار الفائز") }),
              x.avg != null ? h("span", { class: "sc num", text: fmt.int(x.avg) }) : null);
          };
          const card = h("div", { class: "b-match" + (m.isLive ? " live" : "") + (m.bye ? " bye" : "") },
            team(m.A, m.B), m.bye ? null : team(m.B, m.A));
          refs.cards[m.id] = { el: card, next: m.nextMatchId };
          return card;
        }))));
      if (t.champion) cols.push(h("div", { class: "b-round champion-col" }, cup(), h("small", { text: "البطل" }), h("b", { class: "num", text: t.champion.name })));
      refs.lines = svgEl("svg", { class: "b-lines" });
      refs.wrap = h("div", { class: "bracket-wrap" }, h("div", { class: "bracket" }, cols), refs.lines);
      const onResize = () => this.draw(refs);
      window.addEventListener("resize", onResize);
      refs.stop.push(() => window.removeEventListener("resize", onResize));
      return h("section", { class: "scr" }, refs.wrap);
    },
    after(s, refs) { requestAnimationFrame(() => this.draw(refs)); },
    draw(refs) {
      const lines = refs.lines;
      while (lines.firstChild) lines.removeChild(lines.firstChild);
      const base = refs.wrap.getBoundingClientRect();
      for (const c of Object.values(refs.cards)) {
        const n = c.next && refs.cards[c.next];
        if (!n) continue;
        const a = c.el.getBoundingClientRect(), b = n.el.getBoundingClientRect();
        const rtl = b.left < a.left;
        const x1 = (rtl ? a.left : a.right) - base.left, y1 = a.top + a.height / 2 - base.top;
        const x2 = (rtl ? b.right : b.left) - base.left, y2 = b.top + b.height / 2 - base.top;
        const mx = (x1 + x2) / 2;
        lines.appendChild(svgEl("path", { d: `M${x1} ${y1} H${mx} V${y2} H${x2}` }));
      }
    },
    update(s, refs) { if (this.sig(s) !== refs.sig) { cur = null; paint(); } },
  };

  SCREENS.leaders = {
    sig(s) { return JSON.stringify([s.leaders, s.tournament && s.tournament.contests]); },
    mount(s, refs) {
      refs.sig = this.sig(s);
      const t = s.tournament;
      let left;
      if (t && t.type === "separate") {
        left = h("div", { class: "board" }, h("h2", { text: "ترتيب الفصول (من 5)" }),
          (t.contests || []).filter((r) => r.status !== "idle").map((r, i) => h("div", { class: "lrow" },
            h("span", { class: "rk num", text: r.rank || "—" }),
            h("span", {}, h("span", { class: "num", text: r.name }), h("small", {}, r.status === "finished" ? ["انتهت · الدقة ", h("span", { class: "num", text: fmt.pct(r.accuracy) })] : "جارية")),
            h("span", { class: "v num", text: fmt.dec(r.status === "finished" ? r.avg : r.liveAvg, 2) }))));
      } else {
        const rows = (s.leaders && s.leaders.classes) || [];
        left = h("div", { class: "board" }, h("h2", { text: "ترتيب الفصول" }),
          rows.length ? rows.slice(0, 10).map((r, i) => h("div", { class: "lrow" + (r.out ? " out" : "") },
            h("span", { class: "rk num", text: i + 1 }),
            h("span", {}, h("span", { class: "num", text: r.name }), h("small", { text: "فاز " + r.wins + " من " + r.played + (r.out ? " · خرج من المنافسة" : "") })),
            h("span", { class: "v num", text: fmt.int(r.avg) }))) : h("p", { class: "muted", text: "لم تُلعب مباريات بعد" }));
      }
      const studs = (s.leaders && s.leaders.students) || [];
      const right = h("div", { class: "board" }, h("h2", { text: "أفضل الطلاب" }),
        studs.length ? studs.map((p, i) => h("div", { class: "lrow" },
          h("span", { class: "rk num", text: i + 1 }),
          h("span", {}, p.name, h("small", { class: "num", text: p.className })),
          h("span", { class: "v" }, h("span", { class: "num", text: p.correct }), h("small", { text: "إجابة صحيحة" })))) : h("p", { class: "muted", text: "لا توجد نتائج بعد" }));
      return h("section", { class: "scr" }, h("div", { class: "leaders" }, left, right));
    },
    update(s, refs) { if (this.sig(s) !== refs.sig) { cur = null; paint(); } },
  };

  SCREENS.contests = {
    mount(s, refs) {
      refs.grid = h("div", { class: "contests" });
      refs.cards = {};
      this.update(s, refs);
      return h("section", { class: "scr" }, refs.grid);
    },
    update(s, refs) {
      const rows = (s.tournament && s.tournament.contests) || [];
      const order = rows.map((r) => r.classId).join("|");
      if (order !== refs.order) {
        refs.order = order;
        render(refs.grid);
        refs.cards = {};
        for (const r of rows) {
          const c = {
            rank: h("span", { class: "rank num" }), st: h("span", { class: "st" }), bar: h("i"),
            avg: h("b", { class: "num" }), acc: h("b", { class: "num" }), ppl: h("b", { class: "num" }),
          };
          c.el = h("div", { class: "ccard" },
            h("header", {}, teamBadge(r.name), c.st, c.rank),
            h("div", { class: "pbar" }, c.bar),
            h("div", { class: "nums" },
              h("div", {}, c.avg, h("small", { text: "المتوسط من 5" })),
              h("div", {}, c.acc, h("small", { text: "الدقة" })),
              h("div", {}, c.ppl, h("small", { text: "أنهوا من المشاركين" }))));
          refs.cards[r.classId] = c;
          refs.grid.appendChild(c.el);
        }
      }
      for (const r of rows) {
        const c = refs.cards[r.classId];
        if (!c) continue;
        c.el.className = "ccard " + r.status;
        c.rank.hidden = !r.rank;
        c.rank.textContent = r.rank || "";
        c.st.textContent = { idle: "لم تبدأ", lobby: "بانتظار البدء · متصل " + r.online, running: "جارية الآن", finished: "انتهت" }[r.status] || "";
        c.bar.style.width = Math.round((r.status === "finished" ? 1 : r.progress) * 100) + "%";
        const idle = r.status === "idle";
        c.avg.textContent = idle ? "—" : fmt.dec(r.status === "finished" ? r.avg : r.liveAvg, 2);
        c.acc.textContent = idle ? "—" : fmt.pct(r.accuracy);
        c.ppl.textContent = idle ? "—" : r.finished + "/" + r.players;
      }
    },
  };

  /* --------------------------------------------------------------- overlays */
  async function runOpening() {
    if (opening) return;
    opening = true;
    Sound.unlock();
    overlay.hidden = false;
    for (const n of [5, 4, 3, 2, 1]) {
      render(overlay, h("div", { class: "ring-pulse" }), h("div", { class: "big num", text: n }));
      Sound.count();
      await sleep(1000);
    }
    render(overlay, h("div", { class: "rays" }), h("div", { class: "launch" },
      h("img", { src: "/static/img/logo-mark-light.png", alt: "" }),
      h("h1", { text: "ضفاف العربية" }),
      h("p", { text: "انطلقت المنافسات!" })));
    Sound.fanfare();
    D.confetti({ count: 260, duration: 5000 });
    setTimeout(() => D.confetti({ count: 160, sides: false }), 1400);
    await sleep(6000);
    overlay.hidden = true;
    render(overlay);
    opening = false;
    localAuto = true;
    cur = null;
    paint();
  }

  let champTimer = 0;
  function showChampion(c) {
    if (!c) return;
    clearTimeout(champTimer);
    overlay.hidden = false;
    render(overlay, h("div", { class: "rays" }), h("div", { class: "launch champ" },
      cup(),
      h("small", { text: "بطل " + (c.tournament || "الدوري") }),
      h("h1", { class: "num", text: c.name }),
      h("p", { text: "ألف مبروك! فخورون بكم" })));
    Sound.fanfare();
    D.confetti({ count: 300, duration: 6000 });
    setTimeout(() => D.confetti({ count: 200, sides: false, duration: 5000 }), 2000);
    const close = () => { overlay.hidden = true; render(overlay); overlay.removeEventListener("click", close); };
    overlay.addEventListener("click", close);
    champTimer = setTimeout(close, 15000);
  }
})();
