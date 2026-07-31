const $ = s => document.querySelector(s);
let token = localStorage.getItem("ecoflow_token") || "";
let historyMinutes = 1440;
let controlEnabled = false;
let historyEnabled = false;
let currency = "€";
let lastPoints = [];
let hoverIndex = null;

function authHeaders() { return token ? { "X-Auth-Token": token } : {}; }

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2800);
}

function fmtMins(m) {
  if (m == null) return "–";
  if (m >= 6000) return "∞";
  const h = Math.floor(m / 60), mm = m % 60;
  return h ? `${h}h ${mm}m` : `${mm}m`;
}
function fmtRuntime(s) {
  if (s == null || s >= 99999) return "idle";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function money(v) { return currency + (v ?? 0).toFixed(2); }

function setPort(id, cls, text, title) {
  const el = $("#" + id); if (!el) return;
  el.className = "pstat " + (cls === "on" ? "on" : cls === "off" ? "off" : "");
  el.innerHTML = `<span class="led ${cls}"></span>${text}`;
  el.title = title || "";
}
function updatePorts(s) {
  // AC has a real on/off flag (flow_info_ac_out); USB/DC do not, so USB is
  // inferred from power draw and DC has no telemetry at all.
  const ac = s.ac_output_on;
  setPort("stAc", ac === true ? "on" : ac === false ? "off" : "unknown",
    ac === true ? "ON" : ac === false ? "OFF" : "?",
    ac == null ? "Awaiting the AC-output flag from the device." : "");
  const usbW = Math.round((s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0));
  setPort("stUsb", usbW > 0 ? "on" : "unknown",
    usbW > 0 ? "ON · " + usbW + "W" : "— idle/off",
    usbW > 0 ? "" : "Inferred from power draw; the device reports no USB " +
                    "enable flag, so 0 W means off OR on-but-idle.");
  setPort("stDc", "unknown", "n/a",
    "The DELTA 3 sends no 12V DC telemetry, so its on/off state is unknown.");
}

async function refreshState() {
  try {
    const r = await fetch("api/state", { headers: authHeaders() });
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    controlEnabled = s.control_enabled;
    historyEnabled = s.history_enabled;
    $("#soc").textContent = s.soc_percent ?? "–";
    $("#socFill").style.width = (s.soc_percent ?? 0) + "%";
    $("#acIn").textContent = Math.round(s.ac_input_watts ?? 0);
    $("#acOut").textContent = Math.round(s.ac_output_watts ?? 0);
    const usb = (s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0);
    $("#usb").textContent = Math.round(usb);
    $("#runtime").textContent = fmtRuntime(s.runtime_seconds);
    const charging = (s.status || "").startsWith("OL");
    $("#remain").textContent = charging
      ? "chg " + fmtMins(s.remain_charge_minutes)
      : "dsg " + fmtMins(s.remain_discharge_minutes);
    const pill = $("#status");
    pill.textContent = s.status ?? "?";
    pill.className = "status-pill " +
      (s.status?.includes("LB") ? "status-LB" : s.status?.startsWith("OL") ? "status-OL" : "status-OB");
    updatePorts(s);
    const eveOn = s.eve_on;
    $("#eveCtl").style.display = s.eve_enabled === true ? "flex" : "none";
    if (s.eve_enabled === true) {
      setPort("stEve",
        eveOn === true ? "on" : eveOn === false ? "off" : "unknown",
        eveOn === true ? "ON" : eveOn === false ? "OFF" : "?",
        eveOn == null ? "Unknown until first command." : "Last commanded state.");
    }
    $("#sbCtl").style.display = s.switchbot_enabled === true ? "flex" : "none";
    applyControlState();
    $("#historyCard").style.display = historyEnabled ? "" : "none";
    $("#energyCard").style.display = historyEnabled ? "" : "none";
  } catch (e) {
    $("#status").textContent = "offline";
    $("#status").className = "status-pill";
  }
}

function applyControlState() {
  const need = controlEnabled && !token;
  const lock = !controlEnabled || need;
  document.querySelectorAll("[data-out]").forEach(b => b.disabled = lock);
  $("#asOn").disabled = $("#asOff").disabled = lock;
  $("#sbPress").disabled = lock;
  $("#saveSettings").disabled = lock;
  $("#tokenRow").style.display = controlEnabled ? "flex" : "none";
  $("#controlNote").textContent = !controlEnabled
    ? "Controls disabled (no auth_token configured on the bridge)."
    : need ? "Enter the control token to enable actions." : "";
}

async function control(output, enabled) {
  // Guard: turning USB off can kill a Pi powered from the DELTA 3's USB port.
  if (output === "usb" && !enabled) {
    if (!confirm(
      "Turn the USB output OFF?\n\n" +
      "If this bridge (e.g. a Raspberry Pi) is powered from the DELTA 3's USB " +
      "port, this cuts its OWN power — the dashboard and the bridge will go down.\n\n" +
      "Continue only if you are sure nothing critical runs off USB.")) {
      return;
    }
  }
  try {
    const r = await fetch("api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ output, enabled }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    toast(d.message || "ok");
    setTimeout(refreshState, 800);
  } catch (e) { toast("error: " + e.message); }
}

async function switchbotPress() {
  try {
    const r = await fetch("api/switchbot", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ action: "press" }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    toast(d.message || "pressed");
  } catch (e) { toast("error: " + e.message); }
}

async function refreshAuto() {
  try {
    const r = await fetch("api/autoshutdown", { headers: authHeaders() });
    if (!r.ok) return;
    const a = await r.json();
    let cls, txt;
    if (!a.enabled) { cls = "off"; txt = "Disabled"; }
    else if (a.triggered) { cls = "crit"; txt = "CUT sent"; }
    else if (a.armed) {
      cls = "warn";
      txt = "ARMED" + (a.seconds_until_cut != null
        ? ` · cutting in ${Math.round(a.seconds_until_cut)}s` : "");
    } else { cls = "on"; txt = "Monitoring"; }
    $("#asLed").className = "led " + cls;
    $("#asState").textContent = txt;
    let d = `trigger ≤ ${a.trigger_soc_percent}%, recover ${a.recover_soc_percent}%, ` +
            `grace ${a.grace_period_seconds}s, cuts: ${(a.cut_outputs || []).join(", ") || "none"}`;
    $("#asDetail").textContent = d;
  } catch (e) {}
}

// Enable/disable auto-shutdown via the settings endpoint (auto_shutdown.enabled).
async function setAuto(enabled) {
  const ok = await saveSettings({ "auto_shutdown.enabled": enabled }, true);
  if (ok) { toast("auto-shutdown " + (enabled ? "enabled" : "disabled")); refreshAuto(); }
}

// ---- chart with hover tooltip ----
const SERIES = [
  { key: "soc_percent", color: "#7ee2a8", max: 100, label: "SoC", unit: "%" },
  { key: "ac_output_watts", color: "#f2c969", max: null, label: "AC out", unit: "W" },
  { key: "ac_input_watts", color: "#a9c2ff", max: null, label: "AC in", unit: "W" },
];
const PAD = 28;
function xAt(i, n, W) { return PAD + (n < 2 ? 0 : (i / (n - 1)) * (W - 2 * PAD)); }
function wattMax(points) {
  let m = 100;
  for (const p of points) m = Math.max(m, p.ac_output_watts || 0, p.ac_input_watts || 0);
  return m;
}
function drawChart() {
  const c = $("#chart"), ctx = c.getContext("2d");
  const W = c.width, H = c.height, points = lastPoints;
  ctx.clearRect(0, 0, W, H);
  if (!points.length) {
    ctx.fillStyle = "#8a93a6"; ctx.font = "13px system-ui";
    ctx.fillText("no data yet", PAD, H / 2); return;
  }
  const wMax = wattMax(points), n = points.length;
  ctx.strokeStyle = "#222631"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD, H - PAD); ctx.lineTo(W - PAD, H - PAD); ctx.stroke();
  for (const s of SERIES) {
    const max = s.max || wMax;
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.8; ctx.beginPath();
    let started = false;
    points.forEach((p, i) => {
      const v = p[s.key]; if (v == null) return;
      const y = H - PAD - (v / max) * (H - 2 * PAD);
      if (!started) { ctx.moveTo(xAt(i, n, W), y); started = true; }
      else ctx.lineTo(xAt(i, n, W), y);
    });
    ctx.stroke();
  }
  if (hoverIndex != null && hoverIndex < n) {
    const x = xAt(hoverIndex, n, W);
    ctx.strokeStyle = "#3a4356"; ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(x, PAD - 8); ctx.lineTo(x, H - PAD); ctx.stroke();
    const p = points[hoverIndex];
    for (const s of SERIES) {
      const v = p[s.key]; if (v == null) continue;
      const max = s.max || wMax, y = H - PAD - (v / max) * (H - 2 * PAD);
      ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(x, y, 3, 0, 7); ctx.fill();
    }
  }
}
function showTip(p, clientX) {
  const tip = $("#tip"), wrap = $(".chart-wrap").getBoundingClientRect();
  const when = new Date(p.ts).toLocaleString();
  let rows = `<div class="t">${when}</div>`;
  for (const s of SERIES) {
    const v = p[s.key];
    rows += `<div><i style="background:${s.color}"></i>${s.label}: ` +
            `<b>${v == null ? "–" : Math.round(v) + s.unit}</b></div>`;
  }
  tip.innerHTML = rows; tip.style.display = "block";
  let left = clientX - wrap.left + 12;
  if (left + tip.offsetWidth > wrap.width) left = clientX - wrap.left - tip.offsetWidth - 12;
  tip.style.left = Math.max(0, left) + "px"; tip.style.top = "6px";
}
function onMove(e) {
  const c = $("#chart"), rect = c.getBoundingClientRect(), n = lastPoints.length;
  if (!n) return;
  const xCanvas = (e.clientX - rect.left) * (c.width / rect.width);
  let i = Math.round((xCanvas - PAD) / ((c.width - 2 * PAD) || 1) * (n - 1));
  i = Math.max(0, Math.min(n - 1, i));
  hoverIndex = i; drawChart(); showTip(lastPoints[i], e.clientX);
}
function onLeave() { hoverIndex = null; drawChart(); $("#tip").style.display = "none"; }

async function refreshHistory() {
  if (!historyEnabled) return;
  try {
    const r = await fetch("api/history?minutes=" + historyMinutes, { headers: authHeaders() });
    const d = await r.json();
    lastPoints = d.points || []; hoverIndex = null; drawChart();
    $("#historyNote").textContent = lastPoints.length
      ? `${lastPoints.length} points · hover for detail`
      : "Collecting data…";
  } catch (e) { $("#historyNote").textContent = "history unavailable"; }
}

async function refreshEnergy() {
  if (!historyEnabled) return;
  try {
    const r = await fetch("api/energy?minutes=" + historyMinutes, { headers: authHeaders() });
    const d = await r.json();
    if (d.currency) currency = d.currency;
    $("#energyRange").textContent = "· last " + fmtMins(historyMinutes);
    $("#eKwh").textContent = (d.grid_kwh ?? 0).toFixed(2);
    $("#eCost").textContent = d.pricing_enabled ? money(d.total_cost) : "—";
    $("#eHc").innerHTML = `${(d.hc_kwh ?? 0).toFixed(2)} kWh` +
      (d.pricing_enabled ? ` · <b>${money(d.hc_cost)}</b>` : "");
    $("#eHp").innerHTML = `${(d.hp_kwh ?? 0).toFixed(2)} kWh` +
      (d.pricing_enabled ? ` · <b>${money(d.hp_cost)}</b>` : "");
    $("#eAvg").textContent = `${Math.round(d.avg_grid_watts ?? 0)} / ${Math.round(d.peak_grid_watts ?? 0)} W`;
    $("#eProj").innerHTML = d.pricing_enabled
      ? `${money(d.cost_per_day)}/day · <b>${money(d.cost_per_month)}/mo</b>` : "—";
    $("#energyNote").textContent = d.pricing_enabled
      ? `Grid draw priced by HC window ${d.hc_window}. Load delivered: ${(d.load_kwh ?? 0).toFixed(2)} kWh.`
      : "Enable pricing in Settings to see cost. Load delivered: " + (d.load_kwh ?? 0).toFixed(2) + " kWh.";
  } catch (e) { $("#energyNote").textContent = "energy unavailable"; }
}

// ---- settings form ----
const inputId = k => "set_" + k.replace(/[^a-z0-9]/gi, "_");
function fieldInput(f, value) {
  const id = inputId(f.key);
  if (f.type === "bool")
    return `<input type="checkbox" id="${id}" data-key="${f.key}" ${value ? "checked" : ""}>`;
  if (f.type === "time")
    return `<input type="time" id="${id}" data-key="${f.key}" value="${value ?? ""}">`;
  if (f.type === "str")
    return `<input type="text" id="${id}" data-key="${f.key}" value="${value ?? ""}">`;
  const step = f.step != null ? `step="${f.step}"` : "";
  const mn = f.min != null ? `min="${f.min}"` : "", mx = f.max != null ? `max="${f.max}"` : "";
  return `<input type="number" id="${id}" data-key="${f.key}" data-type="${f.type}" ` +
         `${step} ${mn} ${mx} value="${value ?? ""}">`;
}
function renderSettings(schema, values) {
  const groups = {};
  for (const f of schema) (groups[f.group] = groups[f.group] || []).push(f);
  let html = "";
  for (const [group, fields] of Object.entries(groups)) {
    html += `<fieldset><legend>${group}</legend>`;
    for (const f of fields) {
      html += `<div class="frow"><label for="${inputId(f.key)}">${f.label}` +
              (f.help ? `<span class="help">${f.help}</span>` : "") + `</label>` +
              fieldInput(f, values[f.key]) + `</div>`;
    }
    html += `</fieldset>`;
  }
  $("#settingsForm").innerHTML = html;
}
async function loadSettings() {
  try {
    const r = await fetch("api/settings", { headers: authHeaders() });
    if (!r.ok) return;
    const d = await r.json();
    renderSettings(d.fields, d.values);
    applyControlState();
  } catch (e) {}
}
function collectSettings() {
  const out = {};
  document.querySelectorAll("#settingsForm [data-key]").forEach(el => {
    const k = el.dataset.key;
    if (el.type === "checkbox") out[k] = el.checked;
    else if (el.type === "number") {
      if (el.value === "") out[k] = (el.dataset.type === "float_or_null") ? null : 0;
      else out[k] = Number(el.value);
    } else out[k] = el.value;
  });
  return out;
}
async function saveSettings(updates, quiet) {
  try {
    const r = await fetch("api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ updates }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    if (!quiet) {
      $("#settingsNote").textContent = (d.changed || []).length
        ? `saved ${d.changed.length} change(s)` : "no changes";
      refreshEnergy();
    }
    return true;
  } catch (e) { if (!quiet) $("#settingsNote").textContent = "error: " + e.message;
                else toast("error: " + e.message); return false; }
}

document.querySelectorAll("[data-out]").forEach(b =>
  b.addEventListener("click", () => control(b.dataset.out, b.dataset.on === "1")));
$("#asOn").addEventListener("click", () => setAuto(true));
$("#asOff").addEventListener("click", () => setAuto(false));
$("#sbPress").addEventListener("click", switchbotPress);
$("#saveSettings").addEventListener("click", () => saveSettings(collectSettings(), false));
$("#saveToken").addEventListener("click", () => {
  token = $("#token").value.trim();
  localStorage.setItem("ecoflow_token", token);
  toast("token saved"); applyControlState(); refreshState(); loadSettings();
});
document.querySelectorAll(".range button").forEach(b =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".range button").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); historyMinutes = +b.dataset.min;
    refreshHistory(); refreshEnergy();
  }));
const chart = $("#chart");
chart.addEventListener("mousemove", onMove);
chart.addEventListener("mouseleave", onLeave);

$("#token").value = token;
refreshState(); refreshAuto(); refreshHistory(); refreshEnergy(); loadSettings();
setInterval(refreshState, 4000);
setInterval(refreshAuto, 8000);
setInterval(() => { if (hoverIndex == null) refreshHistory(); }, 30000);
setInterval(refreshEnergy, 60000);
