/* Teacher control panel. */
(function () {
  "use strict";
  const D = window.Difaf;
  const { $, $$, h, render, store, toast, fmt, confirmBox } = D;
  const TOKEN_KEY = "difaf:adminToken";
  const GRADES = D.GRADE_NAMES;

  let token = store.get(TOKEN_KEY);
  let data = null;
  let live = null;
  let loggedIn = false;
  let tab = store.get("difaf:adminTab") || "classes";

  let selId = store.get("difaf:adminClass");
  let draft = null;
  let dirty = false;
  let reloadDraft = false;

  const form = { type: "knockout", name: "", classIds: new Set(), qpm: 10, pairing: "grade" };
  let formSig = "";
  const bulkSel = new Set();
  let liveStop = null;

  /* ------------------------------------------------------------ connection */
  let pointerDown = false;
  let livePending = false;
  document.addEventListener("pointerdown", () => { pointerDown = true; }, true);
  const release = () => {
    pointerDown = false;
    if (livePending) { livePending = false; setTimeout(renderLive, 60); }
  };
  document.addEventListener("pointerup", release, true);
  document.addEventListener("pointercancel", release, true);

  const socket = D.connect(async () => {
    if (token) {
      const r = await socket.request("admin:login", { token });
      if (r.ok) { loggedIn = true; showShell(); return; }
      token = null;
      store.del(TOKEN_KEY);
    }
    showLogin();
  });
  socket.on("admin:data", (d) => { data = d; if (loggedIn) onData(); });
  socket.on("admin:live", (l) => { live = l; if (loggedIn) onLive(); });
  socket.on("admin:loggedOut", (p) => { logoutLocal(); showLogin(p && p.reason); });

  function logoutLocal() { loggedIn = false; token = null; store.del(TOKEN_KEY); }

  async function act(event, payload, okMsg) {
    const r = await socket.request(event, payload);
    if (!r.ok) {
      if (r.code === "auth") { logoutLocal(); showLogin(r.error); }
      else toast(r.error, "bad", 6000);
      return null;
    }
    if (r.warning) toast(r.warning, "warn", 7000);
    if (okMsg) toast(okMsg, "ok");
    return r;
  }

  /* ------------------------------------------------------------------ login */
  function showLogin(msg) {
    $("#shell").hidden = true;
    $("#login").hidden = false;
    $("#loginMsg").textContent = msg || "";
    $("#pw").value = "";
    setTimeout(() => $("#pw").focus(), 50);
  }
  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    btn.disabled = true;
    const r = await socket.request("admin:login", { password: $("#pw").value });
    btn.disabled = false;
    if (!r.ok) { $("#loginMsg").textContent = r.error; $("#pw").select(); return; }
    token = r.token;
    store.set(TOKEN_KEY, token);
    loggedIn = true;
    showShell();
  });

  function showShell() {
    $("#login").hidden = true;
    $("#shell").hidden = false;
    selectTab(tab);
  }

  $$(".a-tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  function selectTab(name) {
    tab = name;
    store.set("difaf:adminTab", name);
    $$(".a-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    $$(".tab").forEach((s) => { s.hidden = s.id !== "tab-" + name; });
    $(".save-bar") && $(".save-bar").classList.toggle("show", dirty && name === "classes");
    renderTab();
  }
  function renderTab() {
    if (!data) return;
    ({ classes: renderClasses, league: renderLeague, live: renderLive, stats: renderStats, settings: renderSettings })[tab]();
  }

  function onData() {
    renderBanner();
    if (tab === "live") scheduleLive(); else renderTab();
  }
  function onLive() {
    $("#stOnline").textContent = (live.onlineTotal || 0) + " طالب متصل";
    $("#stDisplay").textContent = live.displays ? "الشاشة متصلة (" + live.displays + ")" : "لا شاشة";
    if (tab === "live") scheduleLive();
  }
  function renderBanner() {
    const b = $("#banner");
    if (data.password && data.password.isDefault) {
      render(b, "تنبيه: ما زالت كلمة المرور الافتراضية (admin123) مستخدمة. غيّرها من ",
        h("a", { href: "#", text: "الإعدادات", onclick: (e) => { e.preventDefault(); selectTab("settings"); } }),
        " حتى لا يتمكن أحد الطلاب من الدخول إلى لوحة التحكم.");
      b.hidden = false;
    } else b.hidden = true;
  }

  const classById = (id) => data.classes.find((c) => c.id === id);
  const lim = () => data.limits;
  function byGrade(classes) {
    const g = {};
    for (const c of classes) (g[c.grade] = g[c.grade] || []).push(c);
    return Object.keys(g).map(Number).sort((a, b) => a - b).map((k) => [k, g[k]]);
  }

  function modal(title, body, buttons) {
    const back = h("div", { class: "modal-back" });
    const close = () => back.remove();
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    back.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    render(back, h("div", { class: "card modal stack", role: "dialog", "aria-modal": "true" },
      h("h3", { text: title }), body,
      h("div", { class: "row", style: { justifyContent: "flex-end" } },
        h("button", { class: "btn btn-light", type: "button", text: "إغلاق", onclick: close }),
        (buttons || []).map((b) => h("button", { class: "btn " + (b.cls || ""), type: "button", text: b.text, onclick: () => b.onclick(close) })))));
    document.body.appendChild(back);
    return close;
  }

  /* ===================================================== classes & questions */
  function renderClasses() {
    const root = $("#tab-classes");
    if (!root.firstChild) {
      render(root, h("div", { class: "classes-layout" }, h("aside", { class: "class-side", id: "classSide" }), h("section", { id: "editor" })));
      const bar = h("div", { class: "save-bar" },
        h("b", { text: "لديك تغييرات غير محفوظة" }),
        h("button", { class: "btn btn-gold", type: "button", text: "حفظ الأسئلة", onclick: saveDraft }),
        h("button", { class: "btn btn-light", type: "button", text: "تراجع عن التغييرات", onclick: async () => {
          if (await confirmBox("هل تريد التراجع عن جميع التغييرات غير المحفوظة؟", { ok: "تراجع" })) { setDirty(false); loadDraft(); renderEditor(); }
        } }));
      document.body.appendChild(bar);
    }
    renderClassSide();
    if (!classById(selId)) { selId = data.classes[0] ? data.classes[0].id : null; draft = null; setDirty(false); }
    const outdated = draft && !dirty && draft.base !== JSON.stringify(classById(selId)?.questions || []);
    if (!draft || draft.classId !== selId || reloadDraft || outdated) {
      reloadDraft = false;
      loadDraft();
      renderEditor();
    }
  }

  function setDirty(v) {
    dirty = v;
    const bar = $(".save-bar");
    if (bar) bar.classList.toggle("show", v && tab === "classes");
  }
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  function renderClassSide() {
    const side = $("#classSide");
    const gradeSel = h("select", { class: "input" }, Object.entries(GRADES).map(([g, n]) => h("option", { value: g, text: "الصف " + n })));
    gradeSel.value = store.get("difaf:lastGrade") || "10";
    const from = h("input", { class: "input", type: "number", min: 1, max: 30, value: 1 });
    const to = h("input", { class: "input", type: "number", min: 1, max: 30, value: 1 });
    from.addEventListener("change", () => { if (+to.value < +from.value) to.value = from.value; });
    render(side,
      h("div", { class: "panel-card" },
        h("h3", { text: "إضافة فصول" }),
        h("div", { class: "add-class" },
          h("label", { class: "field" }, h("span", { text: "الصف" }), gradeSel),
          h("label", { class: "field" }, h("span", { text: "من شعبة" }), from),
          h("label", { class: "field" }, h("span", { text: "إلى شعبة" }), to),
          h("button", { class: "btn btn-sm", type: "button", text: "إضافة", onclick: async () => {
            store.set("difaf:lastGrade", gradeSel.value);
            const r = await act("class:create", { grade: +gradeSel.value, from: +from.value, to: +to.value });
            if (r) toast("أُضيفت الفصول: " + r.created.join("، "), "ok");
          } })),
        h("p", { class: "hint", text: "مثال: اختر الصف العاشر من شعبة 1 إلى شعبة 4 لإضافة أربعة فصول دفعة واحدة." })),
      h("div", { class: "panel-card" },
        h("h3", { text: "الفصول (" + data.classes.length + ")" }),
        data.classes.length ? byGrade(data.classes).map(([g, list]) => h("div", { class: "grade-group" },
          h("h4", { text: "الصف " + GRADES[g] }),
          h("div", { class: "class-list" }, list.map((c) => h("button", {
            class: "class-item" + (c.id === selId ? " sel" : ""), type: "button",
            onclick: () => pickClass(c.id),
          }, h("span", { class: "num", text: c.name }), h("span", { class: "cnt" + (c.questions.length ? "" : " zero"), text: c.questions.length + " سؤال" }))))))
          : h("p", { class: "hint", text: "لا توجد فصول بعد." })));
  }

  async function pickClass(id) {
    if (id === selId) return;
    if (dirty && !(await confirmBox("لديك تغييرات غير محفوظة في أسئلة هذا الفصل. هل تريد تجاهلها؟", { ok: "تجاهل التغييرات", danger: true }))) return;
    selId = id;
    store.set("difaf:adminClass", id);
    setDirty(false);
    loadDraft();
    renderClassSide();
    renderEditor();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function blankQuestion() { return { id: null, text: "", correct: "", wrong: ["", "", ""], time: lim().defaultTime }; }
  function loadDraft() {
    const c = classById(selId);
    if (!c) { draft = null; return; }
    draft = {
      classId: c.id,
      base: JSON.stringify(c.questions),
      questions: c.questions.map((q) => ({ id: q.id, text: q.text, correct: q.correct, wrong: [...q.wrong, "", "", ""].slice(0, 3), time: q.time })),
    };
  }

  function renderEditor() {
    const ed = $("#editor");
    if (!selId || !draft) {
      render(ed, h("div", { class: "empty" }, "أضف فصلًا من القائمة لتبدأ بكتابة أسئلته."));
      return;
    }
    const c = classById(selId);
    const L = lim();
    const countInput = h("input", { class: "input", type: "number", min: 0, max: L.maxQuestions, value: draft.questions.length });
    const timeAll = h("input", { class: "input", type: "number", min: L.minTime, max: L.maxTime, value: L.defaultTime });
    render(ed, h("div", { class: "panel-card" },
      h("div", { class: "editor-head" },
        h("span", { class: "title" }, "أسئلة فصل ", h("span", { class: "num", text: c.name })),
        h("span", { class: "chip navy", text: c.questions.length + " سؤالًا محفوظًا" }),
        h("div", { class: "row", style: { marginInlineStart: "auto" } },
          h("button", { class: "btn btn-light btn-sm", type: "button", text: "أسئلة تجريبية", onclick: addSamples }),
          h("button", { class: "btn btn-light btn-sm", type: "button", text: "استيراد من نص", onclick: importText }),
          h("button", { class: "btn btn-light btn-sm", type: "button", text: "نسخ إلى فصول أخرى", onclick: copyModal }),
          h("button", { class: "btn btn-bad btn-sm", type: "button", text: "حذف الفصل", onclick: deleteClass }))),
      h("div", { class: "editor-tools" },
        h("label", { class: "field" }, h("span", { text: "عدد الأسئلة" }), countInput),
        h("button", { class: "btn btn-sm", type: "button", text: "تطبيق العدد", onclick: () => setCount(+countInput.value) }),
        h("span", { class: "sep" }),
        h("label", { class: "field" }, h("span", { text: "زمن كل سؤال (ثانية)" }), timeAll),
        h("button", { class: "btn btn-light btn-sm", type: "button", text: "تطبيق الزمن على الكل", onclick: () => {
          const t = Math.max(L.minTime, Math.min(L.maxTime, +timeAll.value || L.defaultTime));
          draft.questions.forEach((q) => { q.time = t; });
          setDirty(true);
          renderEditor();
        } })),
      h("p", { class: "hint", text: "لكل سؤال: النص، والإجابة الصحيحة، وخيار خاطئ واحد على الأقل (حتى ثلاثة)، والزمن بالثواني (من " + L.minTime + " إلى " + L.maxTime + "). تُخلط الخيارات تلقائيًا وبترتيب مختلف لكل طالب." }),
      h("div", { class: "qcards" }, draft.questions.map((q, i) => qCard(q, i, draft.questions.length))),
      draft.questions.length
        ? h("div", { class: "row", style: { marginTop: "14px" } },
            h("button", { class: "btn btn-ghost", type: "button", text: "+ إضافة سؤال", onclick: addOne }),
            h("button", { class: "btn btn-gold", type: "button", text: "حفظ الأسئلة", onclick: saveDraft }))
        : h("div", { class: "empty" }, "لا توجد أسئلة بعد. اكتب العدد ثم اضغط «تطبيق العدد»، أو أضف أسئلة تجريبية.", h("br"), h("br"),
            h("button", { class: "btn", type: "button", text: "+ إضافة سؤال", onclick: addOne }))));
  }

  function qCard(q, i, n) {
    const mark = () => setDirty(true);
    const text = h("textarea", { class: "input", rows: 2, maxlength: 400, placeholder: "اكتب نص السؤال…" });
    text.value = q.text;
    text.addEventListener("input", () => { q.text = text.value; mark(); });
    const cor = h("input", { class: "input ok", maxlength: 150, placeholder: "الإجابة الصحيحة" });
    cor.value = q.correct;
    cor.addEventListener("input", () => { q.correct = cor.value; mark(); });
    const wrongs = [0, 1, 2].map((k) => {
      const w = h("input", { class: "input bad", maxlength: 150, placeholder: k === 0 ? "خيار خاطئ (مطلوب)" : "خيار خاطئ (اختياري)" });
      w.value = q.wrong[k] || "";
      w.addEventListener("input", () => { q.wrong[k] = w.value; mark(); });
      return h("label", { class: "field" }, h("span", { text: "خيار خاطئ " + (k + 1) }), w);
    });
    const time = h("input", { class: "input", type: "number", min: lim().minTime, max: lim().maxTime, value: q.time });
    time.addEventListener("input", () => { q.time = time.value; mark(); });
    return h("div", { class: "qcard", id: "q-" + i },
      h("div", { class: "qcard-head" },
        h("span", { class: "qnum num", text: i + 1 }),
        h("b", { text: "السؤال " + (i + 1) }),
        h("div", { class: "tools" },
          h("button", { class: "icon-btn", type: "button", title: "تحريك لأعلى", "aria-label": "تحريك لأعلى", text: "↑", disabled: i === 0, onclick: () => move(i, -1) }),
          h("button", { class: "icon-btn", type: "button", title: "تحريك لأسفل", "aria-label": "تحريك لأسفل", text: "↓", disabled: i === n - 1, onclick: () => move(i, 1) }),
          h("button", { class: "icon-btn danger", type: "button", title: "حذف السؤال", "aria-label": "حذف السؤال", text: "✕", onclick: () => removeQ(i) }))),
      h("label", { class: "field" }, h("span", { text: "نص السؤال" }), text),
      h("div", { class: "qgrid" },
        h("label", { class: "field full" }, h("span", { text: "الإجابة الصحيحة" }), cor),
        wrongs,
        h("label", { class: "field qtime" }, h("span", { text: "الزمن (ثانية)" }), time)));
  }

  const isEmptyQ = (q) => !q.text.trim() && !q.correct.trim() && !q.wrong.some((w) => w.trim());
  function addOne() {
    draft.questions.push(blankQuestion());
    setDirty(true);
    renderEditor();
    const last = $("#q-" + (draft.questions.length - 1));
    if (last) { last.scrollIntoView({ behavior: "smooth", block: "center" }); last.querySelector("textarea").focus(); }
  }
  async function setCount(n) {
    const L = lim();
    n = Math.max(0, Math.min(L.maxQuestions, Math.floor(n) || 0));
    const qs = draft.questions;
    if (n < qs.length) {
      const removed = qs.slice(n).filter((q) => !isEmptyQ(q)).length;
      if (removed && !(await confirmBox("سيتم حذف " + removed + " من الأسئلة المكتوبة في آخر القائمة. هل تريد المتابعة؟", { ok: "حذف", danger: true }))) return;
      qs.length = n;
    } else {
      while (qs.length < n) qs.push(blankQuestion());
    }
    setDirty(true);
    renderEditor();
  }
  function move(i, d) {
    const qs = draft.questions;
    const j = i + d;
    if (j < 0 || j >= qs.length) return;
    [qs[i], qs[j]] = [qs[j], qs[i]];
    setDirty(true);
    renderEditor();
    const el = $("#q-" + j);
    if (el) el.scrollIntoView({ block: "center" });
  }
  async function removeQ(i) {
    const q = draft.questions[i];
    if (!isEmptyQ(q) && !(await confirmBox("حذف السؤال رقم " + (i + 1) + "؟", { ok: "حذف", danger: true }))) return;
    draft.questions.splice(i, 1);
    setDirty(true);
    renderEditor();
  }

  function validate() {
    const L = lim();
    const errs = [];
    draft.questions.forEach((q, i) => {
      const text = q.text.trim(), cor = q.correct.trim();
      const wr = q.wrong.map((w) => w.trim()).filter(Boolean);
      let e = null;
      if (!text) e = "نص السؤال فارغ";
      else if (!cor) e = "اكتب الإجابة الصحيحة";
      else if (!wr.length) e = "أضف خيارًا خاطئًا واحدًا على الأقل";
      else {
        const all = [cor, ...wr].map((x) => x.toLowerCase());
        if (new Set(all).size !== all.length) e = "يوجد خيار مكرر";
      }
      const t = Number(q.time);
      if (!e && !(Number.isFinite(t) && t >= L.minTime && t <= L.maxTime)) e = "الزمن يجب أن يكون بين " + L.minTime + " و" + L.maxTime + " ثانية";
      if (e) errs.push([i, e]);
    });
    return errs;
  }

  async function saveDraft() {
    if (!draft) return;
    $$(".qcard.err").forEach((c) => c.classList.remove("err"));
    const errs = validate();
    if (errs.length) {
      errs.forEach(([i]) => { const c = $("#q-" + i); if (c) c.classList.add("err"); });
      const [i, msg] = errs[0];
      const c = $("#q-" + i);
      if (c) c.scrollIntoView({ behavior: "smooth", block: "center" });
      toast("السؤال " + (i + 1) + ": " + msg + (errs.length > 1 ? "\n(وهناك " + (errs.length - 1) + " أخطاء أخرى)" : ""), "bad", 6000);
      return;
    }
    const questions = draft.questions.map((q) => ({
      id: q.id || undefined, text: q.text, correct: q.correct,
      wrong: q.wrong.map((w) => w.trim()).filter(Boolean), time: Number(q.time),
    }));
    const r = await act("class:saveQuestions", { classId: draft.classId, questions });
    if (r) {
      setDirty(false);
      reloadDraft = true;
      toast("تم حفظ " + r.count + " سؤالًا", "ok");
    }
  }

  async function addSamples() {
    if (dirty) { toast("احفظ تغييراتك أولًا", "warn"); return; }
    const r = await act("class:addSamples", { classId: selId });
    if (r) { reloadDraft = true; toast(r.added ? "أُضيف " + r.added + " سؤالًا تجريبيًا" : "الأسئلة التجريبية موجودة مسبقًا", r.added ? "ok" : "warn"); }
  }

  async function deleteClass() {
    const c = classById(selId);
    if (!c) return;
    const yes = await confirmBox("سيُحذف الفصل " + c.name + " وجميع أسئلته (" + c.questions.length + ")، ويُسجَّل خروج طلابه. لا يمكن التراجع.", { title: "حذف الفصل", ok: "حذف الفصل", danger: true });
    if (!yes) return;
    setDirty(false);
    const r = await act("class:delete", { classId: c.id }, "تم حذف الفصل");
    if (r) draft = null;
  }

  function copyModal() {
    if (dirty) { toast("احفظ تغييراتك أولًا قبل النسخ", "warn"); return; }
    const src = classById(selId);
    if (!src.questions.length) { toast("لا توجد أسئلة محفوظة في هذا الفصل", "warn"); return; }
    const chosen = new Set();
    const others = data.classes.filter((c) => c.id !== selId);
    if (!others.length) { toast("لا توجد فصول أخرى", "warn"); return; }
    let mode = "append";
    const body = h("div", { class: "stack" },
      h("p", { text: "نسخ " + src.questions.length + " سؤالًا من فصل " + src.name + " إلى:" }),
      h("div", { class: "row" }, others.map((c) => {
        const cb = h("input", { type: "checkbox" });
        const lab = h("label", { class: "pill-check" }, cb, h("span", { class: "num", text: c.name }), h("small", { text: "(" + c.questions.length + ")" }));
        cb.addEventListener("change", () => { cb.checked ? chosen.add(c.id) : chosen.delete(c.id); lab.classList.toggle("on", cb.checked); });
        return lab;
      })),
      h("div", { class: "seg" },
        ...[["append", "إضافة إلى أسئلتهم"], ["replace", "استبدال أسئلتهم"]].map(([m, t]) => {
          const b = h("button", { type: "button", class: m === mode ? "on" : "", text: t });
          b.addEventListener("click", () => { mode = m; $$(".seg button", b.parentNode).forEach((x) => x.classList.toggle("on", x === b)); });
          return b;
        })));
    modal("نسخ الأسئلة إلى فصول أخرى", body, [{ text: "نسخ", cls: "btn-gold", onclick: async (close) => {
      if (!chosen.size) { toast("اختر فصلًا واحدًا على الأقل", "warn"); return; }
      if (mode === "replace" && !(await confirmBox("سيتم استبدال أسئلة الفصول المختارة بالكامل. متابعة؟", { ok: "استبدال", danger: true }))) return;
      const r = await act("class:copyQuestions", { fromId: selId, toIds: [...chosen], mode }, "تم نسخ الأسئلة");
      if (r) close();
    } }]);
  }

  function importText() {
    const ta = h("textarea", { class: "input", rows: 12, placeholder: "ما إعراب كلمة «العلمُ» في «العلمُ نورٌ»؟\nمبتدأ مرفوع\nخبر مرفوع\nفاعل مرفوع\nمفعول به منصوب\n20\n\nالسؤال التالي…" });
    const body = h("div", { class: "stack" },
      h("p", { class: "hint" }, "اكتب كل سؤال في مجموعة أسطر، وافصل بين الأسئلة بسطر فارغ:", h("br"),
        "السطر الأول: نص السؤال — الثاني: الإجابة الصحيحة — بعده من 1 إلى 3 خيارات خاطئة — ويمكن إضافة الزمن بالثواني في سطر أخير (اختياري)."),
      ta);
    modal("استيراد أسئلة من نص", body, [{ text: "إضافة إلى القائمة", cls: "btn-gold", onclick: (close) => {
      const L = lim();
      const blocks = ta.value.replace(/\r/g, "").split(/\n\s*\n/).map((b) => b.split("\n").map((x) => x.trim()).filter(Boolean)).filter((b) => b.length);
      let added = 0, bad = 0;
      for (const b of blocks) {
        let time = L.defaultTime;
        const last = b[b.length - 1].replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d));
        if (/^\d{1,3}$/.test(last) && b.length >= 4) { time = Math.max(L.minTime, Math.min(L.maxTime, +last)); b.pop(); }
        if (b.length < 3 || b.length > 5) { bad++; continue; }
        draft.questions.push({ id: null, text: b[0], correct: b[1], wrong: [...b.slice(2), "", "", ""].slice(0, 3), time });
        added++;
      }
      if (!added) { toast("لم يُعثر على أسئلة بالتنسيق المطلوب", "bad"); return; }
      setDirty(true);
      renderEditor();
      close();
      toast("أُضيف " + added + " سؤالًا إلى القائمة. لا تنسَ الحفظ." + (bad ? "\nتم تجاهل " + bad + " مجموعة غير مكتملة." : ""), bad ? "warn" : "ok", 6000);
    } }]);
    setTimeout(() => ta.focus(), 50);
  }

  /* ========================================================= league & rounds */
  function renderLeague() {
    const root = $("#tab-league");
    if (!root.firstChild) {
      render(root, h("div", { class: "grid-2", style: { alignItems: "start" } }, h("div", { id: "createBox" }), h("div", { id: "tList" })));
      formSig = "";
    }
    const sig = JSON.stringify(data.classes.map((c) => [c.id, c.name, c.questions.length]));
    if (sig !== formSig) { formSig = sig; renderCreateForm(); }
    renderTournamentList();
  }

  function renderCreateForm() {
    const box = $("#createBox");
    for (const id of [...form.classIds]) if (!classById(id)) form.classIds.delete(id);
    const ko = form.type === "knockout";
    const typeCard = (type, title, desc) => h("button", {
      type: "button", class: "type-card" + (form.type === type ? " sel" : ""),
      onclick: () => { form.type = type; renderCreateForm(); },
    }, h("b", { text: title }), h("span", { text: desc }));
    const nameInput = h("input", { class: "input", maxlength: 60, placeholder: ko ? "دوري ضفاف العربية" : "مسابقات ضفاف العربية" });
    nameInput.value = form.name;
    nameInput.addEventListener("input", () => { form.name = nameInput.value; });
    const qpm = h("input", { class: "input", type: "number", min: 1, max: 50, value: form.qpm, style: { width: "120px" } });
    qpm.addEventListener("change", () => { form.qpm = Math.max(1, Math.min(50, +qpm.value || 10)); renderCreateForm(); });
    const checks = data.classes.length ? h("div", { class: "class-checks" }, byGrade(data.classes).map(([g, list]) => h("div", { class: "grade-row" },
      h("b", { text: "الصف " + GRADES[g] }),
      list.map((c) => {
        const short = ko ? c.questions.length < form.qpm : !c.questions.length;
        const cb = h("input", { type: "checkbox" });
        cb.checked = form.classIds.has(c.id);
        const lab = h("label", { class: "pill-check" + (cb.checked ? " on" : "") + (short ? " warn" : "") },
          cb, h("span", { class: "num", text: c.name }), h("small", { text: c.questions.length + " سؤال" }));
        cb.addEventListener("change", () => { cb.checked ? form.classIds.add(c.id) : form.classIds.delete(c.id); lab.classList.toggle("on", cb.checked); count.textContent = form.classIds.size; });
        return lab;
      }),
      h("button", { class: "btn btn-light btn-sm", type: "button", text: "الكل", onclick: () => {
        const all = list.every((c) => form.classIds.has(c.id));
        list.forEach((c) => (all ? form.classIds.delete(c.id) : form.classIds.add(c.id)));
        renderCreateForm();
      } }))))
      : h("div", { class: "empty" }, "أضف الفصول أولًا من تبويب «الفصول والأسئلة».");
    const count = h("b", { class: "num", text: form.classIds.size });
    render(box, h("div", { class: "panel-card stack" },
      h("h2", { text: "إنشاء مسابقة جديدة" }),
      h("div", { class: "type-pick" },
        typeCard("knockout", "دوري خروج المغلوب", "مباريات بين فصلين، والفائز يتأهل حتى يُتوَّج البطل."),
        typeCard("separate", "مسابقات منفصلة للفصول", "كل فصل يجيب عن جميع أسئلته، ويمكن تشغيل عدة فصول معًا.")),
      h("label", { class: "field" }, h("span", { text: "اسم المسابقة" }), nameInput),
      h("div", { class: "field" }, h("span", {}, "الفصول المشاركة (المختار: ", count, ")"), checks),
      ko ? h("div", { class: "row" },
        h("label", { class: "field" }, h("span", { text: "عدد الأسئلة في كل مباراة" }), qpm),
        h("div", { class: "field" }, h("span", { text: "طريقة الاقتران" }),
          h("div", { class: "seg" },
            ...[["grade", "حسب الصف"], ["random", "قرعة عشوائية"]].map(([v, t]) => h("button", {
              type: "button", class: form.pairing === v ? "on" : "", text: t, onclick: () => { form.pairing = v; renderCreateForm(); },
            }))))) : null,
      ko ? h("p", { class: "hint", text: "يُنشأ جدول خروج المغلوب تلقائيًا، مع تأهل مباشر لبعض الفصول إذا لم يكن العدد من قوى العدد 2 (4، 8، 16…). الفصول المعلَّمة بالأحمر أسئلتها أقل من المطلوب." })
        : h("p", { class: "hint", text: "تُرتَّب الفصول بمتوسط النقاط من 5 لكل سؤال، ثم الدقة، ثم متوسط زمن الإنجاز." }),
      h("button", { class: "btn btn-gold btn-lg", type: "button", text: "إنشاء المسابقة", onclick: createTournament })));
  }

  async function createTournament() {
    const min = form.type === "knockout" ? 2 : 1;
    if (form.classIds.size < min) { toast(min === 2 ? "اختر فصلين على الأقل" : "اختر فصلًا واحدًا على الأقل", "warn"); return; }
    const r = await act("tournament:create", {
      type: form.type, name: form.name, classIds: [...form.classIds], questionsPerMatch: form.qpm, pairing: form.pairing,
    }, "تم إنشاء المسابقة");
    if (r) {
      form.name = "";
      form.classIds.clear();
      formSig = "";
      renderLeague();
    }
  }

  function renderTournamentList() {
    const box = $("#tList");
    const activeId = data.settings.activeTournamentId;
    const list = data.tournaments;
    render(box, h("div", { class: "stack" },
      h("div", { class: "sec-head" }, h("h2", { text: "المسابقات" }), h("span", { class: "chip navy", text: list.length + " مسابقة" })),
      list.length ? list.map((t) => tournamentCard(t, t.id === activeId)) : h("div", { class: "empty", text: "لم تُنشأ أي مسابقة بعد." })));
  }

  function tournamentCard(t, active) {
    const ko = t.type === "knockout";
    let body;
    if (ko) {
      const qpm = h("input", { class: "input", type: "number", min: 1, max: 50, value: t.questionsPerMatch, style: { width: "90px", minHeight: "36px" } });
      body = [
        h("div", { class: "row" },
          h("span", { class: "hint", text: "أسئلة كل مباراة:" }), qpm,
          h("button", { class: "btn btn-light btn-sm", type: "button", text: "حفظ", onclick: () => act("tournament:setQuestions", { tournamentId: t.id, questionsPerMatch: +qpm.value }, "تم التحديث") }),
          h("span", { class: "hint", text: t.pairing === "random" ? "قرعة عشوائية" : "حسب الصف" })),
        t.champion ? h("p", {}, h("span", { class: "chip ok", text: "البطل: " + t.champion.name })) : null,
        h("div", { class: "mini-bracket" }, t.rounds.map((r) => h("div", { class: "mini-round" },
          h("b", { text: r[0] ? r[0].roundName : "" }),
          r.map((m) => h("div", { class: "mini-match" },
            m.bye ? h("span", { class: "num" }, (m.A.name || "") + " ", h("small", { class: "muted", text: "(تأهل مباشر)" }))
              : h("span", { class: "num" },
                  h("span", { class: m.winner && m.winner === m.A.id ? "w" : "", text: m.A.name || "؟" }), " × ",
                  h("span", { class: m.winner && m.winner === m.B.id ? "w" : "", text: m.B.name || "؟" }))))))),
      ];
    } else {
      body = [h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["الفصل", "الحالة", "المتوسط /5", "الدقة"].map((x) => h("th", { text: x })))),
        h("tbody", {}, t.contests.map((r) => h("tr", {},
          h("td", { class: "num", text: r.name }), h("td", { text: STATUS[r.status] }),
          h("td", { class: "num", text: r.status === "idle" ? "—" : fmt.dec(r.status === "finished" ? r.avg : r.liveAvg, 2) }),
          h("td", { class: "num", text: r.status === "idle" ? "—" : fmt.pct(r.accuracy) }))))))];
    }
    return h("div", { class: "panel-card tcard" + (active ? " active" : "") },
      h("div", { class: "tcard-head" },
        h("h3", { text: t.name }),
        h("span", { class: "chip navy", text: ko ? "خروج المغلوب" : "فصول منفصلة" }),
        active ? h("span", { class: "chip ok", text: "مفعّلة" }) : null,
        h("div", { class: "tcard-actions" },
          active ? h("button", { class: "btn btn-sm", type: "button", text: "إلى التحكم المباشر", onclick: () => selectTab("live") })
            : h("button", { class: "btn btn-sm btn-gold", type: "button", text: "تفعيل", onclick: () => act("tournament:activate", { tournamentId: t.id }, "تم التفعيل") }),
          h("button", { class: "btn btn-sm btn-light", type: "button", text: "حذف", onclick: async () => {
            if (await confirmBox("حذف «" + t.name + "» نهائيًا؟ تبقى النتائج المسجلة في الإحصائيات.", { title: "حذف المسابقة", ok: "حذف", danger: true }))
              act("tournament:delete", { tournamentId: t.id }, "تم الحذف");
          } }))),
      h("div", { class: "row" }, h("span", { class: "hint", text: "أُنشئت " + fmt.date(t.createdAt) + " — الفصول: " }),
        t.classNames.map((n) => h("span", { class: "chip num", text: n }))),
      body);
  }

  const STATUS = { idle: "لم تبدأ", lobby: "بانتظار البدء", running: "جارية", finished: "انتهت" };
  const PHASES = { lobby: "بانتظار انضمام الطلاب", countdown: "عد تنازلي", question: "السؤال معروض", reveal: "عرض الإجابة", done: "انتهت المباراة" };

  /* ============================================================ live control */
  function scheduleLive() {
    if (pointerDown) { livePending = true; return; }
    renderLive();
  }

  function renderLive() {
    if (tab !== "live" || !data || !live) return;
    if (liveStop) { liveStop(); liveStop = null; }
    const root = $("#tab-live");
    const t = live.tournament;
    const side = h("div", { class: "stack" }, displayCard(), lateJoinCard(), onlineCard());
    if (!t) {
      render(root, h("div", { class: "live-grid" },
        h("div", { class: "empty" }, "لا توجد مسابقة مفعّلة. أنشئ مسابقة أو فعّلها من تبويب «الدوري والجولات».", h("br"), h("br"),
          h("button", { class: "btn", type: "button", text: "الذهاب إلى الدوري والجولات", onclick: () => selectTab("league") })),
        side));
      return;
    }
    render(root, h("div", { class: "live-grid" },
      h("div", { class: "stack" }, t.type === "knockout" ? knockoutLive(t) : separateLive(t)),
      side));
  }

  function knockoutLive(t) {
    const L = live.live;
    const out = [];
    out.push(h("div", { class: "sec-head" }, h("h2", { text: t.name }), h("span", { class: "chip navy", text: "خروج المغلوب · " + t.questionsPerMatch + " أسئلة لكل مباراة" })));
    if (L) out.push(liveMatchCard(L), rosterCard(L));
    else out.push(h("div", { class: "panel-card" }, h("b", { text: "لا توجد مباراة مفتوحة." }), " اختر مباراة من الجدول أدناه واضغط «فتح المباراة» ليبدأ الطلاب بالانضمام."));
    if (t.champion) out.push(h("div", { class: "panel-card", style: { textAlign: "center" } }, h("h2", {}, "البطل: ", h("span", { class: "num", text: t.champion.name }))));
    out.push(matchesCard(t, L));
    return out;
  }

  function liveMatchCard(L) {
    const [A, B] = L.sides;
    const actions = h("div", { class: "actions" });
    const add = (text, cls, fn) => actions.appendChild(h("button", { class: "btn " + cls, type: "button", text, onclick: fn }));
    let center = null;
    if (L.phase === "lobby") {
      add("ابدأ المباراة", "btn-gold btn-lg", () => act("match:start"));
      add("إلغاء المباراة", "btn-light", async () => { if (await confirmBox("إلغاء المباراة وإعادة الطلاب إلى الانتظار؟", { ok: "إلغاء المباراة", danger: true })) act("match:cancel"); });
      center = h("p", { class: "hint", text: "المتصلون الآن: " + A.name + " (" + A.online + ") — " + B.name + " (" + B.online + "). تأكد من أسماء الطلاب في القائمة قبل البدء." });
    } else if (L.phase === "done") {
      const R = L.result || {};
      center = h("div", { style: { textAlign: "center" } },
        h("h2", { text: R.winner ? "الفائز: " + R.winner : "تعادل تام" }),
        R.winner ? h("p", { class: "hint", text: "تأهل تلقائيًا إلى الجولة التالية." }) : h("p", { class: "hint", text: "حدّد الفائز يدويًا:" }));
      if (!R.winner) {
        add("فوز " + A.name, "btn-gold", () => act("match:setWinner", { matchId: L.matchId, classId: A.classId }, "تم تحديد الفائز"));
        add("فوز " + B.name, "btn-gold", () => act("match:setWinner", { matchId: L.matchId, classId: B.classId }, "تم تحديد الفائز"));
      }
      add("إغلاق شاشة النتيجة", "btn-light", () => act("match:close"));
    } else {
      if (L.phase === "question") add("كشف الإجابة الآن", "btn-gold btn-lg", () => act("match:reveal"));
      if (L.phase === "reveal") add(L.qIndex + 1 < L.qTotal ? "السؤال التالي" : "عرض النتيجة النهائية", "btn-gold btn-lg", () => act("match:next"));
      if (L.phase === "countdown") add("العد التنازلي جارٍ…", "btn-light", () => {});
      add("إنهاء واعتماد النتيجة", "btn-light", async () => {
        if (await confirmBox("إنهاء المباراة الآن واعتماد النتيجة بحسب الأسئلة المكتملة (" + (L.phase === "question" ? L.qIndex + 1 : L.qIndex + (L.phase === "reveal" ? 1 : 0)) + ")؟", { ok: "إنهاء واعتماد" })) act("match:close");
      });
      add("إلغاء المباراة", "btn-bad", async () => {
        if (await confirmBox("إلغاء المباراة وحذف نقاطها؟ ستعود المباراة إلى حالة «بانتظار اللعب».", { ok: "إلغاء المباراة", danger: true })) act("match:cancel");
      });
      const timer = h("div", { class: "big-timer num" });
      if (L.phase === "question") {
        const ms = Math.max(...L.sides.map((s) => (s.question ? s.question.remainingMs : 0)));
        liveStop = D.countdown(ms, (left) => { timer.textContent = Math.ceil(left / 1000) + " ث"; });
      } else if (L.phase === "countdown") {
        liveStop = D.countdown(L.remainingMs, (left) => { timer.textContent = Math.ceil(left / 1000); });
      }
      center = h("div", { style: { textAlign: "center" } },
        h("div", { class: "phase-pill", text: "السؤال " + (L.qIndex + 1) + " من " + L.qTotal }), " ", timer,
        L.phase !== "countdown" ? h("p", { class: "hint" },
          "أجاب من ", h("b", { class: "num", text: A.name }), ": ", h("b", { class: "num", text: (A.answered || 0) + "/" + (A.expected || 0) }),
          " — ومن ", h("b", { class: "num", text: B.name }), ": ", h("b", { class: "num", text: (B.answered || 0) + "/" + (B.expected || 0) })) : null);
    }
    const lq = live.liveQuestions;
    const preview = lq && L.phase !== "done" ? h("div", { class: "qpreview" }, L.sides.map((s) => lq[s.side] ? h("div", {},
      h("small", {}, h("span", { class: "num", text: s.name }), " · " + lq[s.side].time + " ث"), h("div", { text: lq[s.side].text }),
      h("div", {}, "الصحيحة: ", h("b", { text: lq[s.side].correct }))) : null)) : null;
    return h("div", { class: "panel-card live-hero" },
      h("div", { class: "sec-head" }, h("h2", { text: "المباراة الحالية — " + L.roundName }), h("span", { class: "phase-pill chip live", text: PHASES[L.phase] || L.phase })),
      h("div", { class: "versus-row" },
        h("div", { class: "t num" }, A.name, h("small", { text: "متوسط " + fmt.int(A.avg) })),
        h("span", { class: "vs", text: "ضد" }),
        h("div", { class: "t num" }, B.name, h("small", { text: "متوسط " + fmt.int(B.avg) }))),
      center, preview, actions);
  }

  function rosterCard(L) {
    return h("div", { class: "panel-card" },
      h("div", { class: "sec-head" }, h("h3", { text: "الطلاب المشاركون" }), h("span", { class: "hint", text: "⚠ = غادر الصفحة أثناء سؤال. يمكنك استبعاد أي اسم غير صحيح أو طالب من فصل آخر." })),
      h("div", { class: "grid-2" }, L.sides.map((s) => h("div", {},
        h("b", { class: "num", text: s.name + " (" + (s.roster || []).filter((r) => !r.kicked).length + ")" }),
        h("div", { class: "roster" }, (s.roster || []).length ? s.roster.map((r) => rosterRow(r, r.answeredNow ? "✓" : "", fmt.int(r.points)))
          : h("p", { class: "hint", text: "لم ينضم أحد بعد" }))))));
  }

  function rosterRow(r, mark, value) {
    return h("div", { class: "roster-row" + (r.kicked ? " kicked" : "") },
      h("span", { class: "dot" + (r.online ? " on" : ""), title: r.online ? "متصل" : "غير متصل" }),
      h("span", {}, r.name, r.leaves ? h("span", { class: "leaves", title: "غادر الصفحة أثناء السؤال " + r.leaves + " مرة", text: " ⚠ " + r.leaves }) : null),
      h("span", { class: "ans-ok", text: mark || "" }),
      h("span", { class: "num", text: value }),
      r.kicked ? h("span") : h("button", { class: "icon-btn danger", type: "button", title: "استبعاد", "aria-label": "استبعاد " + r.name, text: "✕", onclick: async () => {
        if (await confirmBox("استبعاد «" + r.name + "» من هذه المسابقة؟ لن تُحسب نقاطه لفصله.", { ok: "استبعاد", danger: true })) act("match:kick", { playerId: r.id }, "تم الاستبعاد");
      } }));
  }

  function matchesCard(t, L) {
    const busy = L && L.phase !== "done";
    return h("div", { class: "panel-card" },
      h("h3", { text: "جدول المباريات" }),
      h("div", { class: "match-list" }, t.rounds.map((r) => h("div", { class: "round-block" },
        h("h4", { text: r[0] ? r[0].roundName : "" }),
        h("div", { class: "round-matches" }, r.map((m) => {
          const name = (x) => h("span", { class: "num " + (m.winner ? (m.winner === x.id ? "w" : "l") : ""), text: x.name || "بانتظار الفائز" });
          const both = m.A.id && m.B.id;
          const btns = [];
          if (!m.bye && both && m.status === "pending" && !busy)
            btns.push(h("button", { class: "btn btn-sm btn-gold", type: "button", text: "فتح المباراة", onclick: () => act("match:open", { matchId: m.id }, "فُتحت المباراة") }));
          if (!m.bye && both && !(m.isLive && busy)) {
            for (const x of [m.A, m.B]) {
              if (m.winner === x.id) continue;
              btns.push(h("button", { class: "btn btn-sm btn-light", type: "button", text: "فوز " + x.name, onclick: async () => {
                if (await confirmBox("تحديد " + x.name + " فائزًا يدويًا في هذه المباراة؟", { ok: "تأكيد" })) act("match:setWinner", { matchId: m.id, classId: x.id }, "تم تحديد الفائز");
              } }));
            }
          }
          if (!m.bye && (m.status === "finished" || m.isLive))
            btns.push(h("button", { class: "btn btn-sm btn-light", type: "button", text: "إعادة ضبط", onclick: async () => {
              if (await confirmBox("إعادة ضبط المباراة وحذف نتيجتها لإعادة لعبها؟", { ok: "إعادة ضبط", danger: true })) act("match:reset", { matchId: m.id }, "أُعيد ضبط المباراة");
            } }));
          const status = m.bye ? "تأهل مباشر" : m.isLive ? "مفتوحة الآن" : { pending: both ? "بانتظار اللعب" : "بانتظار الطرفين", finished: m.tie ? "تعادل" : "انتهت" }[m.status] || m.status;
          return h("div", { class: "mcard" + (m.isLive ? " live" : "") + (m.status === "finished" ? " done" : "") },
            h("div", { class: "teams" }, name(m.A), m.bye ? null : [h("span", { class: "muted", text: "×" }), name(m.B)]),
            h("div", { class: "row" }, h("span", { class: "chip " + (m.isLive ? "live" : m.status === "finished" ? "ok" : ""), text: status }),
              m.A.avg != null && !m.bye ? h("span", { class: "hint num", text: fmt.int(m.A.avg) + " : " + fmt.int(m.B.avg) }) : null,
              m.decidedBy === "manual" ? h("span", { class: "hint", text: "(قرار يدوي)" }) : null),
            btns.length ? h("div", { class: "row" }, btns) : null);
        }))))));
  }

  function separateLive(t) {
    const rows = t.contests;
    for (const id of [...bulkSel]) if (!rows.some((r) => r.classId === id && r.status === "idle")) bulkSel.delete(id);
    const waiting = rows.filter((r) => r.status === "lobby").length;
    const table = h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["", "الفصل", "الحالة", "متصل", "أنهوا", "التقدم", "المتوسط /5", "الدقة", "الزمن", "الإجراءات"].map((x) => h("th", { text: x })))),
      h("tbody", {}, rows.map((r) => {
        const cb = h("input", { type: "checkbox", disabled: r.status !== "idle" });
        cb.checked = bulkSel.has(r.classId);
        cb.addEventListener("change", () => { cb.checked ? bulkSel.add(r.classId) : bulkSel.delete(r.classId); });
        const acts = [];
        const btn = (text, cls, fn) => acts.push(h("button", { class: "btn btn-sm " + cls, type: "button", text, onclick: fn }));
        const payload = { tournamentId: t.id, classIds: [r.classId] };
        if (r.status === "idle") btn("فتح", "btn-gold", () => act("classCompetition:open", payload, "فُتحت مسابقة " + r.name));
        if (r.status === "lobby") {
          btn("ابدأ", "btn-ok", () => act("classCompetition:start", payload));
          btn("إلغاء", "btn-light", () => act("classCompetition:finish", payload));
        }
        if (r.status === "running") btn("إنهاء الآن", "btn-warn", async () => {
          if (await confirmBox("إنهاء مسابقة " + r.name + " الآن؟ الأسئلة التي لم يجب عنها الطلاب تُحسب بلا نقاط.", { ok: "إنهاء" })) act("classCompetition:finish", payload, "انتهت المسابقة");
        });
        if (r.status === "finished") btn("إعادة ضبط", "btn-light", async () => {
          if (await confirmBox("حذف نتيجة " + r.name + " لإعادة المسابقة من جديد؟", { ok: "إعادة ضبط", danger: true })) act("classCompetition:reset", payload, "أُعيد الضبط");
        });
        const avg = r.status === "finished" ? r.avg : r.liveAvg;
        return h("tr", {},
          h("td", {}, cb),
          h("td", {}, h("b", { class: "num", text: r.name }), r.rank ? h("span", { class: "chip", text: "#" + r.rank, style: { marginInlineStart: "6px" } }) : null),
          h("td", {}, h("span", { class: "status-tag " + r.status, text: STATUS[r.status] + (r.interrupted ? " (توقف الخادم)" : "") })),
          h("td", { class: "num", text: r.status === "idle" ? "—" : String(r.online) }),
          h("td", { class: "num", text: r.status === "idle" ? "—" : r.finished + "/" + r.players }),
          h("td", {}, h("div", { class: "prog" }, h("i", { style: { width: Math.round((r.status === "finished" ? 1 : r.progress) * 100) + "%" } }))),
          h("td", { class: "num", text: r.status === "idle" ? "—" : fmt.dec(avg, 2) }),
          h("td", { class: "num", text: r.status === "idle" ? "—" : fmt.pct(r.accuracy) }),
          h("td", { class: "num", text: r.status === "idle" ? "—" : fmt.sec(r.avgTime) }),
          h("td", {}, h("div", { class: "row" }, acts)));
      }))));
    const active = rows.filter((r) => r.status === "lobby" || r.status === "running");
    return [
      h("div", { class: "sec-head" }, h("h2", { text: t.name }), h("span", { class: "chip navy", text: "مسابقات منفصلة" })),
      h("div", { class: "panel-card stack" },
        h("div", { class: "row" },
          h("button", { class: "btn btn-gold", type: "button", text: "فتح الفصول المحددة", onclick: () => {
            if (!bulkSel.size) { toast("حدّد فصلًا واحدًا على الأقل من العمود الأول", "warn"); return; }
            act("classCompetition:open", { tournamentId: t.id, classIds: [...bulkSel] }, "فُتحت الفصول المحددة").then((r) => { if (r) bulkSel.clear(); });
          } }),
          h("button", { class: "btn btn-ok", type: "button", text: "بدء جميع الفصول المنتظرة (" + waiting + ")", disabled: !waiting,
            onclick: () => act("classCompetition:start", { tournamentId: t.id }, "انطلقت المسابقات") })),
        h("p", { class: "hint", text: "افتح فصلًا أو أكثر ليتمكن طلابه من الانضمام، ثم اضغط «ابدأ». يتقدم كل طالب إلى سؤاله التالي فور إجابته، وتنتهي مسابقة الفصل تلقائيًا عندما ينهي جميع طلابه." }),
        table),
      active.length ? h("div", { class: "panel-card" },
        h("h3", { text: "الطلاب في المسابقات الجارية" }),
        h("div", { class: "grid-2" }, active.map((r) => h("div", {},
          h("b", { class: "num", text: r.name + " (" + (r.roster || []).filter((x) => !x.kicked).length + ")" }),
          h("div", { class: "roster" }, (r.roster || []).length
            ? r.roster.map((x) => rosterRow(x, x.finished ? "✓" : x.pos + "/" + r.questions, fmt.dec(x.points, 1)))
            : h("p", { class: "hint", text: "لم ينضم أحد بعد" })))))) : null,
    ];
  }

  function displayCard() {
    const mode = data.settings.displayMode;
    const modes = [["auto", "تلقائي"], ["join", "الانضمام"], ["leaderboard", "لوحة الصدارة"], ["bracket", "الشجرة / الترتيب"], ["idle", "الشعار"]];
    return h("div", { class: "panel-card stack" },
      h("div", { class: "sec-head" }, h("h3", { text: "شاشة العرض" }),
        h("span", { class: "chip " + (live.displays ? "ok" : "bad"), text: live.displays ? "متصلة" : "غير متصلة" })),
      h("div", { class: "seg" }, modes.map(([m, t]) => h("button", {
        type: "button", class: mode === m ? "on" : "", text: t, onclick: () => act("display:set", { mode: m }),
      }))),
      h("p", { class: "hint", text: "«تلقائي» يعرض المباراة الجارية أو ترتيب الفصول حسب الحالة." }),
      h("div", { class: "row" },
        h("button", { class: "btn btn-gold btn-sm", type: "button", text: "تشغيل الافتتاح على الشاشة", onclick: () => act("display:opening", {}, "بدأ الافتتاح") }),
        h("a", { class: "btn btn-light btn-sm", href: "/display", target: "_blank", rel: "noopener", text: "فتح الشاشة" })));
  }

  function lateJoinCard() {
    const cb = h("input", { type: "checkbox" });
    cb.checked = !!data.settings.allowLateJoin;
    cb.addEventListener("change", () => act("settings:lateJoin", { allow: cb.checked }, cb.checked ? "سُمح بالانضمام المتأخر" : "أُغلق الانضمام بعد البدء"));
    return h("div", { class: "panel-card stack" },
      h("h3", { text: "الانضمام بعد البدء" }),
      h("label", { class: "check" }, cb, "السماح لطلاب جدد بالانضمام بعد بدء المباراة أو المسابقة"),
      h("p", { class: "hint", text: "عند إيقافه (الوضع الأنسب لمنع الغش) لا ينضم بعد البدء إلا من كان في قائمة الانتظار." }));
  }

  function onlineCard() {
    const on = live.online || {};
    return h("div", { class: "panel-card stack" },
      h("h3", { text: "الطلاب المتصلون الآن (" + (live.onlineTotal || 0) + ")" }),
      data.classes.length ? h("div", { class: "online-list" }, data.classes.map((c) => h("span", { class: "chip " + (on[c.id] ? "navy" : ""), text: c.name + ": " + (on[c.id] || 0) })))
        : h("p", { class: "hint", text: "لا توجد فصول" }));
  }

  /* =============================================================== stats */
  function renderStats() {
    const s = data.stats;
    const rate = (r) => h("span", { class: "rate " + (r < 0.4 ? "low" : r < 0.7 ? "mid" : "high"), text: fmt.pct(r) });
    const table = (heads, rows) => rows.length
      ? h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, heads.map((x) => h("th", { text: x })))), h("tbody", {}, rows)))
      : h("div", { class: "empty", text: "لا توجد بيانات بعد" });
    render($("#tab-stats"), h("div", { class: "stack" },
      h("div", { class: "kpis" },
        h("div", { class: "kpi" }, h("b", { class: "num", text: s.totals.players }), h("span", { text: "طالبًا مسجّلًا" })),
        h("div", { class: "kpi" }, h("b", { class: "num", text: s.totals.answers }), h("span", { text: "إجابة مسجّلة" })),
        h("div", { class: "kpi" }, h("b", { class: "num", text: s.totals.questions }), h("span", { text: "سؤالًا في بنوك الفصول" })),
        h("div", { class: "kpi" }, h("b", { class: "num", text: s.history.length }), h("span", { text: "مباراة ومسابقة منتهية" }))),
      h("div", { class: "grid-2", style: { alignItems: "start" } },
        h("div", { class: "panel-card" }, h("h3", { text: "أفضل الطلاب" }),
          table(["#", "الطالب", "الفصل", "صحيحة", "الدقة", "نقاط الدوري", "نقاط المسابقات"], s.topStudents.map((p, i) => h("tr", {},
            h("td", { class: "num", text: i + 1 }), h("td", { text: p.name }), h("td", { class: "num", text: p.className }),
            h("td", { class: "num", text: p.correct + "/" + p.answered }), h("td", {}, rate(p.accuracy)),
            h("td", { class: "num", text: fmt.int(p.koPoints) }), h("td", { class: "num", text: fmt.dec(p.contestPoints, 1) }))))),
        h("div", { class: "panel-card" }, h("h3", { text: "الأسئلة الأصعب (أقل نسبة إجابة صحيحة)" }),
          table(["السؤال", "الفصل", "الإجابات", "الصحيحة"], s.hardest.map((q) => h("tr", {},
            h("td", { text: q.text }), h("td", { class: "num", text: q.className }), h("td", { class: "num", text: q.answered }), h("td", {}, rate(q.rate))))))),
      h("div", { class: "panel-card" }, h("h3", { text: "ترتيب الفصول" }),
        table(["الفصل", "مباريات", "فوز", "خسارة", "متوسط نقاط الدوري", "مسابقات منفصلة", "متوسطها /5"], s.standings.map((r) => h("tr", {},
          h("td", { class: "num", text: r.name }), h("td", { class: "num", text: r.koPlayed }), h("td", { class: "num", text: r.koWins }),
          h("td", { class: "num", text: r.koLosses }), h("td", { class: "num", text: r.koAvg == null ? "—" : fmt.int(r.koAvg) }),
          h("td", { class: "num", text: r.contests }), h("td", { class: "num", text: r.contestAvg == null ? "—" : fmt.dec(r.contestAvg, 2) }))))),
      h("div", { class: "panel-card" }, h("h3", { text: "سجل المباريات والمسابقات" }),
        table(["التاريخ", "المسابقة", "النوع", "التفاصيل", "النتيجة"], s.history.map((x) => h("tr", {},
          h("td", { text: fmt.date(x.at) }), h("td", { text: x.tournament }),
          h("td", { text: x.kind === "knockout" ? x.round : "فصول منفصلة" }),
          h("td", { text: x.kind === "knockout"
            ? (x.A && x.A.name) + " (" + fmt.int(x.A && x.A.avg) + ") × " + (x.B && x.B.name) + " (" + fmt.int(x.B && x.B.avg) + ")"
            : x.class + " — " + fmt.dec(x.avg, 2) + "/5 · دقة " + fmt.pct(x.accuracy) + " · " + (x.players || 0) + " طالب" }),
          h("td", { text: x.kind === "knockout" ? (x.winner ? "فوز " + x.winner + (x.decidedBy === "manual" ? " (يدوي)" : "") : "تعادل") : "—" }))))),
      h("div", { class: "panel-card danger-zone stack" },
        h("h3", { text: "إعادة الضبط" }),
        h("div", { class: "row" },
          h("button", { class: "btn btn-warn", type: "button", text: "تصفير الإحصائيات", onclick: async () => {
            if (await confirmBox("حذف جميع الإحصائيات وسجل المباريات؟ تبقى الفصول والأسئلة والمسابقات كما هي.", { ok: "تصفير", danger: true })) act("stats:reset", {}, "تم تصفير الإحصائيات");
          } }),
          h("button", { class: "btn btn-bad", type: "button", text: "حذف جميع البيانات", onclick: async () => {
            const ok = await confirmBox("سيُحذف كل شيء: الفصول، والأسئلة، والمسابقات، والطلاب، والإحصائيات. يُنصح بتنزيل نسخة احتياطية أولًا من «الإعدادات».", { title: "حذف جميع البيانات", ok: "حذف كل شيء", danger: true, typeToConfirm: "حذف الكل" });
            if (ok) { setDirty(false); draft = null; act("data:reset", { confirm: "RESET" }, "تم حذف جميع البيانات"); }
          } })),
        h("p", { class: "hint", text: "لا يمكن التراجع عن هذه العمليات." }))));
  }

  /* ============================================================ settings */
  function renderSettings() {
    const pw = data.password || {};
    const cur = h("input", { class: "input", type: "password", autocomplete: "current-password", dir: "ltr" });
    const nw = h("input", { class: "input", type: "password", autocomplete: "new-password", dir: "ltr" });
    const nw2 = h("input", { class: "input", type: "password", autocomplete: "new-password", dir: "ltr" });
    const fileIn = h("input", { type: "file", accept: "application/json,.json", hidden: true });
    fileIn.addEventListener("change", () => importBackup(fileIn));
    const linkBox = h("div", { class: "row" });
    D.joinUrl().then((u) => render(linkBox,
      h("img", { src: D.qrSrc(u), alt: "QR", width: 120, height: 120, style: { border: "1px solid var(--line)", borderRadius: "12px" } }),
      h("div", { class: "stack" },
        h("div", {}, "رابط الطلاب: ", h("b", { class: "num", text: u })),
        h("div", {}, "شاشة العرض: ", h("b", { class: "num", text: u.replace(/\/play$/, "/display") })),
        h("p", { class: "hint", text: "يجب أن تكون أجهزة الطلاب متصلة بالشبكة نفسها (Wi‑Fi) التي يعمل عليها هذا الجهاز." }))));
    render($("#tab-settings"), h("div", { class: "grid-2", style: { alignItems: "start" } },
      h("div", { class: "panel-card stack" },
        h("h3", { text: "تغيير كلمة المرور" }),
        pw.source === "env"
          ? h("p", { class: "hint", text: "كلمة المرور محددة من متغير البيئة ADMIN_PASSWORD على الخادم، ولا يمكن تغييرها من هنا." })
          : h("form", { class: "stack", onsubmit: async (e) => {
              e.preventDefault();
              if (nw.value !== nw2.value) { toast("كلمتا المرور الجديدتان غير متطابقتين", "bad"); return; }
              const r = await act("admin:password", { current: cur.value, new: nw.value }, "تم تغيير كلمة المرور");
              if (r) { cur.value = nw.value = nw2.value = ""; }
            } },
            h("label", { class: "field" }, h("span", { text: "كلمة المرور الحالية" }), cur),
            h("label", { class: "field" }, h("span", { text: "كلمة المرور الجديدة (6 أحرف على الأقل)" }), nw),
            h("label", { class: "field" }, h("span", { text: "تأكيد كلمة المرور الجديدة" }), nw2),
            h("button", { class: "btn", type: "submit", text: "حفظ كلمة المرور" }))),
      h("div", { class: "stack" },
        h("div", { class: "panel-card stack" },
          h("h3", { text: "النسخ الاحتياطي" }),
          h("p", { class: "hint", text: "نزّل نسخة من كل البيانات (الفصول، الأسئلة، المسابقات، النتائج) واحتفظ بها، خاصة عند التشغيل على خادم مجاني قد يُعاد تشغيله." }),
          h("div", { class: "row" },
            h("button", { class: "btn btn-gold", type: "button", text: "تنزيل نسخة احتياطية", onclick: exportBackup }),
            h("button", { class: "btn btn-light", type: "button", text: "استعادة من ملف…", onclick: () => fileIn.click() }), fileIn)),
        h("div", { class: "panel-card stack" }, h("h3", { text: "روابط الدخول" }), linkBox),
        h("div", { class: "panel-card stack" },
          h("h3", { text: "الخروج" }),
          h("button", { class: "btn btn-light", type: "button", text: "تسجيل الخروج من لوحة التحكم", onclick: async () => {
            await act("admin:logout");
            logoutLocal();
            showLogin("تم تسجيل الخروج");
          } })))));
  }

  async function exportBackup() {
    try {
      const r = await fetch("/api/admin/export", { headers: { "X-Admin-Token": token || "" }, cache: "no-store" });
      if (!r.ok) throw new Error();
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
      const a = h("a", { href: url, download: "difaf-backup-" + stamp + ".json" });
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      toast("تم تنزيل النسخة الاحتياطية", "ok");
    } catch (e) { toast("تعذّر تنزيل النسخة الاحتياطية", "bad"); }
  }

  async function importBackup(input) {
    const f = input.files && input.files[0];
    input.value = "";
    if (!f) return;
    const ok = await confirmBox("ستُستبدل جميع البيانات الحالية بمحتوى الملف «" + f.name + "». متابعة؟", { title: "استعادة نسخة احتياطية", ok: "استعادة", danger: true });
    if (!ok) return;
    try {
      const r = await fetch("/api/admin/import", { method: "POST", headers: { "X-Admin-Token": token || "", "Content-Type": "application/json" }, body: await f.text() });
      const res = await r.json();
      if (!res.ok) { toast(res.error || "تعذّرت الاستعادة", "bad"); return; }
      setDirty(false);
      draft = null;
      toast("تمت الاستعادة (" + res.classes + " فصلًا)", "ok");
    } catch (e) { toast("تعذّرت قراءة الملف", "bad"); }
  }
})();
