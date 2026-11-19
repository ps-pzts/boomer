// DOM and formatting helpers. Nothing here ever uses innerHTML: symbols, rules and
// reasons come from the database, so every string goes in through textContent.

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const NS = "http://www.w3.org/2000/svg";
export function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}

const MINUS = "−";
export function money(x, { sign = true } = {}) {
  const v = Number(x || 0);
  if (Math.abs(v) < 0.5) return "₹0";
  const body = "₹" + Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  if (!sign) return v < 0 ? MINUS + body : body;
  return (v > 0 ? "+" : MINUS) + body;
}

export function price(x) {
  return x == null ? "–" : Number(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export const tone = (x) => (Number(x) > 0.5 ? "gain" : Number(x) < -0.5 ? "loss" : "");
export const arrow = (x) => (Number(x) > 0.5 ? "▲" : Number(x) < -0.5 ? "▼" : "●");

export function clock(ts) {
  return ts ? String(ts).slice(11, 19) : "";
}

export function ago(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

export function shortDate(day) {
  const d = new Date(day + "T00:00:00");
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

// session and trade state -> [tone, glyph, label]
export const SESSION = {
  trading: ["good", "●", "Trading"],
  halted: ["critical", "✕", "Halted"],
  reconciling: ["warn", "↻", "Reconciling"],
  no_new_entries: ["warn", "◑", "No new entries"],
  squaring_off: ["warn", "⇥", "Squaring off"],
  closed: ["", "○", "Closed"],
  abandoned: ["", "○", "Abandoned"],
  pre_open: ["", "○", "Pre-open"],
  connected: ["", "○", "Connected"],
  ready: ["", "○", "Ready"],
};
export const TRADE = {
  entry_pending: ["info", "Waiting to fill"],
  protecting: ["warn", "Placing stop"],
  open: ["good", "Open"],
  exiting: ["warn", "Exiting"],
  closed: ["", "Closed"],
  cancelled: ["", "Cancelled"],
};

export function pill(tone_, text) {
  return h("span", { class: "pill " + (tone_ || "") }, text);
}

export function empty(text) {
  return h("p", { class: "empty" }, text);
}

export function theme() {
  const root = document.documentElement;
  let saved = null;
  try { saved = localStorage.getItem("boomer-theme"); } catch { /* storage may be blocked */ }
  if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  document.getElementById("theme").addEventListener("click", () => {
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("boomer-theme", root.dataset.theme); } catch { /* ignore */ }
  });
}
