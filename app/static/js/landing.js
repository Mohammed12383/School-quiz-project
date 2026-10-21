/* Main page: gate → welcome → home, live content from /api/site, scroll animations. */
(function () {
  "use strict";
  const D = window.Difaf;
  const { $, $$, h, render, Sound, confetti, fmt } = D;
  const gate = $("#gate"), welcome = $("#welcome"), home = $("#home");
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let stage = "gate";

  /* ------------------------------------------------ floating Arabic letters */
  const LETTERS = "ضادعربيةنحوصرفبلاغةأدبإملاءقراءةكتبسمعلم";
  // custom properties (--dur, --delay…) need style.setProperty
  function letter(text, style) {
    const el = h("i", { text });
    for (const [k, v] of Object.entries(style)) el.style.setProperty(k, v);
    return el;
  }
  function letters(box, count) {
    if (reduced || !box) return;
    const frag = document.createDocumentFragment();
    for (let i = 0; i < count; i++) {
      frag.appendChild(letter(LETTERS[Math.floor(Math.random() * LETTERS.length)], {
        left: (Math.random() * 100).toFixed(1) + "%", "font-size": (1.4 + Math.random() * 4.2).toFixed(2) + "rem",
        "--dur": (14 + Math.random() * 18).toFixed(1) + "s", "--delay": (-Math.random() * 30).toFixed(1) + "s",
        "--o": (0.08 + Math.random() * 0.18).toFixed(2),
        "--r0": (Math.random() * 40 - 20).toFixed(0) + "deg", "--r1": (Math.random() * 40 - 20).toFixed(0) + "deg",
      }));
    }
    box.appendChild(frag);
  }
  $$(".letters").forEach((box) => letters(box, box.closest(".hero") ? 26 : box.classList.contains("letters-soft") ? 14 : 22));

  /* -------------------------------------------------------------- stages */
  function showHome(instant) {
    stage = "home";
    gate.hidden = true;
    welcome.hidden = true;
    home.hidden = false;
    if (!instant) home.classList.add("enter");
    if (location.hash !== "#home") history.replaceState(null, "", "#home");
    window.scrollTo(0, 0);
    observeReveals();
    onScroll();
  }

  function openBook() {
    if (stage !== "gate") return;
    stage = "opening";
    Sound.unlock();
    Sound.reveal();
    gate.classList.add("open");
    setTimeout(() => {
      welcome.hidden = false;
      welcome.classList.add("enter");
      Sound.fanfare();
      confetti({ count: 220 });
      stage = "welcome";
      $("#continueBtn").focus();
    }, 700);
    setTimeout(() => { gate.hidden = true; }, 1350);
  }

  $("#openBtn").addEventListener("click", openBook);
  $("#continueBtn").addEventListener("click", () => showHome(false));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.repeat) return;
    if (stage === "welcome") { e.preventDefault(); showHome(false); }
  });

  /* --------------------------------------------------------- navigation */
  const nav = $("#nav");
  function onScroll() { nav.classList.toggle("scrolled", window.scrollY > 40); }
  window.addEventListener("scroll", onScroll, { passive: true });

  /* ------------------------------------------------------ scroll reveal */
  let observer = null;
  function observeReveals() {
    const items = $$(".reveal:not(.in)", home);
    if (reduced || !("IntersectionObserver" in window)) { items.forEach((el) => el.classList.add("in")); countUp(true); return; }
    if (!observer) {
      observer = new IntersectionObserver((entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          e.target.classList.add("in");
          observer.unobserve(e.target);
          if (e.target.closest("#stats")) countUp(false);
        }
      }, { threshold: 0.15, rootMargin: "0px 0px -40px 0px" });
    }
    items.forEach((el) => observer.observe(el));
  }

  /* ------------------------------------------------------ live content */
  let site = null;
  let counted = false;
  function countUp(instant) {
    if (counted || !site) return;
    counted = true;
    $$("[data-count]").forEach((el) => {
      const target = Number(site.stats[el.dataset.count]) || 0;
      if (instant || reduced) { el.textContent = fmt.int(target); return; }
      const start = performance.now(), dur = 1400;
      const step = (t) => {
        const p = Math.min(1, (t - start) / dur);
        el.textContent = fmt.int(target * (1 - Math.pow(1 - p, 3)));
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }

  function cup() {
    const NS = "http://www.w3.org/2000/svg";
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 64 64");
    s.setAttribute("class", "cup");
    s.setAttribute("aria-hidden", "true");
    for (const [d, f] of [["M18 6h28v16a14 14 0 0 1-28 0z", "#ddc08b"], ["M27 36h10v10H27zM18 48h28v9H18z", "#b38841"]]) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", f);
      s.appendChild(p);
    }
    return s;
  }

  function applySite(data) {
    site = data;
    const s = data.site || {};
    $$("[data-site]").forEach((el) => {
      const v = s[el.dataset.site];
      if (typeof v === "string" && v) el.textContent = v;
    });
    $$("[data-site-list]").forEach((el) => {
      const list = s[el.dataset.siteList];
      if (Array.isArray(list) && list.length) render(el, list.map((g) => h("li", { text: g })));
    });
    if (s.programName) document.title = s.programName + " | " + (s.schoolName || "");

    const ann = $("#announce");
    ann.hidden = !s.announcement;
    ann.textContent = s.announcement || "";

    const badge = $("#liveBadge");
    const L = data.live;
    if (L) {
      badge.hidden = false;
      badge.textContent = L.kind === "knockout"
        ? "مباشر الآن: " + L.round + " — " + L.A + " ضد " + L.B + " · انضم ←"
        : "مباشر الآن: " + L.title + " (" + L.classes.join("، ") + ") · انضم ←";
    } else badge.hidden = true;

    const st = data.stats || {};
    $("#stats").hidden = !(st.classes > 0);
    if (counted) $$("[data-count]").forEach((el) => { el.textContent = fmt.int(st[el.dataset.count] || 0); });

    const champs = [...(data.champions || []).map((c) => ({ ...c, kind: "بطل" })),
      ...(data.contestLeaders || []).map((c) => ({ ...c, kind: "الأول في" }))];
    render($("#champions"), champs.length
      ? champs.map((c) => h("li", {}, cup(), h("span", {}, h("b", { class: "num", text: c.name }), h("small", { text: c.kind + " «" + c.tournament + "»" }))))
      : h("li", { class: "empty-fame", text: "لم يُتوَّج أي فصل بعد… كن أول الأبطال!" }));

    const top = data.topStudents || [];
    const podium = $("#podium");
    if (top.length) {
      const order = [top[1], top[0], top[2]];
      const cls = ["p2", "p1", "p3"];
      render(podium, order.map((p, i) => p ? h("div", { class: "p " + cls[i] },
        h("b", { text: p.name }), h("small", { class: "num", text: p.className + " · " + p.correct + " صحيحة" }),
        h("div", { class: "bar num", text: cls[i].slice(1) })) : h("div")));
    } else {
      render(podium, h("p", { class: "empty-fame", text: "تنتظر المنصّة أول المتفوقين!" }));
    }
    let more = $(".more-students");
    if (!more) { more = h("ul", { class: "more-students" }); podium.parentNode.appendChild(more); }
    render(more, top.slice(3).map((p, i) => h("li", { text: (i + 4) + ". " + p.name + " (" + p.className + ")" })));
    if (stage === "home") observeReveals();
  }

  async function loadSite() {
    try {
      const r = await fetch("/api/site", { cache: "no-store" });
      if (r.ok) applySite(await r.json());
    } catch (e) { /* offline: keep the built-in texts */ }
  }
  loadSite();
  setInterval(() => { if (!document.hidden) loadSite(); }, 15000);

  if (location.hash === "#home" || location.hash === "#fields" || location.hash === "#how" || location.hash === "#fame") {
    const target = location.hash;
    showHome(true);
    if (target !== "#home") setTimeout(() => { const el = $(target); if (el) el.scrollIntoView(); }, 50);
  } else {
    $("#openBtn").focus();
  }
})();
