/**
 * The four views. Each exports `needs` (which endpoints the scheduler should
 * poll while it is showing), plus mount/unmount so its timers and listeners die
 * on navigation.
 */

import {
  api, applyTheme, autoStore, el, els, escapeHtml, fmtMinutes, fmtMoney,
  fmtRuntime, getRefresh, getTheme, setRefresh, settingsStore, stateStore,
  toast,
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

function renderPorts(s) {
  // AC has a real on/off flag (flow_info_ac_out); USB/DC do not, so USB is
  // inferred from power draw and DC has no telemetry at all.
  const ac = s.ac_output_on;
  setPort("stAc", ac === true ? "on" : ac === false ? "off" : "unknown",
    ac === true ? "ON" : ac === false ? "OFF" : "?",
    ac == null ? "Awaiting the AC-output flag from the device." : "");

  const usbW = Math.round((s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0));
  setPort("stUsb", usbW > 0 ? "on" : "unknown",
    usbW > 0 ? `ON · ${usbW}W` : "— idle/off",
    usbW > 0 ? "" : "Inferred from power draw; the device reports no USB enable " +
      "flag, so 0 W means off OR on-but-idle.");

  setPort("stDc", "unknown", "n/a",
    "The DELTA 3 sends no 12V DC telemetry, so its on/off state is unknown.");

  el("#eveCtl").hidden = s.eve_enabled !== true;
  if (s.eve_enabled === true) {
    const on = s.eve_on;
    setPort("stEve", on === true ? "on" : on === false ? "off" : "unknown",
      on === true ? "ON" : on === false ? "OFF" : "?",
      on == null ? "Unknown until first command." : "Last commanded state.");
  }
  el("#sbCtl").hidden = s.switchbot_enabled !== true;
}

function renderState(s) {
  if (!s) return;
  const num = (id, value) => { el(id).textContent = value; };
  num("#soc", s.soc_percent ?? "–");
  el("#socFill").style.width = `${s.soc_percent ?? 0}%`;
  num("#acIn", Math.round(s.ac_input_watts ?? 0));
  num("#acOut", Math.round(s.ac_output_watts ?? 0));
  num("#usb", Math.round((s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0)));
  num("#runtime", fmtRuntime(s.runtime_seconds));
  const charging = (s.status || "").startsWith("OL");
  num("#remain", charging
    ? `chg ${fmtMinutes(s.remain_charge_minutes)}`
    : `dsg ${fmtMinutes(s.remain_discharge_minutes)}`);
  renderPorts(s);
  applyControlLock();
}

function renderAuto(a) {
  if (!a) return;
  let kind, text;
  if (!a.enabled) { kind = "off"; text = "Disabled"; }
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
  const hasToken = !!localStorage.getItem("ecoflow_token");
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
  // Guard: turning USB off can kill a Pi powered from the DELTA 3's USB port.
  if (output === "usb" && !enabled) {
    const ok = confirm(
      "Turn the USB output OFF?\n\n" +
      "If this bridge (e.g. a Raspberry Pi) is powered from the DELTA 3's USB " +
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
      el("#energyNote").textContent = d.pricing_enabled
        ? `Grid draw priced by HC window ${d.hc_window}. ` +
          `Load delivered: ${(d.load_kwh ?? 0).toFixed(2)} kWh.`
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
  if (field.type === "float_or_null" && (value === null || value === "")) return null;
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
      const placeholder = f.type === "float_or_null" ? ' placeholder="off"' : "";
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
        ? (field.type === "float_or_null" ? null : "")
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
    el("#saveSettings").disabled = this.hasErrors() || !localStorage.getItem("ecoflow_token");
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
