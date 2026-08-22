/**
 * The four views. Each exports `needs` (which endpoints the scheduler should
 * poll while it is showing), plus mount/unmount so its timers and listeners die
 * on navigation.
 */

import {
  api, applyTheme, autoStore, el, els, escapeHtml, fmtMinutes, fmtMoney,
  fmtRuntime, getRefresh, getTheme, getToken, setRefresh, settingsStore,
  stateStore, toast,
} from "./core.js";
import { createChart, formatBucket } from "./chart.js";

const MINUTE = 60 * 1000;

/** History and Energy share one window, so switching tabs keeps your place. */
const sharedWindow = {
  since: Date.now() - 24 * 60 * MINUTE,
  until: Date.now(),
  live: true,
  presetMinutes: 1440,
};

// ---------------------------------------------------------------------- //
// Dashboard
// ---------------------------------------------------------------------- //

function setPort(id, kind, text, title) {
  const node = el("#" + id);
  if (!node) return;
  node.className = "pstat " + (kind === "on" ? "on" : kind === "off" ? "off" : "");
  node.innerHTML = `<span class="led ${kind}"></span>${escapeHtml(text)}`;
  node.title = title || "";
}

/** Each port reports a real flow flag; watts are shown alongside when drawing. */
function portState(on, watts, unknownNote) {
  const w = Math.round(watts ?? 0);
  // Not every model reports a switch state for every port -- the DELTA 2
  // generation sends no USB flag at all. Keep the LED honestly unknown rather
  // than inferring one, but still show a draw we do know about: "? · 12W" is
  // far more use than a bare "?" that reads like a broken tile.
  if (on == null) return ["unknown", w > 0 ? `? · ${w}W` : "?", unknownNote];
  if (!on) return ["off", "OFF", ""];
  return ["on", w > 0 ? `ON · ${w}W` : "ON · idle", ""];
}

// Battery geometry, mirroring the SVG in index.html.
const ORB = { top: 96, bottom: 204, left: 126, right: 234 };

/** Fill the battery to `pct`, with a gently curved surface. */
function orbSurface(pct) {
  const span = ORB.bottom - ORB.top;
  const level = ORB.bottom - (Math.max(0, Math.min(100, pct ?? 0)) / 100) * span;
  // Flatten the wave as the battery empties or fills, so the surface never
  // bulges outside the circle at the extremes.
  const wave = Math.min(5, (ORB.bottom - level) / 6, (level - ORB.top) / 6);
  const mid = (ORB.left + ORB.right) / 2;
  return `M ${ORB.left} ${level} Q ${(ORB.left + mid) / 2} ${level - wave} ${mid} ${level}` +
    ` T ${ORB.right} ${level} V ${ORB.bottom} H ${ORB.left} Z`;
}

function flowNode(circleId, valueId, wireId, watts, { absent = false } = {}) {
  const node = el(circleId), value = el(valueId), wire = el(wireId);
  if (!node) return;
  const w = watts == null ? null : Math.round(watts);
  // null means the model does not report this port at all, which is not the
  // same as it reporting zero.
  value.textContent = absent || w == null ? "–" : `${w}W`;
  const live = w != null && w > 0;
  node.classList.toggle("on", live);
  wire?.classList.toggle("active", live);
}

/** Power into (+) or out of (-) the battery, and where the figure came from.
 *
 * The BMS reports the pack's own volts and amps, and that product is the
 * answer whenever it is there. Inputs-minus-outputs is a poor substitute: on
 * an E2000 taking solar it claimed +112 W while the pack was actually drawing
 * about 46 W, the difference being conversion loss the ports cannot see.
 *
 * Zero from this source is a real zero -- an idle pack -- so it is trusted as
 * one. That is not true of every field the BMS sends: input_watts and
 * output_watts are dead on this firmware, which is why the driver reads volts
 * and amps instead and leaves battery_watts null when it cannot.
 */
function batteryFlow(s) {
  if (s.battery_watts != null) return { watts: s.battery_watts, measured: true };
  const inferred = netFlow(s);
  return inferred == null ? null : { watts: inferred, measured: false };
}

/** Inputs minus outputs, in watts. Positive means the battery is gaining. */
function netFlow(s) {
  const parts = [
    s.ac_input_watts, s.solar_input_watts,
    s.ac_output_watts, s.dc_output_watts, s.usb_output_watts, s.usbc_output_watts,
  ];
  if (parts.every(v => v == null)) return null;
  const supply = (s.ac_input_watts ?? 0) + (s.solar_input_watts ?? 0);
  const draw = (s.ac_output_watts ?? 0) + (s.dc_output_watts ?? 0)
    + (s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0);
  return supply - draw;
}

// Below this the battery is neither charging nor draining in any meaningful
// sense: conversion losses and rounding alone move the figure by a watt or two,
// and a meter that flips its label on that noise is worse than no meter.
const FLOW_DEADBAND_W = 8;

/** Plain words for what the battery is doing, and why.
 *
 * The question this answers is not "how many watts" -- the number is right
 * there -- but whether solar is *filling* the battery or merely cancelling the
 * load. Those look identical on a wattage readout and completely different in
 * what they mean for the evening.
 */
function batteryState(s, flow) {
  const solar = s.solar_input_watts ?? 0;
  const grid = s.ac_input_watts ?? 0;
  if (flow == null) return { key: "idle", text: "–" };
  if (flow > FLOW_DEADBAND_W) {
    if (solar > 0 && grid <= 0) return { key: "charging", text: "Solar charging" };
    if (solar > 0) return { key: "charging", text: "Charging · solar + grid" };
    return { key: "charging", text: "Charging from grid" };
  }
  if (flow < -FLOW_DEADBAND_W) {
    if (solar > 0) {
      // The interesting middle: PV is carrying part of the load, the pack the
      // rest. Not charging, but not unassisted discharge either.
      return { key: "draining", text: "Solar offsetting · battery covering the rest" };
    }
    if (grid > 0) return { key: "draining", text: "Draining · grid not keeping up" };
    return { key: "draining", text: "Running on battery" };
  }
  if (solar > 0) return { key: "level", text: "Solar covering the load" };
  return { key: "level", text: "Holding steady" };
}

/** Signed fraction of full scale, compressed so small flows stay visible.
 *
 * Linear against a 2400 W rating would render a 30 W trickle as one pixel and
 * a 200 W surplus as eight, which is the range that matters most here. A
 * square-root curve keeps the small end readable while a full-rate charge
 * still reaches the end; the exact number is printed alongside, so the bar
 * only has to carry direction and rough size.
 */
function meterFraction(flow, rated) {
  const scale = Math.max(100, rated || 1000);
  const unit = Math.min(1, Math.abs(flow) / scale);
  return Math.sign(flow) * Math.sqrt(unit);
}

/** Which way the battery is going, and how long it has -- with a source.
 *
 * Direction cannot come from ups.status: OL means the mains are present, not
 * that anything is charging. A station covering its load from solar reports OL
 * while the battery falls, and reading the charge estimate there gave a dash
 * next to a device that was plainly telling us six hours of discharge.
 *
 * The device's own estimate wins when it has one -- it knows its chemistry and
 * temperature -- and our SoC-and-load figure, the one NUT publishes, stands in
 * when it does not.
 */
function timeLeft(s) {
  const flow = batteryFlow(s)?.watts ?? 0;
  const charging = flow > 0;
  const device = charging ? s.remain_charge_minutes : s.remain_discharge_minutes;
  const other = charging ? s.remain_discharge_minutes : s.remain_charge_minutes;

  if (device != null) return { charging, text: fmtMinutes(device), source: "device" };
  // The device gave an estimate for the other direction only: honour that
  // rather than the flow, since a real number beats an inferred direction.
  if (other != null) {
    return { charging: !charging, text: fmtMinutes(other), source: "device" };
  }
  const ours = fmtRuntime(s.runtime_seconds);
  if (charging || ours === "idle") return { charging, text: "–", source: null };
  return { charging: false, text: ours, source: "estimate" };
}

/** The charge/discharge meter: direction, magnitude and what it means. */
function renderBatteryMeter(s) {
  const fill = el("#netFill");
  if (!fill) return;
  const reading = batteryFlow(s);
  const flow = reading?.watts ?? null;
  const state = batteryState(s, flow);

  el("#battState").textContent = state.text;
  const value = el("#netFlow");
  value.textContent = flow == null
    ? "–" : `${flow > 0 ? "+" : flow < 0 ? "−" : "±"}${Math.abs(Math.round(flow))} W`;
  value.title = reading?.measured
    ? "Measured at the battery by its own BMS. Positive means charging."
    : "Inferred from inputs minus outputs, so short by the conversion losses.";

  const frac = flow == null ? 0 : meterFraction(flow, s.rated_watts);
  // The bar grows from the centre, so a negative flow moves its left edge out
  // rather than its right.
  fill.style.left = `${frac >= 0 ? 50 : 50 + frac * 50}%`;
  fill.style.width = `${Math.abs(frac) * 50}%`;
  fill.classList.toggle("charging", state.key === "charging");
  fill.classList.toggle("draining", state.key === "draining");
}

function renderFlow(s) {
  if (!el("#flow")) return;
  flowNode("#nAcIn", "#flowAcIn", "#wAcIn", s.ac_input_watts);
  flowNode("#nSolar", "#flowSolar", "#wSolar", s.solar_input_watts,
    { absent: s.solar_input_watts == null });
  flowNode("#nAcOut", "#flowAcOut", "#wAcOut", s.ac_output_watts);
  flowNode("#nDc", "#flowDc", "#wDc", s.dc_output_watts);
  flowNode("#nUsb", "#flowUsb", "#wUsb",
    (s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0));

  const soc = s.soc_percent;
  el("#flowSoc").textContent = soc == null ? "–" : `${Math.round(soc)}%`;
  const fill = el("#flowFill");
  fill.setAttribute("d", orbSurface(soc));
  fill.classList.toggle("crit", soc != null && soc < 15);
  fill.classList.toggle("warn", soc != null && soc >= 15 && soc < 30);

  const left = timeLeft(s);
  el("#flowRemain").textContent =
    left.text === "–" ? "" : `${left.charging ? "full in" : "left"} ${left.text}`;
}

/** Mark the auto-shutdown trigger level, so headroom before a cut is visible. */
function renderReserve(a) {
  const line = el("#flowReserve");
  if (!line) return;
  const pct = a?.enabled ? a.trigger_soc_percent : null;
  if (pct == null) { line.setAttribute("hidden", ""); return; }
  line.removeAttribute("hidden");
  const y = ORB.bottom - (Math.max(0, Math.min(100, pct)) / 100) * (ORB.bottom - ORB.top);
  line.setAttribute("y1", y);
  line.setAttribute("y2", y);
}

function renderPorts(s) {
  // The AC and 12V states are the device's own switch flags. USB has no such
  // flag on the DELTA 2 generation, so the driver reads it off the draw --
  // power leaving the port proves the switch is closed. Only zero draw is
  // ambiguous, and only that shows as unknown.
  setPort("stAc", ...portState(s.ac_output_on, s.ac_output_watts,
    "The device has not reported an AC-output state."));

  const usbW = (s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0);
  setPort("stUsb", ...portState(s.usb_output_on, usbW,
    "This model reports no USB switch flag, and nothing is drawing, so the " +
    "bank could be off or on with nothing plugged in. Any draw above zero " +
    "proves it is on. The On/Off buttons work either way."));

  setPort("stDc", ...portState(s.dc_output_on, s.dc_output_watts,
    "The device has not reported a 12V DC state."));

  el("#eveCtl").hidden = s.eve_enabled !== true;
  if (s.eve_enabled === true) {
    // Read from the outlet, so its own button and its own app are reflected
    // here too -- not just what this bridge last commanded.
    const on = s.eve_on;
    const w = s.eve_watts;
    const draw = w == null ? "" : ` · ${Math.round(w)}W`;
    setPort("stEve", on === true ? "on" : on === false ? "off" : "unknown",
      on === true ? `ON${draw}` : on === false ? "OFF" : "?",
      on == null ? "Not read yet — the first poll is still to come, or the "
        + "outlet is unreachable." : "");
  }
  el("#sbCtl").hidden = s.switchbot_enabled !== true;
}

function renderState(s) {
  if (!s) return;
  if (s.device_model) {
    const heading = el("#deviceModel");
    if (heading && heading.textContent !== s.device_model) {
      heading.textContent = s.device_model;
      document.title = `${s.device_model} · Bridge`;
    }
  }
  // Only what the diagram cannot state itself: every port's watts are on it
  // already, so repeating them here was four tiles saying nothing new.
  const num = (id, value) => { el(id).textContent = value; };
  num("#soc", s.soc_percent ?? "–");
  num("#acInV", s.ac_input_voltage == null
    ? "–" : `${Math.round(s.ac_input_voltage)} V`);
  // Time-remaining lives on the battery itself; repeating it here would make
  // this strip a second copy of the diagram again. Net flow is the thing the
  // diagram implies but never states: which way the battery is going, and how
  // hard. The station makes up whatever the inputs do not cover.
  renderBatteryMeter(s);
  renderFlow(s);
  renderPorts(s);
  renderHealth(s);
  applyControlLock();
}

/**
 * What the pack says about itself, for the models whose BMS reports it.
 *
 * Hidden outright rather than shown as a row of dashes when nothing is
 * reported: a card of em-dashes reads as a broken card, and on a station whose
 * BMS stays quiet there is nothing to fix.
 */
function renderHealth(s) {
  const card = el("#healthCard");
  const known = [
    s.battery_temp_c, s.cell_mv_spread, s.battery_cycles, s.battery_full_mah,
  ].some(v => v != null);
  card.hidden = !known;
  if (!known) return;

  // The cell range alongside the pack figure: one hot cell in an otherwise
  // cool pack is the thing worth seeing, and the average hides it.
  const range = s.cell_temp_max_c != null && s.cell_temp_min_c != null
    && s.cell_temp_max_c !== s.cell_temp_min_c
    ? ` (cells ${Math.round(s.cell_temp_min_c)}–${Math.round(s.cell_temp_max_c)})`
    : "";
  el("#hTemp").textContent =
    s.battery_temp_c == null ? "–" : `${Math.round(s.battery_temp_c)}°C${range}`;

  el("#hSpread").textContent =
    s.cell_mv_spread == null ? "–" : `${s.cell_mv_spread} mV`;
  el("#hCycles").textContent = s.battery_cycles == null
    ? "–"
    : `${s.battery_cycles}${s.battery_soh_percent != null
      ? ` · ${Math.round(s.battery_soh_percent)}% SoH` : ""}`;

  // Measured fade, not the BMS's own SoH estimate -- they disagree, and this
  // one is arithmetic on two numbers the pack reports.
  el("#hCapacity").textContent =
    s.battery_full_mah == null || !s.battery_design_mah
      ? "–"
      : `${Math.round(s.battery_full_mah / s.battery_design_mah * 100)}% of new`;

  // The limits the station is enforcing on itself. Worth stating only when one
  // of them is actually in force -- "charge to 100%" is not news.
  const parts = [];
  if (s.charge_limit_percent != null && s.charge_limit_percent < 100) {
    parts.push(`charges to ${s.charge_limit_percent}%`);
  }
  if (s.discharge_limit_percent) {
    parts.push(`stops discharging at ${s.discharge_limit_percent}%`);
  }
  if (s.ac_charge_watts) parts.push(`AC charge capped at ${s.ac_charge_watts} W`);
  if (s.inverter_temp_c != null) {
    parts.push(`inverter ${Math.round(s.inverter_temp_c)}°C`
      + (s.fan_on === true ? ", fan on" : ""));
  }
  el("#hLimits").textContent = parts.length
    ? `Set on the unit: ${parts.join(" · ")}.` : "";
}

function renderAuto(a) {
  if (!a) return;
  renderReserve(a);
  let kind, text;
  if (!a.enabled) { kind = "off"; text = "Disabled"; }
  // Checked before `triggered`: the state machine latches that before any I/O,
  // so while we hold for the outlet to go idle nothing has actually been cut.
  else if (a.awaiting_eve_idle) {
    kind = "warn";
    text = a.eve_watts == null
      ? "Waiting · outlet unreadable, holding cut"
      : `Waiting for load to drop · ${Math.round(a.eve_watts)} W`;
  }
  else if (a.triggered) { kind = "crit"; text = "CUT sent"; }
  else if (a.armed) {
    kind = "warn";
    text = "ARMED" + (a.seconds_until_cut != null
      ? ` · cutting in ${Math.round(a.seconds_until_cut)}s` : "");
  } else { kind = "on"; text = "Monitoring"; }
  el("#asLed").className = "led " + kind;
  el("#asState").textContent = text;
  el("#asDetail").textContent =
    `trigger ≤ ${a.trigger_soc_percent}%, recover ${a.recover_soc_percent}%, ` +
    `grace ${a.grace_period_seconds}s, cuts: ` +
    `${(a.cut_outputs || []).join(", ") || "none"}`;
}

/** Controls need both a configured token on the bridge and one in this browser. */
export function applyControlLock() {
  const s = stateStore.get() || {};
  const controlEnabled = s.control_enabled === true;
  // getToken(), not localStorage: a token entered without "remember" lives only
  // in memory, and reading storage directly would leave the UI locked.
  const hasToken = !!getToken();
  const locked = !controlEnabled || !hasToken;
  for (const b of els("[data-out], #asOn, #asOff, #sbPress")) b.disabled = locked;
  const saveBtn = el("#saveSettings");
  if (saveBtn) saveBtn.disabled = locked;
  const note = el("#controlNote");
  if (note) {
    note.textContent = !controlEnabled
      ? "Controls disabled (no auth_token configured on the bridge)."
      : !hasToken ? "Enter the control token to enable actions." : "";
  }
  const locked_note = el("#settingsLocked");
  if (locked_note) {
    locked_note.textContent = locked
      ? "Settings are read-only until you enter the control token."
      : "";
  }
}

async function control(output, enabled) {
  // Guard: turning USB off can kill a Pi powered from the station's USB port.
  if (output === "usb" && !enabled) {
    const ok = confirm(
      "Turn the USB output OFF?\n\n" +
      "If this bridge (e.g. a Raspberry Pi) is powered from the station's USB " +
      "port, this cuts its OWN power — the dashboard and the bridge will go " +
      "down.\n\nContinue only if you are sure nothing critical runs off USB.");
    if (!ok) return;
  }
  try {
    const d = await api("api/control", { method: "POST", body: { output, enabled } });
    toast(d.message || "ok", "ok");
  } catch (err) {
    toast(err.reason || err.message, "error");
  }
}

export const dashboard = {
  needs: ["state", "auto"],
  mount() {
    this._offState = stateStore.subscribe(renderState);
    this._offAuto = autoStore.subscribe(renderAuto);
  },
  unmount() { this._offState?.(); this._offAuto?.(); },
};

// ---------------------------------------------------------------------- //
// History
// ---------------------------------------------------------------------- //

const SERIES = [
  { key: "soc_percent", label: "SoC", unit: "%", color: "--c-soc", axis: "pct" },
  { key: "ac_output_watts", label: "AC out", unit: "W", color: "--c-acout", axis: "w" },
  { key: "ac_input_watts", label: "AC in", unit: "W", color: "--c-acin", axis: "w" },
  { key: "solar_input_watts", label: "Solar", unit: "W", color: "--c-solar", axis: "w" },
  { key: "usb_output_watts", label: "USB", unit: "W", color: "--c-usb", axis: "w" },
  { key: "input_watts", label: "Input", unit: "W", color: "--c-in", axis: "w" },
  { key: "output_watts", label: "Output", unit: "W", color: "--c-out", axis: "w" },
];
const HIDDEN_KEY = "ecoflow_hidden_series";
const DEFAULT_HIDDEN = ["usb_output_watts", "input_watts", "output_watts"];

function loadHidden() {
  try {
    return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY)) ?? DEFAULT_HIDDEN);
  } catch { return new Set(DEFAULT_HIDDEN); }
}
const saveHidden = set => localStorage.setItem(HIDDEN_KEY, JSON.stringify([...set]));

export const history = {
  needs: ["state"],
  chart: null,
  generation: 0,

  mount() {
    const canvas = el("#chart");
    this.chart = createChart(canvas, { series: SERIES });
    const hidden = loadHidden();
    for (const key of hidden) this.chart.setVisible(key, false);
    this.renderLegend(hidden);

    this.chart.setWindow(sharedWindow.since, sharedWindow.until,
      { live: sharedWindow.live, silent: true });
    this.chart.on("windowchange", w => {
      sharedWindow.since = w.since;
      sharedWindow.until = w.until;
      sharedWindow.live = w.live;
      this.syncLiveButton();
      this.fetch();
    });

    this._onRange = e => {
      const btn = e.target.closest("[data-min]");
      if (!btn) return;
      for (const b of els(".range button")) b.classList.toggle("active", b === btn);
      sharedWindow.presetMinutes = Number(btn.dataset.min);
      const now = Date.now();
      this.chart.setWindow(now - sharedWindow.presetMinutes * MINUTE, now,
        { live: true, immediate: true });
    };
    el(".range").addEventListener("click", this._onRange);

    this._onLive = () => {
      const now = Date.now();
      const span = sharedWindow.until - sharedWindow.since;
      this.chart.setWindow(now - span, now, { live: true, immediate: true });
    };
    el("#chartLive").addEventListener("click", this._onLive);

    this._onReset = () => {
      const now = Date.now();
      this.chart.setWindow(now - sharedWindow.presetMinutes * MINUTE, now,
        { live: true, immediate: true });
    };
    el("#chartReset").addEventListener("click", this._onReset);

    this._offState = stateStore.subscribe(s => this.onTick(s), { immediate: false });
    this.syncLiveButton();
    this.fetch();
  },

  renderLegend(hidden) {
    const host = el("#chartLegend");
    host.innerHTML = SERIES.map(s => `
      <button type="button" class="legend-chip${hidden.has(s.key) ? " off" : ""}"
              data-series="${s.key}" aria-pressed="${!hidden.has(s.key)}">
        <i style="background:var(${s.color})"></i>${s.label}
      </button>`).join("");
    this._onLegend = e => {
      const btn = e.target.closest("[data-series]");
      if (!btn) return;
      const key = btn.dataset.series;
      const next = !this.chart.isVisible(key);
      this.chart.setVisible(key, next);
      btn.classList.toggle("off", !next);
      btn.setAttribute("aria-pressed", String(next));
      const set = loadHidden();
      if (next) set.delete(key); else set.add(key);
      saveHidden(set);
    };
    host.addEventListener("click", this._onLegend);
  },

  syncLiveButton() {
    el("#chartLive")?.classList.toggle("active", sharedWindow.live);
  },

  /** In live mode each state tick slides the window forward to "now". */
  onTick() {
    if (!sharedWindow.live || !this.chart) return;
    const span = sharedWindow.until - sharedWindow.since;
    const now = Date.now();
    sharedWindow.since = now - span;
    sharedWindow.until = now;
    this.chart.setWindow(now - span, now, { live: true, silent: true });
    this.fetch();
  },

  async fetch() {
    const s = stateStore.get();
    if (s && s.history_enabled === false) {
      el("#historyNote").textContent =
        "History logging is off. Enable sqlite: or postgres: in the bridge's " +
        "config to record and chart telemetry.";
      return;
    }
    // Ask for roughly one bucket per 3 px, so zooming in genuinely raises the
    // resolution instead of stretching the same buckets.
    const width = el("#chart")?.clientWidth || 600;
    const maxPoints = Math.min(1000, Math.max(60, Math.round(width / 3)));
    const gen = ++this.generation;
    try {
      const d = await api("api/history?" + new URLSearchParams({
        since: Math.floor(sharedWindow.since / 1000),
        until: Math.ceil(sharedWindow.until / 1000),
        max_points: maxPoints,
      }));
      if (gen !== this.generation || !this.chart) return;   // a newer request won
      this.chart.setData(d.points || [], { bucketSeconds: d.bucket_seconds });
      const bucket = formatBucket(d.bucket_seconds);
      el("#historyNote").textContent = d.points?.length
        ? `${d.points.length} points · ${bucket} average`
        : "No samples in this window.";
    } catch (err) {
      if (gen === this.generation) {
        el("#historyNote").textContent = `History unavailable: ${err.reason || err.message}`;
      }
    }
  },

  unmount() {
    el(".range")?.removeEventListener("click", this._onRange);
    el("#chartLive")?.removeEventListener("click", this._onLive);
    el("#chartReset")?.removeEventListener("click", this._onReset);
    el("#chartLegend")?.removeEventListener("click", this._onLegend);
    this._offState?.();
    this.chart?.destroy();
    this.chart = null;
  },
};

// ---------------------------------------------------------------------- //
// Energy
// ---------------------------------------------------------------------- //

/**
 * The two labels for one split, so they always describe the same thing.
 *
 * Rounding each share on its own prints "51%" beside "50%" often enough to
 * notice, so one is rounded and the other is its complement. A contribution
 * too small to round to a whole percent is still a contribution: it reads
 * "<1%", never a flat "0%" that says it never happened.
 */
function shareLabels(solar) {
  const pct = Math.round(solar * 100);
  if (solar > 0 && pct === 0) return ["<1%", ">99%"];
  if (solar < 1 && pct === 100) return [">99%", "<1%"];
  return [`${pct}%`, `${100 - pct}%`];
}

/**
 * The solar/grid split of everything that came in, as one stacked bar.
 *
 * Segment widths carry the ratio; the figures beside them carry it again in
 * words, so nothing here depends on telling two colours apart -- and the
 * numbers are all present as text for a screen reader without a table.
 */
function renderMix(d) {
  const solar = d.solar_share;
  const grid = d.grid_share;
  const known = solar != null && grid != null;
  const [solarLabel, gridLabel] = known ? shareLabels(solar) : ["–", "–"];

  const bar = el("#mixBar");
  const solarSeg = el("#mixSolar");
  const gridSeg = el("#mixGrid");

  // Widths come from the raw shares, not the rounded label: an hour of weak
  // winter sun rounds to 0% but is not nothing, and a bar that drops it while
  // the label beside it says "<1%" contradicts itself. min-width in the CSS
  // keeps that sliver visible. Only a true zero is hidden -- a zero-width flex
  // child still shows the 2px gap, which reads as a colour that contributed.
  solarSeg.hidden = !known || solar <= 0;
  gridSeg.hidden = !known || grid <= 0;
  solarSeg.style.flexGrow = known ? solar * 100 : 0;
  gridSeg.style.flexGrow = known ? grid * 100 : 0;
  bar.classList.toggle("unknown", !known);

  el("#mixSolarPct").textContent = solarLabel;
  el("#mixGridPct").textContent = gridLabel;
  el("#mixSolarKwh").textContent = `${(d.solar_kwh ?? 0).toFixed(2)} kWh`;
  el("#mixGridKwh").textContent = `${(d.grid_kwh ?? 0).toFixed(2)} kWh`;

  bar.setAttribute("aria-label", known
    ? `Energy in: ${solarLabel} solar, ${gridLabel} grid, ` +
      `${(d.input_kwh ?? 0).toFixed(2)} kWh total.`
    : "Energy mix unavailable.");

  el("#mixNote").textContent = !known && d.solar_reported === false
    ? "This model does not report solar input, so the split is unknown — a " +
      "station with no PV sensor is not the same as one harvesting nothing."
    : !known
      ? "Nothing came in over this window."
      : `${(d.input_kwh ?? 0).toFixed(2)} kWh in. What came in, not what ` +
        `powered the load — the battery sits in between.`;
}

export const energy = {
  needs: ["state"],

  mount() { this.refresh(); this._timer = setInterval(() => this.refresh(), 60000); },
  unmount() { clearInterval(this._timer); },

  async refresh() {
    const s = stateStore.get();
    if (s && s.history_enabled === false) {
      el("#energyNote").textContent =
        "History logging is off, so there is nothing to cost. Enable sqlite: or " +
        "postgres: in the bridge's config.";
      return;
    }
    try {
      const d = await api("api/energy?" + new URLSearchParams({
        since: Math.floor(sharedWindow.since / 1000),
        until: Math.ceil(sharedWindow.until / 1000),
      }));
      const cur = d.currency || "€";
      const span = Math.round((sharedWindow.until - sharedWindow.since) / MINUTE);
      el("#energyRange").textContent = `· ${fmtMinutes(span)} window`;
      el("#eKwh").textContent = (d.grid_kwh ?? 0).toFixed(2);
      el("#eCost").textContent = d.pricing_enabled ? fmtMoney(d.total_cost, cur) : "—";
      el("#eHc").innerHTML = `${(d.hc_kwh ?? 0).toFixed(2)} kWh` +
        (d.pricing_enabled ? ` · <b>${fmtMoney(d.hc_cost, cur)}</b>` : "");
      el("#eHp").innerHTML = `${(d.hp_kwh ?? 0).toFixed(2)} kWh` +
        (d.pricing_enabled ? ` · <b>${fmtMoney(d.hp_cost, cur)}</b>` : "");
      el("#eAvg").textContent =
        `${Math.round(d.avg_grid_watts ?? 0)} / ${Math.round(d.peak_grid_watts ?? 0)} W`;
      el("#eProj").innerHTML = d.pricing_enabled
        ? `${fmtMoney(d.cost_per_day, cur)}/day · <b>${fmtMoney(d.cost_per_month, cur)}/mo</b>`
        : "—";
      el("#eSolarKwh").textContent = (d.solar_kwh ?? 0).toFixed(2);
      renderMix(d);
      el("#eLoadCost").textContent =
        d.pricing_enabled ? fmtMoney(d.load_cost, cur) : "—";
      el("#eSolarSaving").textContent =
        d.pricing_enabled ? fmtMoney(d.solar_savings, cur) : "—";
      // A negative net saving is real and worth showing rather than clamping:
      // it means the window bought more than it delivered, which is what
      // charging the battery from the grid looks like.
      const net = d.net_saving ?? 0;
      el("#eNetSaving").textContent = d.pricing_enabled
        ? (net < 0 ? `−${fmtMoney(-net, cur)}` : fmtMoney(net, cur)) : "—";

      el("#energyNote").textContent = d.pricing_enabled
        ? `Grid draw priced by HC window ${d.hc_window}. ` +
          `Load delivered: ${(d.load_kwh ?? 0).toFixed(2)} kWh. ` +
          `Over a window shorter than a full charge cycle these figures are ` +
          `skewed by the battery's own level moving; they settle over days.`
        : `Enable pricing in Settings to see cost. ` +
          `Load delivered: ${(d.load_kwh ?? 0).toFixed(2)} kWh.`;
    } catch (err) {
      el("#energyNote").textContent = `Energy unavailable: ${err.reason || err.message}`;
    }
  },
};

// ---------------------------------------------------------------------- //
// Settings
// ---------------------------------------------------------------------- //

const TIME_RE = /^([01]?\d|2[0-3]):[0-5]\d$/;
// Field types where an empty box means "off" rather than an invalid number.
const NULLABLE = new Set(["float_or_null", "int_or_null"]);
const fieldId = key => "set_" + key.replace(/[^a-z0-9]/gi, "_");
const isPercent = f => f.type === "int" && f.min === 0 && f.max === 100;

/**
 * Mirrors settings_store._coerce so the user gets an answer as they type. The
 * server stays authoritative; this only removes the round-trip.
 */
function validate(field, value) {
  if (field.type === "bool") return null;
  if (field.type === "str") return null;
  if (field.type === "time") {
    return TIME_RE.test(value ?? "") ? null : "expected HH:MM (00:00-23:59)";
  }
  if (NULLABLE.has(field.type) && (value === null || value === "")) return null;
  if (value === "" || value === null || Number.isNaN(Number(value))) {
    return "expected a number";
  }
  const num = Number(value);
  if (field.min != null && num < field.min) return `must be >= ${field.min}`;
  if (field.max != null && num > field.max) return `must be <= ${field.max}`;
  return null;
}

export const settings = {
  needs: ["state"],
  saved: {},
  draft: {},
  fields: [],

  async mount() {
    this._onSearch = () => this.applyFilter();
    el("#settingsSearch").addEventListener("input", this._onSearch);
    this._onSave = () => this.save();
    el("#saveSettings").addEventListener("click", this._onSave);
    this._onRevert = () => this.revert();
    el("#revertSettings").addEventListener("click", this._onRevert);

    el("#prefTheme").value = getTheme();
    this._onTheme = e => applyTheme(e.target.value);
    el("#prefTheme").addEventListener("change", this._onTheme);

    el("#prefRefresh").value = getRefresh();
    this._onRefresh = e => {
      setRefresh(e.target.value);
      toast("Refresh rate updated", "ok");
    };
    el("#prefRefresh").addEventListener("change", this._onRefresh);

    await this.load();
    applyControlLock();
  },

  unmount() {
    el("#settingsSearch")?.removeEventListener("input", this._onSearch);
    el("#saveSettings")?.removeEventListener("click", this._onSave);
    el("#revertSettings")?.removeEventListener("click", this._onRevert);
    el("#prefTheme")?.removeEventListener("change", this._onTheme);
    el("#prefRefresh")?.removeEventListener("change", this._onRefresh);
    el("#saveBar").hidden = true;
  },

  isDirty() {
    return Object.keys(this.draft).some(k => !Object.is(this.draft[k], this.saved[k]));
  },

  /** The router calls this before leaving, so edits are never silently lost. */
  canLeave() {
    if (!this.isDirty()) return true;
    return confirm("You have unsaved settings. Leave and discard them?");
  },

  async load() {
    try {
      const d = await api("api/settings");
      this.fields = d.fields || [];
      this.saved = { ...d.values };
      this.draft = { ...d.values };
      settingsStore.set(d);
      this.render();
    } catch (err) {
      el("#settingsGroups").innerHTML =
        `<div class="card muted">Settings unavailable: ${escapeHtml(err.reason || err.message)}</div>`;
    }
  },

  render() {
    const groups = new Map();
    for (const f of this.fields) {
      if (!groups.has(f.group)) groups.set(f.group, []);
      groups.get(f.group).push(f);
    }
    el("#settingsGroups").innerHTML = [...groups].map(([group, fields]) => `
      <details class="card group" data-group="${escapeHtml(group)}" open>
        <summary><span>${escapeHtml(group)}</span><span class="badge" hidden></span></summary>
        ${fields.map(f => this.fieldRow(f)).join("")}
      </details>`).join("");

    this._onInput = e => this.onInput(e);
    el("#settingsGroups").addEventListener("input", this._onInput);
    el("#settingsGroups").addEventListener("change", this._onInput);
    this.updateDirty();
  },

  fieldRow(f) {
    const id = fieldId(f.key);
    const value = this.draft[f.key];
    const help = f.help ? `<span class="help">${escapeHtml(f.help)}</span>` : "";
    let input;
    if (f.type === "bool") {
      input = `<label class="switch"><input type="checkbox" id="${id}" data-key="${f.key}"
        ${value ? "checked" : ""}><span></span></label>`;
    } else if (f.type === "time") {
      input = `<input type="time" id="${id}" data-key="${f.key}" value="${escapeHtml(value ?? "")}">`;
    } else if (f.type === "str") {
      input = `<input type="text" id="${id}" data-key="${f.key}" value="${escapeHtml(value ?? "")}">`;
    } else if (isPercent(f)) {
      input = `<span class="slider-combo">
        <input type="range" min="0" max="100" step="1" value="${value ?? 0}"
               data-slider-for="${f.key}" aria-label="${escapeHtml(f.label)} slider">
        <input type="number" id="${id}" data-key="${f.key}" data-type="${f.type}"
               min="0" max="100" step="1" value="${value ?? ""}"></span>`;
    } else {
      const attrs = [
        f.step != null ? `step="${f.step}"` : "",
        f.min != null ? `min="${f.min}"` : "",
        f.max != null ? `max="${f.max}"` : "",
      ].join(" ");
      const placeholder = NULLABLE.has(f.type) ? ' placeholder="off"' : "";
      input = `<input type="number" id="${id}" data-key="${f.key}" data-type="${f.type}"
        ${attrs}${placeholder} value="${value ?? ""}">`;
    }
    return `<div class="frow" data-row="${f.key}"
      data-search="${escapeHtml((f.label + " " + f.key + " " + (f.help || "")).toLowerCase())}">
      <label for="${id}">${escapeHtml(f.label)}${help}</label>
      ${input}
      <span class="field-error" hidden></span>
    </div>`;
  },

  onInput(e) {
    const slider = e.target.dataset.sliderFor;
    if (slider) {
      const number = el(`[data-key="${slider}"]`);
      number.value = e.target.value;
      this.setValue(slider, Number(e.target.value));
      return;
    }
    const key = e.target.dataset.key;
    if (!key) return;
    const field = this.fields.find(f => f.key === key);
    let value;
    if (e.target.type === "checkbox") value = e.target.checked;
    else if (e.target.type === "number") {
      value = e.target.value === ""
        ? (NULLABLE.has(field.type) ? null : "")
        : Number(e.target.value);
    } else value = e.target.value;
    if (isPercent(field)) {
      const range = el(`[data-slider-for="${key}"]`);
      if (range) range.value = e.target.value;
    }
    this.setValue(key, value);
  },

  setValue(key, value) {
    this.draft[key] = value;
    const field = this.fields.find(f => f.key === key);
    const row = el(`[data-row="${key}"]`);
    const error = row?.querySelector(".field-error");
    let message = validate(field, value);
    // Cross-field rule from settings_store.apply_updates, surfaced where it bites.
    if (!message && key === "auto_shutdown.recover_soc_percent") {
      const trigger = this.draft["auto_shutdown.trigger_soc_percent"];
      if (trigger != null && Number(value) < Number(trigger)) {
        message = `must be >= trigger SoC (${trigger}%)`;
      }
    }
    if (error) {
      error.textContent = message || "";
      error.hidden = !message;
    }
    row?.classList.toggle("invalid", !!message);
    row?.querySelector("input")?.setAttribute("aria-invalid", String(!!message));
    this.updateDirty();
  },

  hasErrors() { return els("#settingsGroups .frow.invalid").length > 0; },

  updateDirty() {
    const changed = Object.keys(this.draft)
      .filter(k => !Object.is(this.draft[k], this.saved[k]));
    for (const row of els("#settingsGroups .frow")) {
      row.classList.toggle("dirty", changed.includes(row.dataset.row));
    }
    for (const group of els("#settingsGroups .group")) {
      const n = [...group.querySelectorAll(".frow")]
        .filter(r => changed.includes(r.dataset.row)).length;
      const badge = group.querySelector(".badge");
      badge.hidden = n === 0;
      badge.textContent = n;
    }
    const bar = el("#saveBar");
    bar.hidden = changed.length === 0;
    el("#saveBarText").textContent =
      `${changed.length} unsaved change${changed.length === 1 ? "" : "s"}`;
    el("#saveSettings").disabled = this.hasErrors() || !getToken();
  },

  revert() {
    this.draft = { ...this.saved };
    this.render();
  },

  async save() {
    const updates = {};
    for (const key of Object.keys(this.draft)) {
      if (!Object.is(this.draft[key], this.saved[key])) updates[key] = this.draft[key];
    }
    if (!Object.keys(updates).length) return;
    try {
      const d = await api("api/settings", { method: "POST", body: { updates } });
      // Adopt the server's coerced values rather than our own drafts, so e.g. an
      // int field that was typed as 10.6 shows the 10 that was actually stored.
      this.saved = { ...this.saved, ...(d.values || updates) };
      this.draft = { ...this.saved };
      this.render();
      toast(`Saved ${(d.changed || []).length} change(s)`, "ok");
    } catch (err) {
      // Server errors read "<key>: <message>"; pin them to the field they name.
      const reason = err.reason || err.message;
      const [maybeKey, ...rest] = reason.split(": ");
      const row = el(`[data-row="${maybeKey}"]`);
      if (row && rest.length) {
        const error = row.querySelector(".field-error");
        error.textContent = rest.join(": ");
        error.hidden = false;
        row.classList.add("invalid");
      }
      toast(reason, "error");
    }
  },

  applyFilter() {
    const query = el("#settingsSearch").value.trim().toLowerCase();
    let matches = 0;
    for (const group of els("#settingsGroups .group")) {
      let visible = 0;
      for (const row of group.querySelectorAll(".frow")) {
        const hit = !query || row.dataset.search.includes(query);
        row.hidden = !hit;
        if (hit) visible++;
      }
      group.hidden = visible === 0;
      if (query && visible) group.open = true;
      matches += visible;
    }
    el("#settingsCount").textContent = query
      ? `${matches} setting${matches === 1 ? "" : "s"} match`
      : "";
  },
};

/** Auto-shutdown's dashboard toggle writes the same key the settings page does. */
export async function setAutoShutdown(enabled) {
  try {
    await api("api/settings", {
      method: "POST",
      body: { updates: { "auto_shutdown.enabled": enabled } },
    });
    toast(`Auto-shutdown ${enabled ? "enabled" : "disabled"}`, "ok");
    return true;
  } catch (err) {
    toast(err.reason || err.message, "error");
    return false;
  }
}

export { control, sharedWindow };
