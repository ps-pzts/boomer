import { connect, get } from "./api.js";
import * as views from "./views.js";
import { h, empty, theme } from "./ui.js";

const view = document.getElementById("view");
const ui = { filter: "all", skew: 0, render: () => render(), live: null };
let route = location.hash.slice(1) || "live";
let seq = 0;

function setTab(name) {
  document.querySelectorAll(".tabs a").forEach((a) => (a.dataset.tab === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
}

function show(nodes) {
  view.replaceChildren(...nodes.flat().filter(Boolean));
}

async function render() {
  const mine = ++seq;
  const [page, arg] = [route.split("-")[0], route.slice(route.indexOf("-") + 1)];
  setTab(page === "trade" ? "trades" : page === "day" ? "days" : page);
  try {
    let nodes;
    if (page === "live" || page === "trades") {
      if (!ui.live) { show([empty("Loading…")]); return; }
      nodes = page === "live" ? views.live(ui.live, ui) : views.trades(ui.live);
    } else if (page === "days") nodes = await views.days();
    else if (page === "trade") nodes = await views.tradeDetail(arg);
    else if (page === "day") nodes = await views.dayDetail(arg);
    else nodes = [empty("Unknown page.")];
    if (mine === seq) show(nodes);
  } catch (e) {
    if (mine === seq) show([h("section", { class: "card" }, h("h2", {}, "Could not load"), h("p", {}, "Check the connection and try again."), h("button", { class: "btn", type: "button", onclick: render }, "Retry"))]);
  }
}

function status(kind) {
  const el = document.getElementById("link");
  el.className = "link " + kind;
  document.getElementById("link-text").textContent = { live: "Live", poll: "Polling", off: "Offline" }[kind];
}

function onData(d) {
  ui.live = d;
  ui.skew = 0;
  ui.at = Date.now();
  const mode = document.getElementById("mode");
  const m = d.session && d.session.mode;
  mode.hidden = !m;
  if (m) { mode.textContent = m.toUpperCase(); mode.className = "mode " + m; }
  if (route === "live" || route === "trades") render();
}

theme();
addEventListener("hashchange", () => { route = location.hash.slice(1) || "live"; window.scrollTo(0, 0); render(); });
render();
connect({ onData, onStatus: status });
// between pushes the ages on screen keep counting (a quiet system is not a frozen page)
setInterval(() => {
  if (!ui.live || route !== "live") return;
  ui.skew = Math.round((Date.now() - ui.at) / 1000);
  if (ui.skew % 5 === 0) render();
}, 1000);
