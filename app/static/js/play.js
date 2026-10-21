/* Student page: registration, live questions and feedback. */
(function () {
  "use strict";
  const D = window.Difaf;
  const { $, h, render, store, toast, fmt } = D;
  const app = $("#app");
  const KEY = "difaf:player";
  const SVGNS = "http://www.w3.org/2000/svg";

  let player = store.json(KEY);
  let state = null;
  let lastKey = "";
  let stopTimer = null;
  let classes = null;
  let reg = { grade: null, classId: null };
  let lastQuestion = null;
  let chosenText = null;
  let wakeLock = null;

  /* ---------------------------------------------------------------- svg */
  function svg(viewBox, ...kids) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", viewBox);
    s.setAttribute("aria-hidden", "true");
    for (const k of kids) s.appendChild(k);
    return s;
  }
  function el(tag, attrs) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }
  const SHAPES = [
    () => svg("0 0 24 24", el("polygon", { points: "12,2 22,12 12,22 2,12" })),
    () => svg("0 0 24 24", el("circle", { cx: 12, cy: 12, r: 9.5 })),
    () => svg("0 0 24 24", el("polygon", { points: "12,3 22.5,21 1.5,21" })),
    () => svg("0 0 24 24", el("rect", { x: 3, y: 3, width: 18, height: 18, rx: 2.5 })),
  ];
  const stroke = { fill: "none", "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round" };
  const ICONS = {
    check: () => svg("0 0 24 24", el("path", { d: "M5 12.5l4.5 4.5L19 7", ...stroke })),
    cross: () => svg("0 0 24 24", el("path", { d: "M6 6l12 12M18 6L6 18", ...stroke })),
    clock: () => svg("0 0 24 24", el("circle", { cx: 12, cy: 12, r: 9, ...stroke }), el("path", { d: "M12 7v5l3 2", ...stroke })),
    flame: () => svg("0 0 24 24", el("path", { d: "M12 2c1 4 6 6 6 12a6 6 0 0 1-12 0c0-3 2-5 3-6 0 2 1 3 2 3 0-4-1-6 1-9z" })),
    trophy: () => svg("0 0 64 64",
      el("path", { d: "M20 8h24v14a12 12 0 0 1-24 0z", fill: "#fff" }),
      el("path", { d: "M20 12H10v4a10 10 0 0 0 10 10M44 12h10v4a10 10 0 0 1-10 10", fill: "none", stroke: "#fff", "stroke-width": 4 }),
      el("path", { d: "M28 34h8v10h-8zM20 46h24v8H20z", fill: "#fff" })),
  };

  /* ------------------------------------------------------------- socket */
  const socket = D.connect(async () => {
    if (player) await join({ playerId: player.id, token: player.token });
    else if (lastKey !== "reg") showRegistration();
  });
  socket.on("player:state", (s) => {
    if (!s || s.phase === "loggedOut") { forget(); showRegistration(); return; }
    state = s;
    renderState();
  });
  socket.on("player:loggedOut", () => {
    forget();
    toast("تم تسجيل خروجك. سجّل دخولك من جديد.", "warn");
    showRegistration();
  });

  async function join(payload) {
    const res = await socket.request("player:join", payload);
    if (!res.ok) {
      if (res.code === "auth") { forget(); showRegistration(); toast(res.error, "warn"); }
      else toast(res.error, "bad");
      return false;
    }
    player = res.player;
    store.set(KEY, JSON.stringify(player));
    store.set("difaf:lastName", player.name);
    showWho(true);
    return true;
  }

  function forget() {
    player = null;
    state = null;
    lastKey = "";
    store.del(KEY);
    showWho(false);
  }

  function showWho(on) {
    $("#who").hidden = !on || !player;
    document.body.classList.toggle("logged", !!(on && player));
    if (player) {
      $("#whoName").textContent = player.name;
      $("#whoClass").textContent = player.className;
    }
  }

  $("#leaveBtn").addEventListener("click", async () => {
    const yes = await D.confirmBox("هل تريد الخروج من حسابك واختيار فصل أو اسم آخر؟", { title: "تغيير الحساب", ok: "نعم، خروج" });
    if (!yes) return;
    const res = await socket.request("player:leave");
    if (!res.ok) { toast(res.error, "warn"); return; }
    forget();
    showRegistration();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state && state.phase === "question" && socket.connected) socket.emit("player:blur");
    if (!document.hidden && player && socket.connected) socket.emit("player:sync");
    if (!document.hidden) keepAwake();
  });

  async function keepAwake() {
    try {
      if ("wakeLock" in navigator && !wakeLock && document.visibilityState === "visible") {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => { wakeLock = null; });
      }
    } catch (e) { /* not supported */ }
  }

  function stopTimers() { if (stopTimer) { stopTimer(); stopTimer = null; } }
  const vibrate = (p) => { try { if (navigator.vibrate) navigator.vibrate(p); } catch (e) { /* ignore */ } };

  /* ------------------------------------------------------ registration */
  async function loadClasses() {
    try {
      const r = await fetch("/api/classes", { cache: "no-store" });
      classes = (await r.json()).classes || [];
    } catch (e) { classes = null; }
  }

  async function showRegistration() {
    stopTimers();
    lastKey = "reg";
    showWho(false);
    render(app, h("div", { class: "screen center" }, h("div", { class: "spinner" })));
    await loadClasses();
    if (player) return; // logged in meanwhile
    if (!classes) {
      render(app, h("section", { class: "screen center" },
        h("p", { class: "status-box", text: "تعذّر تحميل قائمة الفصول. تأكد من الاتصال بشبكة المدرسة." }),
        h("button", { class: "btn btn-lg", text: "إعادة المحاولة", onclick: showRegistration })));
      return;
    }
    if (reg.classId && !classes.some((c) => c.id === reg.classId)) reg = { grade: null, classId: null };
    if (reg.classId) return stepName();
    if (reg.grade) return stepClass();
    stepGrade();
  }

  function steps(n) {
    return h("div", { class: "steps", "aria-hidden": "true" }, [1, 2, 3].map((i) => h("i", { class: i <= n ? "on" : "" })));
  }

  function stepGrade() {
    const grades = [...new Set(classes.map((c) => c.grade))].sort((a, b) => a - b);
    if (!grades.length) {
      render(app, h("section", { class: "screen center" },
        h("div", { class: "sail-wrap" }, h("img", { src: "/static/img/logo-mark.png", alt: "" })),
        h("p", { class: "status-box", text: "لم يُضف المعلم أي فصل بعد. انتظر قليلًا ثم حدّث الصفحة." }),
        h("button", { class: "btn", text: "تحديث", onclick: showRegistration })));
      return;
    }
    render(app, h("section", { class: "screen" },
      steps(1),
      h("h1", { class: "step-title center", text: "اختر صفّك" }),
      h("p", { class: "step-sub center", text: "أهلًا بك في ضفاف العربية! لنبدأ بتحديد صفّك الدراسي." }),
      h("div", { class: "choice-grid" }, grades.map((g) => h("button", {
        class: "choice", type: "button", onclick: () => { reg.grade = g; stepClass(); },
      }, h("span", {}, h("small", { text: "الصف" }), D.GRADE_NAMES[g] || String(g)))))));
  }

  function stepClass() {
    const list = classes.filter((c) => c.grade === reg.grade);
    if (!list.length) { reg.grade = null; return stepGrade(); }
    render(app, h("section", { class: "screen" },
      steps(2),
      h("button", { class: "back-link", type: "button", text: "→ رجوع", onclick: () => { reg.grade = null; stepGrade(); } }),
      h("h1", { class: "step-title center", text: "اختر فصلك" }),
      h("p", { class: "step-sub center", text: "الصف " + (D.GRADE_NAMES[reg.grade] || reg.grade) }),
      h("div", { class: "choice-grid" }, list.map((c) => h("button", {
        class: "choice class-btn num", type: "button", text: c.name, onclick: () => { reg.classId = c.id; stepName(); },
      })))));
  }

  function stepName() {
    const c = classes.find((x) => x.id === reg.classId);
    const input = h("input", {
      class: "input name-input", maxlength: 30, autocomplete: "name", enterkeyhint: "go",
      placeholder: "الاسم الأول واسم العائلة", "aria-label": "اسمك",
    });
    input.value = store.get("difaf:lastName") || "";
    const btn = h("button", { class: "btn btn-gold btn-lg btn-block", type: "submit", text: "دخول" });
    const form = h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      const name = input.value.trim();
      if (name.length < 2) { input.classList.add("bad"); toast("اكتب اسمك (حرفان على الأقل)", "warn"); input.focus(); return; }
      btn.disabled = true;
      const ok = await join({ classId: reg.classId, name });
      btn.disabled = false;
      if (!ok) input.focus();
    } }, input, btn);
    render(app, h("section", { class: "screen" },
      steps(3),
      h("button", { class: "back-link", type: "button", text: "→ رجوع", onclick: () => { reg.classId = null; stepClass(); } }),
      h("div", { class: "picked" }, h("span", { text: "فصلك" }), h("b", { class: "num", text: c ? c.name : "" })),
      h("h1", { class: "step-title center", text: "ما اسمك؟" }),
      h("p", { class: "step-sub center", text: "اكتب اسمك الحقيقي ليظهر في لوحة الصدارة." }),
      form));
    setTimeout(() => input.focus(), 50);
  }

  /* ------------------------------------------------------------ states */
  function renderState() {
    const s = state;
    if (!s) return;
    showWho(true);
    const key = [s.mode, s.phase, s.qIndex, s.phase === "lobby" ? s.mates + "/" + s.rivals : ""].join("|");
    if (key === lastKey && (s.phase === "question" || s.phase === "countdown" || s.phase === "answered")) return;
    const firstTime = key !== lastKey;
    lastKey = key;
    stopTimers();
    const view = {
      waiting: screenWaiting, lobby: screenLobby, countdown: screenCountdown, question: screenQuestion,
      answered: screenAnswered, reveal: screenReveal, done: screenDone, kicked: screenKicked,
      feedback: screenFeedback, finished: screenFinished, loading: screenLoading,
    }[s.phase] || screenWaiting;
    view(s, firstTime);
    if (["lobby", "countdown", "question"].includes(s.phase)) keepAwake();
  }

  function screenWaiting(s) {
    render(app, h("section", { class: "screen center" },
      h("div", { class: "sail-wrap" }, h("img", { src: "/static/img/logo-mark.png", alt: "" })),
      h("p", { class: "hello", text: "أهلًا " + s.me.name }),
      h("span", { class: "chip navy", text: "فصل " + s.me.className }),
      h("p", { class: "status-box" }, s.note || "لا توجد مسابقة لفصلك الآن.", h("br"),
        h("b", { class: "dots", text: "ابقَ في هذه الصفحة؛ ستبدأ المسابقة تلقائيًا" }))));
  }

  function screenLobby(s) {
    if (s.mode === "knockout") {
      render(app, h("section", { class: "screen center" },
        h("span", { class: "round-label", text: s.match.round }),
        h("div", { class: "versus" },
          h("div", { class: "team mine num", text: s.match.mine }),
          h("span", { class: "vs", text: "ضد" }),
          h("div", { class: "team num", text: s.match.other })),
        h("p", { class: "status-box" }, h("b", { text: "أنت جاهز!" }), " بانتظار المعلم لبدء المباراة", h("span", { class: "dots" })),
        h("div", { class: "scoreline" },
          h("div", { class: "mine" }, h("small", { text: "من فصلك" }), h("b", { class: "num", text: s.mates })),
          h("div", {}, h("small", { text: "من الفصل المنافس" }), h("b", { class: "num", text: s.rivals })))));
    } else {
      render(app, h("section", { class: "screen center" },
        h("div", { class: "sail-wrap" }, h("img", { src: "/static/img/logo-mark.png", alt: "" })),
        h("span", { class: "round-label", text: s.title }),
        h("p", { class: "hello", text: "فصل " + s.me.className }),
        h("p", { class: "status-box" }, h("b", { text: "أنت جاهز!" }), " بانتظار المعلم لبدء المسابقة", h("span", { class: "dots" }),
          h("br"), "عدد الأسئلة: ", h("b", { class: "num", text: s.qTotal }), " — أجب بسرعة لتحصل على نقاط أكثر"),
        h("span", { class: "chip", text: "زملاؤك المتصلون: " + s.mates })));
    }
  }

  function screenCountdown(s) {
    const n = h("div", { class: "big-count num", text: "3" });
    render(app, h("section", { class: "screen center" },
      h("span", { class: "chip navy", text: "السؤال " + (s.qIndex + 1) + " من " + s.qTotal }),
      h("p", { class: "hello", text: "استعد!" }), n));
    let shown = null;
    stopTimer = D.countdown(s.remainingMs, (left) => {
      const v = Math.max(1, Math.ceil(left / 1000));
      if (v !== shown) { shown = v; n.textContent = v; n.style.animation = "none"; void n.offsetWidth; n.style.animation = ""; }
    });
  }

  function screenQuestion(s) {
    const q = s.question;
    lastQuestion = { qIndex: s.qIndex, options: q.options };
    chosenText = null;
    const bar = h("i");
    const timer = h("div", { class: "timer", role: "progressbar", "aria-label": "الوقت المتبقي" }, bar);
    const secs = h("span", { class: "secs num" });
    const opts = h("div", { class: "opts" });
    q.options.forEach((text, i) => {
      const b = h("button", { class: "opt opt-" + (i % 4), type: "button" },
        h("span", { class: "shape" }, SHAPES[i % 4]()), h("span", { class: "t", text }));
      b.addEventListener("click", () => answer(s, i, b, opts, text));
      opts.appendChild(b);
    });
    const info = s.mode === "contest"
      ? h("span", { class: "chip", text: "نقاطك " + fmt.dec(s.points, 1) })
      : h("span", { class: "chip" }, h("span", { class: "num", text: s.match.mine }), " × ", h("span", { class: "num", text: s.match.other }));
    render(app, h("section", { class: "screen" },
      h("div", { class: "q-top" }, h("span", { class: "chip navy", text: "السؤال " + (s.qIndex + 1) + " من " + s.qTotal }), info, secs),
      timer,
      h("p", { class: "q-text", text: q.text }),
      opts,
      h("p", { class: "fair-note", text: "مغادرة هذه الصفحة أثناء السؤال تُسجَّل وتظهر للمعلم." })));
    stopTimer = D.countdown(q.remainingMs, (left) => {
      const f = Math.max(0, Math.min(1, left / q.durationMs));
      bar.style.transform = "scaleX(" + f + ")";
      secs.textContent = Math.ceil(left / 1000);
      const low = left < 5000;
      timer.classList.toggle("low", low);
      secs.classList.toggle("low", low);
    }, () => { opts.classList.add("locked"); });
  }

  async function answer(s, i, btn, opts, text) {
    if (opts.classList.contains("locked")) return;
    opts.classList.add("locked");
    btn.classList.add("chosen");
    chosenText = text;
    vibrate(25);
    const res = await socket.request("player:answer", { q: s.qIndex, choice: i });
    if (!res.ok) {
      if (res.code === "late" || res.code === "dup") { toast(res.error, "warn"); return; }
      toast(res.error, "bad");
      opts.classList.remove("locked");
      btn.classList.remove("chosen");
      chosenText = null;
    }
  }

  function screenAnswered(s) {
    const secs = h("b", { class: "num" });
    render(app, h("section", { class: "screen center" },
      h("span", { class: "chip navy", text: "السؤال " + (s.qIndex + 1) + " من " + s.qTotal }),
      h("div", { class: "result miss", style: { background: "linear-gradient(150deg, var(--navy-700), var(--navy-900))" } },
        h("div", { class: "icon" }, ICONS.check()),
        h("h2", { text: "تم تسجيل إجابتك" }),
        chosenText && lastQuestion && lastQuestion.qIndex === s.qIndex ? h("p", { class: "correct-was", text: "اخترت: " + chosenText }) : null),
      h("p", { class: "status-box" }, "تظهر النتيجة بعد انتهاء الوقت أو إجابة الجميع — ", secs, " ث")));
    stopTimer = D.countdown(s.remainingMs, (left) => { secs.textContent = Math.ceil(left / 1000); });
  }

  function screenReveal(s, firstTime) {
    const r = s.result;
    const kind = r.missed ? "miss" : r.correct ? "ok" : "bad";
    if (firstTime) vibrate(r.correct ? 80 : [120, 60, 120]);
    render(app, h("section", { class: "screen" },
      h("span", { class: "chip navy", style: { alignSelf: "center" }, text: "السؤال " + (s.qIndex + 1) + " من " + s.qTotal }),
      h("div", { class: "result " + kind },
        h("div", { class: "icon" }, r.missed ? ICONS.clock() : r.correct ? ICONS.check() : ICONS.cross()),
        h("h2", { text: r.missed ? "انتهى الوقت!" : r.correct ? "إجابة صحيحة!" : "إجابة خاطئة" }),
        r.correct ? h("div", { class: "pts num", text: "+" + fmt.int(r.points) }) : null,
        r.correct && r.streak >= 2 ? h("span", { class: "streak" }, ICONS.flame(), r.streak + " إجابات صحيحة متتالية") : null,
        !r.correct ? h("p", { class: "correct-was", text: "الإجابة الصحيحة: " + r.correctText }) : null),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", { class: "num", text: fmt.int(r.total) }), h("span", { text: "مجموع نقاطك" })),
        h("div", { class: "stat" }, h("b", { class: "num", text: r.rank + "/" + r.of }), h("span", { text: "ترتيبك" })),
        h("div", { class: "stat" }, h("b", { class: "num", text: r.streak }), h("span", { text: "السلسلة" }))),
      h("div", { class: "scoreline" },
        h("div", { class: "mine" }, h("small", { text: "متوسط فصلك " + s.match.mine }), h("b", { class: "num", text: fmt.int(s.scores.mine) })),
        h("div", {}, h("small", { text: "متوسط " + s.match.other }), h("b", { class: "num", text: fmt.int(s.scores.other) }))),
      h("p", { class: "muted center", text: "بانتظار السؤال التالي…" })));
  }

  function screenDone(s, firstTime) {
    const win = s.outcome === "win";
    if (firstTime && win) { D.confetti(); vibrate([100, 50, 100, 50, 200]); }
    const title = win ? "فاز فصلك!" : s.outcome === "tie" ? "تعادل!" : "انتهت المباراة";
    const sub = win ? "أداء رائع! تأهل فصلك إلى الجولة التالية." : s.outcome === "tie"
      ? "النتيجة متساوية تمامًا، بانتظار قرار لجنة المسابقة." : "حظًا أوفر في المرة القادمة، فخورون بمشاركتكم.";
    render(app, h("section", { class: "screen" },
      h("div", { class: "final " + (win ? "win" : "") },
        win ? h("div", { class: "trophy" }, ICONS.trophy()) : null,
        h("h2", { text: title }), h("p", { text: sub })),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", { class: "num", text: fmt.int(s.total) }), h("span", { text: "نقاطك" })),
        h("div", { class: "stat" }, h("b", { class: "num", text: s.correct + "/" + s.questions }), h("span", { text: "إجابات صحيحة" })),
        h("div", { class: "stat" }, h("b", { class: "num", text: s.rank + "/" + s.of }), h("span", { text: "ترتيبك" }))),
      h("div", { class: "scoreline" },
        h("div", { class: "mine" }, h("small", { text: "متوسط " + s.match.mine }), h("b", { class: "num", text: fmt.int(s.scores.mine) })),
        h("div", {}, h("small", { text: "متوسط " + s.match.other }), h("b", { class: "num", text: fmt.int(s.scores.other) }))),
      s.best >= 2 ? h("p", { class: "center" }, h("span", { class: "chip", text: "أطول سلسلة إجابات صحيحة: " + s.best })) : null));
  }

  function screenKicked() {
    render(app, h("section", { class: "screen center" },
      h("p", { class: "kicked", text: "استبعدك المعلم من هذه المسابقة. إن كان ذلك خطأ، راجع معلمك." }),
      h("button", { class: "btn", text: "تغيير الحساب", onclick: () => $("#leaveBtn").click() })));
  }

  function screenFeedback(s, firstTime) {
    const l = s.last || {};
    const kind = l.timeout ? "miss" : l.correct ? "ok" : "bad";
    if (firstTime) vibrate(l.correct ? 60 : [100, 50, 100]);
    render(app, h("section", { class: "screen center" },
      h("span", { class: "chip navy", text: "أنجزت " + s.qIndex + " من " + s.qTotal }),
      h("div", { class: "flash " + kind },
        l.timeout ? "انتهى الوقت" : l.correct ? "إجابة صحيحة" : "إجابة خاطئة",
        l.correct ? h("small", { class: "num", text: "+" + fmt.dec(l.points, 2) + " نقطة" }) : null),
      h("p", { class: "muted dots", text: s.qIndex < s.qTotal ? "السؤال التالي" : "" })));
  }

  function screenFinished(s, firstTime) {
    if (firstTime) { D.confetti({ count: 120 }); vibrate([80, 40, 80]); }
    render(app, h("section", { class: "screen" },
      h("div", { class: "final win" },
        h("div", { class: "trophy" }, ICONS.trophy()),
        h("h2", { text: "أنهيت جميع الأسئلة!" }),
        h("p", { text: "متوسطك " + fmt.dec(s.avg5, 2) + " من 5 لكل سؤال" })),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("b", { class: "num", text: fmt.dec(s.points, 1) }), h("span", { text: "مجموع نقاطك" })),
        h("div", { class: "stat" }, h("b", { class: "num", text: s.correct + "/" + s.qTotal }), h("span", { text: "إجابات صحيحة" })),
        h("div", { class: "stat" }, h("b", { class: "num", text: s.rank + "/" + s.of }), h("span", { text: "ترتيبك في الفصل" }))),
      s.classDone
        ? h("p", { class: "status-box center" }, "انتهت مسابقة فصلك. متوسط الفصل: ", h("b", { class: "num", text: fmt.dec(s.classAvg, 2) + " / 5" }))
        : h("p", { class: "status-box center" }, h("b", { class: "dots", text: "بانتظار انتهاء زملائك" }))));
  }

  function screenLoading() {
    render(app, h("section", { class: "screen center" }, h("div", { class: "spinner" }), h("p", { class: "muted", text: "جارٍ تجهيز السؤال…" })));
  }
})();
