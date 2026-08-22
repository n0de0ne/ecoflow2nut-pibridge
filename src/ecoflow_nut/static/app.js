/**
 * Entry point: the hash router, the poll scheduler and boot wiring.
 *
 * One scheduler drives every endpoint rather than four independent intervals,
 * and it reschedules itself after each round instead of using setInterval, so a
 * slow bridge cannot pile requests up behind each other.
 */

import {
  api, applyTheme, autoStore, BASE, el, els, fmtAgo, getRefresh, getTheme,
  healthStore, resolveRefreshMs, setToken, stateStore, toast,
} from "./core.js";
import {
  applyControlLock, control, dashboard, energy, history, settings,
  setAutoShutdown,
} from "./views.js";

const VIEWS = { dashboard, history, energy, settings };
const DEFAULT_VIEW = "dashboard";
const MAX_BACKOFF_MS = 30000;
// How often to poll for the things the push stream does not carry, once it is
// live. Auto-shutdown state changes on human timescales.
const IDLE_POLL_MS = 10000;

let current = null;
let currentName = null;
let suppressHashChange = false;

// ---------------------------------------------------------------------- //
// Router
// ---------------------------------------------------------------------- //

const routeName = () => (location.hash.replace(/^#\//, "").split("?")[0] || DEFAULT_VIEW);

function navigate(name) {
  if (!VIEWS[name]) name = DEFAULT_VIEW;
  if (name === currentName) return;

  // Give the outgoing view a chance to stop us (unsaved settings).
  if (current?.canLeave && !current.canLeave()) {
    suppressHashChange = true;
    location.hash = `#/${currentName}`;
    return;
  }

  current?.unmount?.();
  current = VIEWS[name];
  currentName = name;

  for (const section of els("[data-view]")) {
    section.hidden = section.dataset.view !== name;
  }
  for (const link of els("[data-nav]")) {
    const active = link.dataset.nav === name;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${name[0].toUpperCase()}${name.slice(1)} · EcoFlow Bridge`;

  const section = el(`[data-view="${name}"]`);
  section?.classList.remove("view-enter");
  void section?.offsetWidth;              // restart the transition
  section?.classList.add("view-enter");

  current.mount?.();
  schedule(0);
}

function onHashChange() {
  if (suppressHashChange) { suppressHashChange = false; return; }
  navigate(routeName());
}

// ---------------------------------------------------------------------- //
// Poll scheduler
// ---------------------------------------------------------------------- //

let timer = 0;
let backoff = 1;
let inFlight = false;

function baseInterval() {
  return resolveRefreshMs(getRefresh(), publishSeconds());
}

/** How often the bridge actually publishes a new reading.
 *
 * Falls back to poll_interval_seconds only for a server too old to send the
 * real cadence -- that field is the BLE link watchdog tick, and pacing the UI
 * off it is what made a live dashboard look stale.
 */
function publishSeconds() {
  const s = stateStore.get();
  return s?.publish_interval_seconds ?? s?.poll_interval_seconds;
}

async function tick() {
  if (inFlight || document.hidden) return;
  inFlight = true;
  const needs = new Set(current?.needs ?? ["state"]);
  try {
    if (needs.has("state")) stateStore.set(await api("api/state"));
    if (needs.has("auto")) autoStore.set(await api("api/autoshutdown"));
    healthStore.set({ ok: true, reason: "" });
    document.body.classList.remove("booting");
    backoff = 1;
  } catch (err) {
    // A failed first load should show the real state (dashes), not shimmer.
    document.body.classList.remove("booting");
    healthStore.set({ ok: false, reason: err.reason || err.message });
    backoff = Math.min(backoff * 1.6, MAX_BACKOFF_MS / baseInterval());
  } finally {
    inFlight = false;
    schedule();
  }
}

function schedule(delay) {
  clearTimeout(timer);
  // While the push stream is live, state arrives on its own. Keep a slow poll
  // only for what the stream does not carry (auto-shutdown), and let the timer
  // lapse entirely on views that need nothing else.
  if (events && events.readyState !== EventSource.CLOSED) {
    if (!current?.needs?.includes("auto")) return;
    timer = setTimeout(tick, delay ?? IDLE_POLL_MS);
    return;
  }
  const wait = delay ?? Math.min(baseInterval() * backoff, MAX_BACKOFF_MS);
  timer = setTimeout(tick, wait);
}

// ---------------------------------------------------------------------- //
// Push stream
// ---------------------------------------------------------------------- //

let events = null;

/** Subscribe to /api/events, so telemetry arrives when the device sends it.
 *
 * Polling stays as the fallback: EventSource cannot carry an auth header, so a
 * bridge with require_auth_for_read needs the token on the query string, and
 * any transport that will not open simply leaves the poll scheduler running.
 */
function openEventStream() {
  if (!window.EventSource || events) return;
  const token = getToken();
  const url = BASE + "api/events" + (token ? `?token=${encodeURIComponent(token)}` : "");
  try {
    events = new EventSource(url);
  } catch {
    return;                              // fall back to polling
  }
  events.onmessage = e => {
    try {
      stateStore.set(JSON.parse(e.data));
    } catch {
      return;                            // a malformed frame is not a failure
    }
    healthStore.set({ ok: true, reason: "" });
    document.body.classList.remove("booting");
    backoff = 1;
  };
  events.onopen = () => schedule();      // stand the fast poll down
  events.onerror = () => {
    // EventSource reconnects on its own; resume polling meanwhile so a stream
    // that never comes back does not leave the dashboard frozen.
    if (events.readyState === EventSource.CLOSED) { events = null; }
    schedule(0);
  };
}

function reopenEventStream() {
  events?.close();
  events = null;
  openEventStream();
  schedule(0);
}

// ---------------------------------------------------------------------- //
// Freshness pill
// ---------------------------------------------------------------------- //

function renderFreshness() {
  const chip = el("#freshness");
  const text = el("#freshnessText");
  if (!chip) return;
  const health = healthStore.get();
  const s = stateStore.get();
  chip.classList.remove("ok", "warn", "bad");

  if (!health.ok) {
    // The bridge itself is unreachable -- distinct from a stale device link.
    chip.classList.add("bad");
    text.textContent = "bridge unreachable";
    chip.title = health.reason;
    return;
  }
  const age = s?.updated_seconds_ago;
  const staleAfter = Math.max(15, (publishSeconds() ?? 5) * 3);
  if (age == null) {
    chip.classList.add("warn");
    text.textContent = "awaiting device";
    chip.title = "The bridge is up but has not decoded a frame yet.";
  } else if (age > staleAfter) {
    chip.classList.add("warn");
    text.textContent = `BLE stale · ${fmtAgo(age)}`;
    chip.title = "The bridge is up, but the device link has gone quiet.";
  } else {
    chip.classList.add("ok");
    text.textContent = `live · ${fmtAgo(age)}`;
    chip.title = "Telemetry is current.";
  }
}

function renderStatusPill(s) {
  const pill = el("#status");
  if (!pill) return;
  const status = s?.status;
  pill.textContent = status ?? "?";
  pill.className = "status-pill " + (
    status?.includes("LB") ? "status-LB"
      : status?.startsWith("OL") ? "status-OL"
        : status ? "status-OB" : "");
}

// ---------------------------------------------------------------------- //
// Token dialog
// ---------------------------------------------------------------------- //

function openTokenDialog() {
  const dialog = el("#tokenDialog");
  el("#tokenError").hidden = true;
  el("#tokenInput").value = "";
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute("open", "");        // Safari < 15.4
  el("#tokenInput").focus();
}

async function submitToken(event) {
  const dialog = el("#tokenDialog");
  if (event.submitter?.value !== "save") return;
  event.preventDefault();
  const value = el("#tokenInput").value.trim();
  const remember = el("#tokenRemember").checked;
  setToken(value, remember);
  try {
    // Validate before closing, so a typo says so instead of failing later.
    await api("api/auth/check");
    dialog.close();
    toast("Controls unlocked", "ok");
    applyControlLock();
    // The stream carries the token in its URL, so it has to be reopened with
    // the new one -- otherwise a bridge with require_auth_for_read keeps
    // refusing the old stream while every other request now succeeds.
    reopenEventStream();
    if (currentName === "settings") settings.load();
  } catch (err) {
    setToken("", false);
    const error = el("#tokenError");
    error.textContent = err.status === 401
      ? "That token was not accepted."
      : (err.reason || err.message);
    error.hidden = false;
  }
}

// ---------------------------------------------------------------------- //
// Boot
// ---------------------------------------------------------------------- //

function wireGlobals() {
  el("#freshness").addEventListener("click", () => schedule(0));

  el("#themeBtn").addEventListener("click", () => {
    const order = ["auto", "dark", "light"];
    const next = order[(order.indexOf(getTheme()) + 1) % order.length];
    applyTheme(next);
    const select = el("#prefTheme");
    if (select) select.value = next;
    toast(`Theme: ${next}`, "info");
  });

  el("#lockBtn").addEventListener("click", openTokenDialog);
  el("#tokenOpen").addEventListener("click", openTokenDialog);
  el("#tokenClear").addEventListener("click", () => {
    setToken("", false);
    applyControlLock();
    reopenEventStream();
    toast("Token cleared", "info");
  });
  el("#tokenDialog").querySelector("form").addEventListener("submit", submitToken);

  document.addEventListener("click", e => {
    const btn = e.target.closest("[data-out]");
    if (btn) control(btn.dataset.out, btn.dataset.on === "1");
  });
  el("#asOn").addEventListener("click", async () => {
    if (await setAutoShutdown(true)) schedule(0);
  });
  el("#asOff").addEventListener("click", async () => {
    if (await setAutoShutdown(false)) schedule(0);
  });
  el("#sbPress").addEventListener("click", async () => {
    try {
      const d = await api("api/switchbot", { method: "POST", body: { action: "press" } });
      toast(d.message || "pressed", "ok");
    } catch (err) { toast(err.reason || err.message, "error"); }
  });

  // Backgrounded tabs stop polling entirely and refresh the moment they return.
  // pageshow covers iOS restoring from the back-forward cache.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(timer);
      // Hold the socket open: a backgrounded tab that drops it pays a full
      // reconnect on return, and the server drops frames rather than queueing.
      return;
    }
    openEventStream();
    schedule(0);
  });
  window.addEventListener("pageshow", () => { openEventStream(); schedule(0); });

  window.addEventListener("beforeunload", e => {
    if (currentName === "settings" && settings.isDirty()) e.preventDefault();
  });

  document.addEventListener("keydown", e => {
    if (e.target.matches("input, select, textarea")) return;
    if (e.key === "r") { schedule(0); return; }
    // 1-5 pick a range preset while the History view is showing.
    if (currentName === "history" && /^[1-5]$/.test(e.key)) {
      els(".range button")[Number(e.key) - 1]?.click();
    }
  });
}

stateStore.subscribe(s => {
  renderStatusPill(s);
  renderFreshness();
  // Globally, not per-view: control_enabled only becomes known on the first
  // /api/state, and a view mounted before then (e.g. a deep link straight to
  // #/settings) would otherwise keep showing its initial locked state.
  applyControlLock();
});
healthStore.subscribe(renderFreshness);

applyTheme(getTheme());
wireGlobals();
window.addEventListener("hashchange", onHashChange);
openEventStream();
navigate(routeName());
