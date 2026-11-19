// Data access: a WebSocket that pushes the live projection, with polling as the fallback.

export async function get(path) {
  const r = await fetch(path, { cache: "no-store", credentials: "same-origin" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

export function connect({ onData, onStatus }) {
  let ws = null;
  let poll = null;
  let retry = 1000;
  let stopped = false;

  const startPolling = () => {
    if (poll) return;
    onStatus("poll");
    const tick = async () => {
      try { onData(await get("/api/live")); onStatus("poll"); } catch { onStatus("off"); }
    };
    tick();
    poll = setInterval(tick, 3000);
  };
  const stopPolling = () => { clearInterval(poll); poll = null; };

  const open = () => {
    if (stopped) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try { ws = new WebSocket(proto + "//" + location.host + "/ws/live"); } catch { startPolling(); return; }
    ws.onopen = () => { retry = 1000; stopPolling(); onStatus("live"); };
    ws.onmessage = (m) => { try { onData(JSON.parse(m.data)); onStatus("live"); } catch { /* ignore a bad frame */ } };
    ws.onclose = () => {
      if (stopped) return;
      startPolling();  // keep the screen alive while we try the socket again
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 30000);
    };
    ws.onerror = () => ws.close();
  };
  open();
  return () => { stopped = true; stopPolling(); if (ws) ws.close(); };
}
