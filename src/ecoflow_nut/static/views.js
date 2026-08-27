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

/**
 * State of charge, at the resolution the device reports it.
 *
 * One formatter for both places it appears. The orb rounded to a whole number
 * sat 40 px above a tile reading 73.5, which is the same quantity contradicting
 * itself on one screen -- and reads as one of the two being broken rather than
 * as a rounding choice.
 */
const socText = v => (v == null ? "\u2013" : v.toFixed(1));

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
  el("#flowSoc").textContent = soc == null ? "–" : `${socText(soc)}%`;
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
  num("#soc", socText(s.soc_percent));
  num("#acInV", s.ac_input_voltage == null
    ? "–" : `${Math.round(s.ac_input_voltage)} V`);
  // Time-remaining lives on the battery itself; repeating it here would make
  // this strip a second copy of the diagram again. Net flow is the thing the
  // diagram implies but never states: which way the battery is going, and how
  // hard. The station makes up whatever the inputs do not cover.
  renderBatteryMeter(s);
  renderFlow(s);
  renderPorts(s);
  renderBalance(s);
  renderHealth(s);
  applyControlLock();
}

const W = v => `${Math.round(v)} W`;

/**
 * Where every watt is going, including the ones nothing meters.
 *
 * The station meters ports. It does not meter the inverter, the charger, the
 * DC-DC regulators or its own electronics -- but they all sit on the same DC
 * bus, so whatever is unaccounted for is what they are burning. That residual
 * is the answer to the question the flow diagram raises and cannot settle:
 * why the battery is gaining less than the solar coming in.
 */
function renderBalance(s) {
  const card = el("#balanceCard");
  const loss = s.conversion_watts;
  const known = loss != null && s.supply_watts != null && s.draw_watts != null;
  card.hidden = !known;
  if (!known) return;

  const parts = (pairs) => pairs
    .filter(([, w]) => w != null && Math.round(w) > 0)
    .map(([name, w]) => `${name} ${Math.round(w)}`)
    .join(" · ");

  el("#balIn").textContent = W(s.supply_watts);
  el("#balInParts").textContent = parts([
    ["grid", s.ac_input_watts], ["solar", s.solar_input_watts],
  ]);
  el("#balOut").textContent = W(s.draw_watts);
  el("#balOutParts").textContent = parts([
    ["AC", s.ac_output_watts], ["12V", s.dc_output_watts],
    ["USB", s.usb_output_watts], ["USB-C", s.usbc_output_watts],
  ]);

  const batt = s.battery_watts ?? 0;
  el("#balBattK").textContent = batt >= 0 ? "Into the battery" : "From the battery";
  el("#balBatt").textContent = `${batt >= 0 ? "+" : "−"}${W(Math.abs(batt))}`;
  el("#balBattD").textContent = "";

  // A negative residual is two sensors disagreeing, not a station making
  // power. Say so rather than printing it as if it meant something.
  const bogus = loss < 0;
  el("#balLoss").textContent = bogus ? "–" : W(loss);
  el("#balLossD").textContent = bogus
    ? "sensors disagree"
    : s.supply_watts > 0 ? `${(loss / s.supply_watts * 100).toFixed(1)}% of throughput` : "";

  el("#balNote").textContent = bogus
    ? "The metered ports currently add up to slightly more than the input, " +
      "which is measurement noise rather than a reading."
    : balanceNote(s, loss);
}

/** One sentence naming what the overhead means right now. */
function balanceNote(s, loss) {
  const draw = s.draw_watts ?? 0;
  const solar = s.solar_input_watts ?? 0;
  const batt = s.battery_watts ?? 0;
  // The runtime consequence is the part that matters, and it grows as the load
  // shrinks: 14 W beside a 190 W load is nothing, beside a 30 W load it is a
  // third of the runtime.
  const cost = draw > 0
    ? ` At this draw it costs about ${((draw + loss) / draw - 1) * 100 < 1
        ? "1" : Math.round(((draw + loss) / draw - 1) * 100)}% of your runtime on battery.`
    : "";
  if (batt > 0 && solar > 0) {
    return `Solar is bringing in ${W(solar)} and the pack is gaining ` +
      `${W(batt)}; the ${W(loss)} between them is the inverter, the charger ` +
      `and the unit's own electronics.${cost}`;
  }
  if (batt < 0) {
    return `The pack is supplying ${W(-batt)} to cover a ${W(draw)} draw — ` +
      `the extra ${W(loss)} is what the box costs to run.${cost}`;
  }
  return `Everything metered, minus everything metered out, leaves ${W(loss)} ` +
    `for the inverter, the charger and the unit's own electronics.${cost}`;
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
  // The reserve is the usual reason a pack sits below full on mains, and it is
  // a different setting from the charge limit -- reporting only the limit says
  // "no limit set" beside a station that is plainly holding at 90%.
  if (s.backup_reserve_percent) {
    parts.push(`holds ${s.backup_reserve_percent}% in reserve`);
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
  // The only signed series: positive is the pack charging, negative is the pack
  // carrying the load. Dashed because its colour sits close to the others under
  // red-green colour blindness, and because the shape marks it as the odd one
  // out -- it is the one line that means something different below the axis.
  {
    key: "battery_watts", label: "Battery", unit: "W", color: "--c-batt",
    axis: "w", dash: [5, 4], signed: true,
  },
  { key: "usb_output_watts", label: "USB", unit: "W", color: "--c-usb", axis: "w" },
  { key: "input_watts", label: "Input", unit: "W", color: "--c-in", axis: "w" },
  { key: "output_watts", label: "Output", unit: "W", color: "--c-out", axis: "w" },
];
/**
 * The legend swatch, carrying the same dash the line does.
 *
 * A solid block beside a dashed line makes the legend the one place the second
 * channel is missing -- exactly where someone goes to resolve two colours they
 * cannot tell apart.
 */
function swatch(s) {
  if (!s.dash) return `style="background:var(${s.color})"`;
  const [on, off] = s.dash;
  return `class="dashed" style="background:repeating-linear-gradient(90deg,` +
    `var(${s.color}) 0 ${on}px,transparent ${on}px ${on + off}px)"`;
}

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
      for (const b of els("#chartRange button")) b.classList.toggle("active", b === btn);
      sharedWindow.presetMinutes = Number(btn.dataset.min);
      const now = Date.now();
      this.chart.setWindow(now - sharedWindow.presetMinutes * MINUTE, now,
        { live: true, immediate: true });
    };
    el("#chartRange").addEventListener("click", this._onRange);

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
        <i ${swatch(s)}></i>${s.label}
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
    el("#chartRange")?.removeEventListener("click", this._onRange);
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

  const solarKwh = `${(d.solar_kwh ?? 0).toFixed(2)} kWh`;
  const gridKwh = `${(d.grid_kwh ?? 0).toFixed(2)} kWh`;
  el("#mixSolarPct").textContent = solarLabel;
  el("#mixGridPct").textContent = gridLabel;
  el("#mixSolarKwh").textContent = known ? solarKwh : "–";
  el("#mixGridKwh").textContent = known ? gridKwh : "–";
  el("#mixTotal").textContent = known ? `${(d.input_kwh ?? 0).toFixed(2)} kWh in` : "";

  // The kWh goes on the mark itself, which is the figure being asked for and
  // saves a glance down to the key. Only where the segment can hold it: below
  // this it clips to something like "2.3 k", and a truncated number is worse
  // than none. The keys underneath always carry both figures in full.
  const ROOM_FOR_A_LABEL = 0.18;
  el("#mixSolarBar").textContent = known && solar >= ROOM_FOR_A_LABEL ? solarKwh : "";
  el("#mixGridBar").textContent = known && grid >= ROOM_FOR_A_LABEL ? gridKwh : "";

  bar.setAttribute("aria-label", known
    ? `Energy in: ${solarKwh} solar (${solarLabel}), ${gridKwh} grid ` +
      `(${gridLabel}), ${(d.input_kwh ?? 0).toFixed(2)} kWh total.`
    : "Energy mix unavailable.");

  el("#mixNote").textContent = !known && d.solar_reported === false
    ? "This model does not report solar input, so the split is unknown — a " +
      "station with no PV sensor is not the same as one harvesting nothing."
    : !known
      ? "Nothing came in over this window."
      // The total is beside the heading now; this only carries the caveat.
      : "What came in, not what powered the load — the battery sits in between.";
}

const KWH = v => `${v.toFixed(2)} kWh`;

/**
 * The dashboard's power balance, integrated over the chosen window.
 *
 * Same conservation, in energy rather than watts, and the same refusal to
 * guess: without the pack's own contribution the residual silently absorbs
 * every watt-hour the battery moved, which over a day dwarfs the losses it
 * would claim to be.
 */
function renderEnergyBalance(d) {
  const block = el("#eBalance");
  const loss = d.conversion_kwh;
  const batt = d.battery_kwh;
  const known = d.battery_reported === true && loss != null && batt != null;
  block.hidden = !known;
  if (!known) {
    el("#eBalNote").textContent =
      "The battery's own contribution was not recorded over this window, and " +
      "without it the rest cannot be balanced. New samples carry it, so this " +
      "fills in as the window moves forward.";
    return;
  }

  el("#eBalIn").textContent = KWH(d.input_kwh ?? 0);
  el("#eBalInParts").textContent =
    `grid ${(d.grid_kwh ?? 0).toFixed(2)} · solar ${(d.solar_kwh ?? 0).toFixed(2)}`;
  el("#eBalOut").textContent = KWH(d.load_kwh ?? 0);

  el("#eBalBattK").textContent =
    batt >= 0 ? "Net into the battery" : "Net out of the battery";
  el("#eBalBatt").textContent = `${batt >= 0 ? "+" : "−"}${KWH(Math.abs(batt))}`;
  // Net, not throughput: a pack that filled and emptied twice over shows near
  // zero here, and saying so stops that reading as "the battery did nothing".
  el("#eBalBattD").textContent = "net change over the window";

  const bogus = loss < 0;
  el("#eBalLoss").textContent = bogus ? "–" : KWH(loss);
  el("#eBalLossD").textContent = bogus
    ? "sensors disagree over this window"
    : d.input_kwh > 0 ? `${(loss / d.input_kwh * 100).toFixed(1)}% of what came in` : "";

  const hours = d.span_hours || 0;
  el("#eBalNote").textContent = bogus
    ? "The ports add up to more than the input over this window, which is " +
      "measurement noise accumulating rather than a reading."
    : `That is ${hours > 0 ? `${Math.round(loss * 1000 / hours)} W on average, ` : ""}` +
      "burned by the inverter, the charger and the unit's own electronics. It " +
      "is charged whether or not anything is plugged in, so it is the floor " +
      "under every runtime estimate.";
}

// ---------------------------------------------------------------------- //
// Solar production, by local calendar day
// ---------------------------------------------------------------------- //

const KWH2 = v => `${v.toFixed(2)} kWh`;
// Calendar-scale figures: a day's total does not move fast enough to be worth
// re-querying a 90-day grouped scan every minute the tab is open.
const SOLAR_REFRESH_MS = 5 * MINUTE;
const SOLAR_DAYS_KEY = "ecoflow_solar_days";

const loadSolarDays = () => Number(localStorage.getItem(SOLAR_DAYS_KEY)) || 30;

/** "Tue 26 Aug", in the browser's locale. */
function dayLabel(iso, opts) {
  // Parsed with an explicit time: a bare "2026-08-26" is UTC midnight, which in
  // any western timezone is the 25th.
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined,
    opts || { weekday: "short", day: "numeric", month: "short" });
}

const monthLabel = iso =>
  new Date(`${iso}T00:00:00`).toLocaleDateString(undefined,
    { month: "long", year: "numeric" });

const clock = minutes =>
  `${String(Math.floor(minutes / 60)).padStart(2, "0")}:` +
  `${String(Math.round(minutes) % 60).padStart(2, "0")}`;

/**
 * Today against yesterday *at the same point in the day*.
 *
 * Against yesterday's total it would read as a collapse every morning, which is
 * the one comparison guaranteed to be useless.
 */
function paceNote(today, yesterday, minutes) {
  const when = minutes == null ? "" : ` at ${clock(minutes)}`;
  if (today == null || yesterday == null) return "No comparable day recorded yet.";
  // A percentage off a near-zero base is noise: 20 Wh against 5 Wh is "300%
  // ahead" and says nothing at dawn.
  if (yesterday < 0.05 && today < 0.05) return `Neither day had started${when}.`;
  if (yesterday < 0.05) return `Yesterday had produced nothing by this point.`;
  const delta = (today - yesterday) / yesterday * 100;
  if (Math.abs(delta) < 2) return `Level with yesterday${when}.`;
  return `${Math.round(Math.abs(delta))}% ` +
    `${delta > 0 ? "ahead of" : "behind"} yesterday${when}.`;
}

/** The bucket the daily peak was averaged over, named for a sentence. */
const peakWindow = seconds =>
  seconds === 3600 ? "hour" : formatBucket(seconds);

function dayDetail(day, bucketSeconds) {
  const when = dayLabel(day.date);
  if (!day.hours) return `${when} · nothing recorded`;
  const bits = [when];
  if (day.solar_kwh != null) bits.push(`${KWH2(day.solar_kwh)} solar`);
  bits.push(`${KWH2(day.grid_kwh ?? 0)} from the grid`);
  if (day.solar_share != null) {
    bits.push(`${Math.round(day.solar_share * 100)}% solar`);
  }
  if (day.peak_w) bits.push(`best ${peakWindow(bucketSeconds)} ${day.peak_w} W`);
  // Said out loud, because a short bar on a partly-recorded day looks exactly
  // like a short bar on a dull one.
  if (!day.whole && day.hours) bits.push(`only ${day.hours.toFixed(1)} h recorded`);
  return bits.join(" · ");
}

/**
 * One stacked bar per calendar day: solar at the bottom, grid above it.
 *
 * Solar sits on the baseline so its top edge is comparable straight across the
 * month; the grid on top makes the whole bar the day's input, which is the
 * other half of the question -- how much you still had to buy.
 */
function renderDayBars(d) {
  const host = el("#solarBars");
  const days = d.days || [];
  const peak = Math.max(
    0.001, ...days.map(x => (x.solar_kwh || 0) + (x.grid_kwh || 0)));
  host.innerHTML = days.map(x => {
    const solar = x.solar_kwh || 0, grid = x.grid_kwh || 0;
    const label = escapeHtml(dayDetail(x, d.bucket_seconds));
    return `<div class="daybar${x.hours ? "" : " missing"}" role="option"
              id="sd-${escapeHtml(x.date)}" aria-selected="false" aria-label="${label}">
      <i class="seg-grid" style="height:${grid / peak * 100}%"></i>
      <i class="seg-solar" style="height:${solar / peak * 100}%"></i>
    </div>`;
  }).join("");
  el("#solAxisFrom").textContent = days.length ? dayLabel(days[0].date) : "";
  el("#solAxisTo").textContent =
    days.length > 1 ? dayLabel(days.at(-1).date) : "";
}

/**
 * Bring the selected day into view by scrolling the strip and nothing else.
 *
 * scrollIntoView would do it, but "nearest" is nearest in both axes: with the
 * card below the fold it drags the whole page down on first render, which is
 * not what selecting a bar asked for.
 */
function revealDay(host, bar) {
  if (!bar) return;
  const left = bar.offsetLeft - host.scrollLeft;
  if (left < 0) host.scrollLeft = bar.offsetLeft;
  else if (left + bar.offsetWidth > host.clientWidth) {
    host.scrollLeft = bar.offsetLeft + bar.offsetWidth - host.clientWidth;
  }
}

function renderSolarTotals(d) {
  const w = d.window || {};
  const month = d.month, prev = d.prev_month;
  const perDay = m => m.days ? (m.solar_kwh / m.days).toFixed(2) : "–";

  el("#solMonthK").textContent = month ? monthLabel(month.month) : "This month";
  el("#solMonth").textContent = month ? KWH2(month.solar_kwh) : "–";
  // Months are different lengths and this one is nearly always half-finished,
  // so they are compared per day, never total against total.
  el("#solPrevMonth").textContent = !month ? ""
    : prev
      ? `${monthLabel(prev.month)} came to ${KWH2(prev.solar_kwh)} — ` +
        `${perDay(prev)} a day against ${perDay(month)} so far.`
      : `${month.days} day${month.days === 1 ? "" : "s"} recorded so far.`;

  el("#solAvg").textContent =
    w.daily_avg_kwh == null ? "–" : KWH2(w.daily_avg_kwh);
  el("#solAvgD").textContent = w.whole_days
    ? `Across ${w.whole_days} whole day${w.whole_days === 1 ? "" : "s"}; ` +
      "part-days are left out."
    : "No whole day recorded yet.";

  el("#solBest").textContent = d.best ? KWH2(d.best.solar_kwh) : "–";
  el("#solBestD").textContent = d.best
    ? dayLabel(d.best.date, { weekday: "long", day: "numeric", month: "long" })
    : "";

  el("#solShare").textContent =
    w.solar_share == null ? "–" : `${Math.round(w.solar_share * 100)}%`;
  el("#solShareD").textContent = w.solar_kwh == null ? ""
    : `${KWH2(w.solar_kwh)} solar against ${KWH2(w.grid_kwh ?? 0)} bought.`;

  el("#solBarsTotal").textContent = w.days
    ? `${KWH2(w.solar_kwh ?? 0)} over ${w.days} day${w.days === 1 ? "" : "s"}` : "";
}

/**
 * Hidden outright on a station whose PV input is never reported, rather than
 * charted as a month of flat zero -- which is what a rainy month looks like.
 */
function renderSolar(d) {
  const card = el("#solarCard");
  card.hidden = !(d && d.enabled && d.reported);
  if (card.hidden) return;

  const p = d.pace || {};
  el("#solToday").textContent =
    p.today_kwh == null ? "–" : p.today_kwh.toFixed(2);
  el("#solYest").textContent =
    p.yesterday_kwh == null ? "–" : p.yesterday_kwh.toFixed(2);
  el("#solPace").textContent =
    paceNote(p.today_kwh, p.yesterday_kwh, p.through_minutes);
  el("#solYestTotal").textContent = p.yesterday_total_kwh == null
    ? "" : `Finished the day at ${KWH2(p.yesterday_total_kwh)}.`;

  renderDayBars(d);
  renderSolarTotals(d);
  el("#solarNote").textContent =
    "Days run midnight to midnight in this machine's timezone. Solar is " +
    "metered where it enters the station, so some of it is lost charging the " +
    "pack before it reaches the load.";
}

export const energy = {
  needs: ["state"],

  mount() {
    this.solarDays = loadSolarDays();
    this.solarDate = null;
    for (const b of els("#solarRange button")) {
      b.classList.toggle("active", Number(b.dataset.days) === this.solarDays);
    }
    this._onSolarRange = e => {
      const btn = e.target.closest("[data-days]");
      if (!btn) return;
      for (const b of els("#solarRange button")) b.classList.toggle("active", b === btn);
      this.solarDays = Number(btn.dataset.days);
      localStorage.setItem(SOLAR_DAYS_KEY, String(this.solarDays));
      // A different range is a different set of days; keeping the old selection
      // would silently point at whichever day now sits at that position.
      this.solarDate = null;
      this.refreshSolar();
    };
    el("#solarRange").addEventListener("click", this._onSolarRange);

    this._onBarClick = e => {
      const bar = e.target.closest(".daybar");
      if (!bar) return;
      this.selectDay([...el("#solarBars").children].indexOf(bar));
      el("#solarBars").focus();
    };
    el("#solarBars").addEventListener("click", this._onBarClick);

    this._onBarKey = e => {
      const last = (this.solarData?.days || []).length - 1;
      if (last < 0) return;
      const at = this.dayIndex();
      const moves = {
        ArrowLeft: at - 1, ArrowRight: at + 1,
        Home: 0, End: last,
        PageDown: at - 7, PageUp: at + 7,
      };
      if (!(e.key in moves)) return;
      e.preventDefault();
      this.selectDay(Math.max(0, Math.min(moves[e.key], last)));
    };
    el("#solarBars").addEventListener("keydown", this._onBarKey);

    this.refresh();
    this.refreshSolar();
    this._timer = setInterval(() => this.refresh(), 60000);
    this._solarTimer = setInterval(() => this.refreshSolar(), SOLAR_REFRESH_MS);
  },

  unmount() {
    clearInterval(this._timer);
    clearInterval(this._solarTimer);
    el("#solarRange")?.removeEventListener("click", this._onSolarRange);
    el("#solarBars")?.removeEventListener("click", this._onBarClick);
    el("#solarBars")?.removeEventListener("keydown", this._onBarKey);
  },

  /** The selected day's position, re-derived by date so a refresh cannot slide it. */
  dayIndex() {
    const days = this.solarData?.days || [];
    const at = days.findIndex(x => x.date === this.solarDate);
    return at >= 0 ? at : days.length - 1;
  },

  selectDay(index) {
    const days = this.solarData?.days || [];
    const at = Math.max(0, Math.min(index, days.length - 1));
    const day = days[at];
    if (!day) return;
    this.solarDate = day.date;
    const host = el("#solarBars");
    [...host.children].forEach((node, n) =>
      node.setAttribute("aria-selected", String(n === at)));
    host.setAttribute("aria-activedescendant", `sd-${day.date}`);
    el("#solDetail").textContent = dayDetail(day, this.solarData.bucket_seconds);
    revealDay(host, host.children[at]);
  },

  async refreshSolar() {
    const s = stateStore.get();
    if (s && s.history_enabled === false) {
      el("#solarCard").hidden = true;
      return;
    }
    try {
      const d = await api("api/solar?" + new URLSearchParams({ days: this.solarDays }));
      this.solarData = d;
      renderSolar(d);
      if (!el("#solarCard").hidden) this.selectDay(this.dayIndex());
    } catch (err) {
      el("#solarNote").textContent =
        `Solar history unavailable: ${err.reason || err.message}`;
    }
  },

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
      renderMix(d);
      renderEnergyBalance(d);
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
        ? `Grid draw priced by HC window ${d.hc_window}. Over a window ` +
          `shorter than a full charge cycle these figures are skewed by the ` +
          `battery's own level moving; they settle over days.`
        : "Enable pricing in Settings to see cost.";
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
