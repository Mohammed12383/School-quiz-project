(function () {
  "use strict";
  const { $, Sound, confetti } = window.Difaf;
  const gate = $("#gate"), welcome = $("#welcome"), home = $("#home");
  let stage = "gate";

  function showHome(instant) {
    stage = "home";
    gate.hidden = true;
    welcome.hidden = true;
    home.hidden = false;
    if (!instant) home.classList.add("enter");
    if (location.hash !== "#home") history.replaceState(null, "", "#home");
    window.scrollTo(0, 0);
  }

  function openBook() {
    if (stage !== "gate") return;
    stage = "opening";
    Sound.unlock();
    gate.classList.add("open");
    setTimeout(() => {
      welcome.hidden = false;
      welcome.classList.add("enter");
      Sound.fanfare();
      confetti({ count: 200 });
      stage = "welcome";
      $("#continueBtn").focus();
    }, 650);
    setTimeout(() => { gate.hidden = true; }, 1300);
  }

  $("#openBtn").addEventListener("click", openBook);
  $("#continueBtn").addEventListener("click", () => showHome(false));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.repeat) return;
    if (stage === "welcome") { e.preventDefault(); showHome(false); }
  });

  if (location.hash === "#home") showHome(true);
  else $("#openBtn").focus();
})();
