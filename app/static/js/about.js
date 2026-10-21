/* About page: texts from the teacher's settings + gentle scroll animations. */
(function () {
  "use strict";
  const { $$, h, render } = window.Difaf;
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const LETTERS = "ضادعربيةنحوصرفبلاغةأدبإملاءقراءة";
  $$(".letters").forEach((box) => {
    if (reduced) return;
    for (let i = 0; i < 18; i++) {
      const el = h("i", { text: LETTERS[Math.floor(Math.random() * LETTERS.length)] });
      const style = {
        left: (Math.random() * 100).toFixed(1) + "%", "font-size": (1.4 + Math.random() * 3.6).toFixed(2) + "rem",
        "--dur": (14 + Math.random() * 16).toFixed(1) + "s", "--delay": (-Math.random() * 30).toFixed(1) + "s",
        "--o": (0.08 + Math.random() * 0.16).toFixed(2),
      };
      for (const [k, v] of Object.entries(style)) el.style.setProperty(k, v);
      box.appendChild(el);
    }
  });

  const items = $$(".reveal");
  if (reduced || !("IntersectionObserver" in window)) items.forEach((el) => el.classList.add("in"));
  else {
    const io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }), { threshold: 0.12 });
    items.forEach((el) => io.observe(el));
  }

  fetch("/api/site", { cache: "no-store" }).then((r) => r.json()).then((data) => {
    const s = data.site || {};
    $$("[data-site]").forEach((el) => { const v = s[el.dataset.site]; if (typeof v === "string" && v) el.textContent = v; });
    $$("[data-site-list]").forEach((el) => {
      const list = s[el.dataset.siteList];
      if (Array.isArray(list) && list.length) render(el, list.map((g) => h("li", { text: g })));
    });
  }).catch(() => { /* keep built-in texts */ });
})();
