import { columns, withTable } from "./charts.js";
import { get } from "./api.js";
import { SESSION, TRADE, ago, arrow, clock, empty, h, money, pill, price, shortDate, tone } from "./ui.js";

const MAX_OPEN = 3; // decision_cycle.MAX_OPEN_TRADES

// ── shared pieces ─────────────────────────────────────────────────────────────

function kpi(label, value, sub, cls = "", wide = false) {
  return h("div", { class: "kpi" + (wide ? " wide" : "") },
    h("span", { class: "kpi-label" }, label),
    h("span", { class: "kpi-value num " + cls }, value),
    sub && h("span", { class: "kpi-sub" }, sub));
}

function ladder(t) {
  // stop .. target on one line, with entry and the last price placed on the same scale
  const lo = Math.min(t.stop, t.entry, t.last ?? t.entry), hi = Math.max(t.target, t.entry, t.last ?? t.entry);
  const pos = (p) => ((p - lo) / Math.max(hi - lo, 0.01)) * 100 + "%";
  const tick = (cls, p, label) => h("span", { class: "tick " + cls, style: { left: pos(p) } }, h("span", {}, label));
  return h("div", { class: "ladder", role: "img", "aria-label": `Stop ${t.stop}, entry ${t.entry}, target ${t.target}` },
    h("span", { class: "track" }),
    tick("stop", t.stop, "Stop"), tick("", t.entry, "Entry"), tick("target", t.target, "Target"),
    t.last != null && h("span", { class: "last", style: { left: pos(t.last) } }));
}

function tradeCard(t) {
  const [tn, label] = TRADE[t.state] || ["", t.state];
  const live = t.last != null;
  const pnl = t.state === "closed" ? t.realised : t.unrealised;
  return h("a", { class: "card trade", href: "#trade-" + t.id },
    h("div", { class: "trade-head" },
      h("span", {}, h("span", { class: "sym" }, t.symbol), h("span", { class: "muted small" }, `  ${t.quantity} shares`)),
      pill(tn, label)),
    (live || t.state === "protecting" || t.state === "exiting" || t.state === "open") && ladder(t),
    h("div", { class: "ladder-nums num" },
      h("span", {}, live ? `Last ${price(t.last)}` : `Entry ${price(t.entry)}`),
      pnl != null && h("span", { class: tone(pnl) }, `${arrow(pnl)} ${money(pnl)}`),
      t.state === "open" && h("span", { class: "prot " + (t.protected ? "yes" : "no") }, t.protected ? "✓ Stop confirmed" : "! No stop"),
      t.state === "closed" && h("span", { class: "muted" }, (t.exit_reason || "").replaceAll("_", " "))));
}

function stream(events, filter) {
  const rows = events.filter((e) => filter === "all" || e.group === filter);
  if (!rows.length) return empty("Nothing yet.");
  return h("div", { class: "list" }, rows.map((e) => {
    const glyph = e.severity === "critical" ? "!" : e.severity === "warn" ? "▲" : e.group === "trade" ? "●" : "○";
    const body = [
      h("span", { class: "glyph", "aria-hidden": "true" }, glyph),
      h("span", { class: "item-main" },
        h("span", { class: "item-title" }, (e.symbol ? e.symbol + ": " : "") + e.title),
        e.detail && h("span", { class: "small muted" }, e.detail)),
      h("span", { class: "item-time num" }, clock(e.ts)),
    ];
    const cls = "item " + (e.severity === "critical" ? "critical" : e.severity === "warn" ? "warn" : "");
    return e.trade_id ? h("a", { class: cls, href: "#trade-" + e.trade_id }, body) : h("div", { class: cls }, body);
  }));
}

// ── live ──────────────────────────────────────────────────────────────────────

function waiting(d) {
  return [h("section", { class: "card hero" }, h("div", { class: "hero-state" }, "Waiting for data"),
    h("p", { class: "why" }, "The database is not ready yet. Start the orchestrator (./dev.sh); this page fills in by itself.")),
    h("p", { class: "small muted" }, d.reason || "")];
}

export function live(d, ui) {
  if (d.ready === false) return waiting(d);
  const s = d.session;
  const [stTone, glyph, stLabel] = s ? SESSION[s.state] || ["", "○", s.state] : ["", "○", "No session yet"];
  const k = d.kpis;
  const why = s && s.halt_reason
    ? `Halted: ${s.halt_reason.replaceAll("_", " ")}`
    : d.bot_mode !== "auto" ? `Bot mode: ${d.bot_mode.replaceAll("_", " ")}` : "";
  const failed = (s && s.halt_detail && s.halt_detail.failed_checks) || [];

  const view = [
    h("section", { class: "card hero " + stTone },
      h("div", { class: "hero-state" }, h("span", { class: "glyph", "aria-hidden": "true" }, glyph), stLabel),
      why && h("p", { class: "why" }, why),
      failed.length > 0 && h("p", { class: "why" }, "Failed checks: " + failed.join(", ")),
      s && s.halt_reason === "reconcile_mismatch" && h("p", { class: "why" }, s.halt_acked ? "Acknowledged. Waiting for a clean reconcile." : "Fix it at the broker, then send /ack in Telegram.")),
  ];
  if (d.attention.length) {
    view.push(h("section", { class: "attention", "aria-label": "Needs attention" }, d.attention.map((a) => {
      const href = a.kind === "trade" || (a.kind === "event" && a.ref) ? "#trade-" + a.ref : null;
      const body = [h("span", { class: "glyph", "aria-hidden": "true" }, a.severity === "critical" ? "!" : "▲"), h("span", {}, a.text)];
      return href ? h("a", { class: "alert " + a.severity, href }, body) : h("div", { class: "alert " + a.severity }, body);
    })));
  }
  view.push(h("section", { class: "kpis" },
    kpi("Realised today", `${arrow(k.realised)} ${money(k.realised)}`, `${k.closed} closed: ${k.wins} won, ${k.losses} lost`, tone(k.realised), true),
    kpi("Unrealised", money(k.unrealised), `${k.active} active`, tone(k.unrealised)),
    kpi("Open slots", `${k.active} / ${MAX_OPEN}`, k.active >= MAX_OPEN ? "Full: no new entries" : "Room for entries"),
  ));
  const active = d.trades.filter((t) => ["entry_pending", "protecting", "open", "exiting"].includes(t.state));
  view.push(h("h2", { class: "section-title" }, "Active trades"));
  view.push(active.length ? h("div", { class: "list-gap" }, active.map(tradeCard)) : empty("No active trades."));

  const closed = d.pnl;
  if (closed.length) {
    const chart = columns({ items: closed.map((p) => ({ label: p.symbol, value: p.pnl, sub: `Day total ${money(p.cum)}`, ref: p })) });
    view.push(withTable({ title: "P&L per closed trade", chart, columnsHead: ["Time", "Symbol", "P&L", "Day"],
      rows: closed.map((p) => [clock(p.ts), p.symbol, money(p.pnl), money(p.cum)]) }));
  }

  view.push(h("section", { class: "card" }, h("h2", {}, "Components"),
    d.components.length ? h("div", { class: "list" }, d.components.map((c) => {
      const tn = c.status === "silent" ? "critical" : "";
      const age = c.age_s + ui.skew;
      return h("div", { class: "item " + tn },
        h("span", { class: "glyph", "aria-hidden": "true" }, c.status === "alive" ? "●" : c.status === "silent" ? "!" : "○"),
        h("span", { class: "item-main" }, h("span", {}, c.source),
          h("span", { class: "small muted" }, c.status === "idle" ? "idle outside market hours" : `last beat ${ago(age)} ago`)),
        h("span", { class: "comp-age " + c.status }, c.status));
    })) : empty("No heartbeats yet. Start the orchestrator.")));

  const chips = ["all", "trade", "session", "alert"].map((g) =>
    h("button", { class: "chip", type: "button", "aria-pressed": String(ui.filter === g), onclick: () => { ui.filter = g; ui.render(); } },
      { all: "All", trade: "Trades", session: "Session", alert: "Alerts" }[g]));
  view.push(h("section", { class: "card" }, h("h2", {}, "Events"), h("div", { class: "chips" }, chips), stream(d.stream, ui.filter)));
  return view;
}

// ── trades tab ────────────────────────────────────────────────────────────────

export function trades(d) {
  if (d.ready === false) return waiting(d);
  if (!d.trades.length) return [empty("No trades today.")];
  const order = { open: 0, protecting: 0, exiting: 0, entry_pending: 1, closed: 2, cancelled: 3 };
  return [h("h2", { class: "section-title" }, `Today: ${d.trades.length} trades`),
    h("div", { class: "list-gap" }, [...d.trades].sort((a, b) => order[a.state] - order[b.state] || (b.created_at > a.created_at ? 1 : -1)).map(tradeCard))];
}

// ── days tab ──────────────────────────────────────────────────────────────────

export async function days() {
  const rows = await get("/api/days?n=14");
  if (!rows.length) return [empty("No trading days recorded yet.")];
  const asc = [...rows].reverse();
  const chart = columns({ items: asc.map((r) => ({ label: shortDate(r.day), value: r.realised, sub: `${r.closed} closed, ${r.wins}W/${r.losses}L`, ref: r })), onPick: (i) => (location.hash = "#day-" + i.ref.day) });
  return [
    withTable({ title: "Realised P&L by day", chart, columnsHead: ["Day", "Mode", "Trades", "W/L", "P&L"],
      rows: rows.map((r) => [shortDate(r.day), (r.mode || "").toUpperCase(), r.trades, `${r.wins}/${r.losses}`, money(r.realised)]) }),
    h("section", { class: "card" }, h("h2", {}, "Sessions"), h("div", { class: "list" }, rows.map((r) =>
      h("a", { class: "item", href: "#day-" + r.day },
        h("span", { class: "item-main" }, h("span", {}, shortDate(r.day) + "  " + (r.mode || "").toUpperCase()),
          h("span", { class: "small muted" }, `${r.trades} trades, ${r.wins} won, ${r.losses} lost`)),
        h("span", { class: "num " + tone(r.realised) }, `${arrow(r.realised)} ${money(r.realised)}`))))),
  ];
}

// ── detail screens ────────────────────────────────────────────────────────────

function timeline(events) {
  if (!events.length) return empty("No events recorded.");
  return h("ol", { class: "timeline" }, events.map((e) => h("li", { class: "t-item " + (e.severity === "critical" ? "critical" : e.severity === "warn" ? "warn" : e.entity_type === "trade" ? "key" : "") },
    h("span", { class: "t-title" }, (e.symbol && e.entity_type !== "trade" ? e.symbol + ": " : "") + e.title),
    h("span", { class: "t-meta num" }, h("span", {}, clock(e.ts)), h("span", {}, e.entity_type || e.kind),
      e.from_state && e.to_state && h("span", {}, `${e.from_state} → ${e.to_state}`)),
    e.detail && h("span", { class: "small" }, e.detail),
    h("details", {}, h("summary", {}, "Raw event"), h("pre", {}, JSON.stringify(e.payload, null, 2))))));
}

export async function tradeDetail(id) {
  const d = await get("/api/trade/" + encodeURIComponent(id));
  const t = d.trade;
  const [tn, label] = TRADE[t.state] || ["", t.state];
  return [
    h("a", { class: "back", href: "#trades" }, "‹ Trades"),
    h("section", { class: "card" },
      h("div", { class: "trade-head" }, h("span", { class: "sym" }, `${t.symbol}  ${t.filled_quantity || t.planned_quantity}`), pill(tn, label)),
      h("div", { class: "ladder-nums num" }, h("span", {}, `Entry ${price(t.entry_price)}`), h("span", {}, `Stop ${price(t.stop_price)}`), h("span", {}, `Target ${price(t.target_price)}`)),
      t.realised_pnl != null && h("p", { class: "num " + tone(t.realised_pnl) }, `${arrow(t.realised_pnl)} ${money(t.realised_pnl)}  ${(t.exit_reason || "").replaceAll("_", " ")}`)),
    d.orders.length > 0 && h("section", { class: "card" }, h("h2", {}, "Orders"), h("div", { class: "scroll-x" },
      h("table", { class: "data" }, h("thead", {}, h("tr", {}, ["Side", "Type", "Status", "Qty", "Fill"].map((c) => h("th", {}, c)))),
        h("tbody", {}, d.orders.map((o) => h("tr", {}, [o.side, o.order_type, o.status, `${o.filled_quantity}/${o.quantity}`, price(o.average_fill_price)].map((c) => h("td", { class: "num" }, c)))))))),
    h("section", { class: "card" }, h("h2", {}, `Timeline (${d.events.length})`), timeline(d.events)),
  ];
}

export async function dayDetail(day) {
  const d = await get("/api/session/" + encodeURIComponent(day));
  const s = d.session;
  return [
    h("a", { class: "back", href: "#days" }, "‹ Days"),
    h("section", { class: "card hero" }, h("div", { class: "hero-state" }, shortDate(day)),
      h("p", { class: "why" }, s ? `${(s.mode || "").toUpperCase()} session, ${s.state}${s.halt_reason ? ", halted: " + s.halt_reason : ""}` : "No session row")),
    d.trades.length > 0 && h("div", { class: "list-gap" }, d.trades.map(tradeCard)),
    h("section", { class: "card" }, h("h2", {}, `Timeline (${d.events.length})`), timeline(d.events)),
  ];
}
