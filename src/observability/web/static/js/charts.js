// Column charts drawn to the dataviz marks spec: bars <= 24px with a 4px rounded data end,
// one zero baseline, hairline grid, the value at the tip, polarity in two hues (blue gain,
// red loss) AND a sign in the label, never colour alone. A tooltip and a table view carry
// every value, so nothing depends on hover.
import { h, svg, money, tone } from "./ui.js";

const BAR = 22, GAP = 8, PAD = { t: 22, r: 8, b: 30, l: 8 };

function niceMax(v) {
  const m = Math.max(1, v);
  const p = Math.pow(10, Math.floor(Math.log10(m)));
  return [1, 2, 5, 10].map((k) => k * p).find((x) => x >= m);
}

export function columns({ items, onPick, height = 150 }) {
  // items: [{label, value, sub, ref}]
  const wrap = h("div", { class: "chart" });
  if (!items.length) return wrap;
  const width = Math.max(300, 24 + items.length * (BAR + GAP));
  const top = Math.max(0, ...items.map((i) => i.value));
  const bot = Math.min(0, ...items.map((i) => i.value));
  const span = niceMax(Math.max(top, -bot, 1));
  const innerH = height - PAD.t - PAD.b;
  const hasNeg = bot < 0;
  const zeroY = PAD.t + (hasNeg ? innerH / 2 : innerH);
  const scale = (hasNeg ? innerH / 2 : innerH) / span;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Column chart" });
  for (const frac of hasNeg ? [-1, 0, 1] : [0, 1]) {
    const y = zeroY - frac * span * scale;
    root.append(svg("line", { x1: PAD.l, x2: width - PAD.r, y1: y, y2: y, class: frac === 0 ? "base" : "grid" }));
  }
  const slot = (width - PAD.l - PAD.r) / items.length;
  const tip = h("div", { class: "tip", hidden: true });
  const show = (i, x) => {
    tip.replaceChildren(h("strong", { class: tone(i.value) }, money(i.value)), h("span", {}, i.label), i.sub && h("div", { class: "muted" }, i.sub));
    tip.hidden = false;
    const w = wrap.clientWidth || width;
    tip.style.left = Math.min(Math.max(4, (x / width) * w - 60), w - 190) + "px";
    tip.style.top = "0px";
  };
  items.forEach((i, n) => {
    const cx = PAD.l + slot * n + slot / 2;
    const len = Math.max(2, Math.abs(i.value) * scale);
    const up = i.value >= 0;
    const y = up ? zeroY - len : zeroY;
    const bar = svg("rect", {
      x: cx - Math.min(BAR, slot - 4) / 2, y, width: Math.min(BAR, slot - 4), height: len, rx: 4,
      fill: up ? "var(--gain)" : "var(--loss)", class: "bar", tabindex: 0, role: "button",
      "aria-label": `${i.label}: ${money(i.value)}`,
    });
    const pick = () => (onPick ? onPick(i) : null);
    bar.addEventListener("pointerenter", () => show(i, cx));
    bar.addEventListener("focus", () => show(i, cx));
    bar.addEventListener("pointerleave", () => (tip.hidden = true));
    bar.addEventListener("blur", () => (tip.hidden = true));
    bar.addEventListener("click", () => { show(i, cx); pick(); });
    root.append(bar);
    // value at the tip (outside the bar), label under the baseline
    root.append(svg("text", { x: cx, y: up ? y - 5 : y + len + 12, "text-anchor": "middle", class: "val", text: money(i.value) }));
    root.append(svg("text", { x: cx, y: height - 8, "text-anchor": "middle", text: i.label }));
  });
  wrap.append(root, tip);
  return wrap;
}

// A table behind a toggle: the same numbers without hover.
export function withTable({ title, chart, columnsHead, rows }) {
  const table = h("div", { class: "scroll-x", hidden: true },
    h("table", { class: "data" },
      h("thead", {}, h("tr", {}, columnsHead.map((c) => h("th", {}, c)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", { class: "num" }, c)))))));
  const btn = h("button", { class: "btn", type: "button", "aria-pressed": "false" }, "Show table");
  btn.addEventListener("click", () => {
    const on = table.hidden;
    table.hidden = !on;
    chart.hidden = on;
    btn.textContent = on ? "Show chart" : "Show table";
    btn.setAttribute("aria-pressed", String(on));
  });
  return h("div", { class: "card" }, h("div", { class: "row" }, h("h2", {}, title), btn), chart, table);
}
