/**
 * A small canvas time-series chart: zoom, pan, data dots and a snapped crosshair.
 *
 * Points are plotted against their timestamp, not their array index, which is
 * what makes an arbitrary time window (and therefore zoom and pan) possible. The
 * chart owns only the window; it never fetches. It emits "windowchange" and the
 * caller re-fetches at the new resolution, so the same engine works for any
 * series the API can return.
 *
 * All drawing is done in CSS pixels via a devicePixelRatio transform, so the
 * canvas stays sharp on a phone, and every hit-test works in the same units.
 */

const MIN_SPAN_MS = 30 * 1000;
const MAX_SPAN_MS = 30 * 24 * 3600 * 1000;
const PAD = { top: 14, right: 54, bottom: 24, left: 44 };
// Gridline intervals. The percentage axis uses the same four (0/25/50/75/100),
// so both axes label the same lines and neither floats between them.
const DIVISIONS = 4;
// Below this spacing individual dots merge into noise, so we draw the line only.
const DOT_MIN_STEP_PX = 8;
const HIT_RADIUS_PX = 24;
// A hole wider than this many buckets is a real gap in the data, not a segment.
const GAP_FACTOR = 2.5;
const SECOND = 1000, MINUTE = 60 * SECOND, HOUR = 60 * MINUTE, DAY = 24 * HOUR;
const TICK_LADDER = [
  SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND, 15 * SECOND, 30 * SECOND,
  MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR,
  DAY, 2 * DAY, 7 * DAY,
];

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/** Round gridline steps, ascending: 1, 2, 5 per decade, so labels stay whole. */
function* niceSteps() {
  for (let exp = 0; exp < 9; exp++) {
    for (const mult of [1, 2, 5]) yield mult * Math.pow(10, exp);
  }
}

/**
 * A watt scale covering [trough, peak] in DIVISIONS steps of a round size.
 *
 * Battery watts are signed -- positive charging, negative supplying the load --
 * so the axis has to be able to open a negative half. Zero always lands on a
 * gridline, which is what makes the sign readable without reading the labels;
 * any spare divisions go above it, where every other series lives.
 */
export function wattScale(trough, peak) {
  // A floor of 10 W keeps a flat-zero chart from collapsing onto its baseline.
  const up = Math.max(peak, 10);
  const down = Math.max(-Math.min(trough, 0), 0);
  for (const step of niceSteps()) {
    const below = Math.ceil(down / step);
    if (Math.ceil(up / step) + below <= DIVISIONS) {
      return { min: -below * step, max: (DIVISIONS - below) * step };
    }
  }
  return { min: 0, max: DIVISIONS * 1e8 };
}

/**
 * Floor a timestamp to a tick boundary in *local* time, so "00:00" lands on
 * local midnight rather than drifting with the UTC offset.
 */
function floorLocal(ms, interval) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  if (interval >= DAY) return d.getTime();
  const dayStart = d.getTime();
  return dayStart + Math.floor((ms - dayStart) / interval) * interval;
}

function pickInterval(span, plotWidth, minSpacingPx = 64) {
  const maxTicks = Math.max(2, Math.floor(plotWidth / minSpacingPx));
  return TICK_LADDER.find(i => span / i <= maxTicks) ?? TICK_LADDER.at(-1);
}

function formatTick(ms, interval) {
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (interval >= DAY) return `${d.getDate()}/${d.getMonth() + 1}`;
  if (interval >= HOUR && hh === "00" && mm === "00") {
    return `${d.getDate()}/${d.getMonth() + 1}`;
  }
  if (interval < MINUTE) return `${hh}:${mm}:${String(d.getSeconds()).padStart(2, "0")}`;
  return `${hh}:${mm}`;
}

/** Human-readable bucket width, for the "30 s average" caption. */
export function formatBucket(seconds) {
  if (!seconds) return "";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

export function createChart(canvas, options = {}) {
  const ctx = canvas.getContext("2d");
  const wrap = canvas.parentElement;
  const listeners = new Map();
  let series = options.series ?? [];
  let points = [];
  let stamps = [];           // parallel epoch-ms array, for binary search
  let bucketMs = 0;
  let t0 = Date.now() - HOUR, t1 = Date.now();
  let live = true;
  let hidden = new Set();
  let hover = null;
  // Sticky, with hysteresis, so panning doesn't jitter. wattMin stays 0 until a
  // visible series actually goes negative.
  let wattMax = 100, wattMin = 0;
  let cssW = 0, cssH = 0;
  let frame = 0;
  let tooltip = null;

  // -- events ------------------------------------------------------------- //
  function on(name, fn) {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name).add(fn);
    return () => listeners.get(name).delete(fn);
  }
  function emit(name, payload) {
    for (const fn of listeners.get(name) ?? []) fn(payload);
  }

  let windowTimer = 0;
  function emitWindow(immediate) {
    clearTimeout(windowTimer);
    const fire = () => emit("windowchange", { since: t0, until: t1, live });
    if (immediate) fire();
    else windowTimer = setTimeout(fire, 250);
  }

  // -- geometry ----------------------------------------------------------- //
  const plotW = () => Math.max(1, cssW - PAD.left - PAD.right);
  const plotH = () => Math.max(1, cssH - PAD.top - PAD.bottom);
  const xOf = t => PAD.left + ((t - t0) / (t1 - t0)) * plotW();
  const tOf = x => t0 + ((x - PAD.left) / plotW()) * (t1 - t0);
  const yOf = (v, min, max) => PAD.top + plotH() - ((v - min) / (max - min)) * plotH();
  /** Same, but reading the range off whichever axis the series belongs to. */
  const yFor = (v, spec) =>
    spec.axis === "pct" ? yOf(v, 0, 100) : yOf(v, wattMin, wattMax);
  const WATTS = { axis: "w" };   // for placing the axis's own labels and rules

  function colorOf(spec) {
    const styles = getComputedStyle(canvas);
    return styles.getPropertyValue(spec.color).trim() || spec.color;
  }

  // -- window ------------------------------------------------------------- //
  function setWindow(since, until, opts = {}) {
    const span = clamp(until - since, MIN_SPAN_MS, MAX_SPAN_MS);
    t1 = until;
    t0 = until - span;
    if (opts.live !== undefined) live = opts.live;
    schedule();
    if (!opts.silent) emitWindow(opts.immediate);
  }

  function zoomAbout(anchorT, factor) {
    const span = clamp((t1 - t0) / factor, MIN_SPAN_MS, MAX_SPAN_MS);
    const ratio = (anchorT - t0) / (t1 - t0);
    t0 = anchorT - span * ratio;
    t1 = t0 + span;
    // Zooming while pinned to now keeps you pinned; otherwise it frees the view.
    live = live && t1 >= Date.now() - span * 0.02;
    schedule();
    emitWindow();
  }

  function panBy(deltaT) {
    t0 += deltaT;
    t1 += deltaT;
    live = t1 >= Date.now() - (t1 - t0) * 0.02;
    schedule();
    emitWindow();
  }

  // -- data --------------------------------------------------------------- //
  function setData(next, meta = {}) {
    points = Array.isArray(next) ? next : [];
    stamps = points.map(p => new Date(p.ts).getTime());
    bucketMs = (meta.bucketSeconds ?? 0) * 1000;
    schedule();
  }

  /** Nearest point to a timestamp, or null when the cursor is over a gap. */
  function nearest(t) {
    if (!stamps.length) return null;
    let lo = 0, hi = stamps.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (stamps[mid] < t) lo = mid + 1; else hi = mid;
    }
    const cands = [lo - 1, lo].filter(i => i >= 0 && i < stamps.length);
    let best = null, bestDist = Infinity;
    for (const i of cands) {
      const d = Math.abs(stamps[i] - t);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    return best;
  }

  // -- rendering ---------------------------------------------------------- //
  function schedule() {
    if (frame || document.hidden) return;
    frame = requestAnimationFrame(() => { frame = 0; draw(); });
  }

  function measure() {
    const rect = wrap.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cssW = Math.max(1, rect.width);
    cssH = Math.max(1, canvas.clientHeight || rect.height);
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function visibleSeries() {
    return series.filter(s => !hidden.has(s.key));
  }

  function rescaleWatts() {
    let peak = 0, trough = 0;
    for (let i = 0; i < points.length; i++) {
      if (stamps[i] < t0 || stamps[i] > t1) continue;
      for (const s of visibleSeries()) {
        if (s.axis !== "w") continue;
        const v = points[i][s.key];
        if (v == null) continue;
        peak = Math.max(peak, v);
        trough = Math.min(trough, v);
      }
    }
    const next = wattScale(trough, peak);
    // Hysteresis: grow at once, but shrink only when both ends have real slack,
    // so the axis does not twitch on every frame while dragging.
    const roomy = peak < wattMax * 0.55 && trough >= wattMin * 0.55;
    if (next.max > wattMax || next.min < wattMin || roomy) {
      wattMax = next.max;
      wattMin = next.min;
    }
  }

  function drawGrid(css) {
    const interval = pickInterval(t1 - t0, plotW());
    ctx.strokeStyle = css.grid;
    ctx.fillStyle = css.dim;
    ctx.lineWidth = 1;
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let t = floorLocal(t0, interval); t <= t1; t += interval) {
      if (t < t0) continue;
      const x = Math.round(xOf(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, PAD.top + plotH());
      ctx.stroke();
      ctx.fillText(formatTick(t, interval), x, PAD.top + plotH() + 6);
    }
    // Horizontal gridlines double as the percentage axis.
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= DIVISIONS; i++) {
      const pct = (100 / DIVISIONS) * i;
      const y = Math.round(yOf(pct, 0, 100)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(PAD.left + plotW(), y);
      ctx.stroke();
      ctx.fillText(`${pct}%`, PAD.left - 6, y);
    }
    ctx.textAlign = "left";
    for (let i = 0; i <= DIVISIONS; i++) {
      const v = wattMin + ((wattMax - wattMin) / DIVISIONS) * i;
      ctx.fillText(`${Math.round(v)}W`, PAD.left + plotW() + 6, yFor(v, WATTS) + 0.5);
    }
    // With a negative half open, which line is zero stops being obvious, and
    // every signed series is read against it.
    if (wattMin < 0) {
      const y = Math.round(yFor(0, WATTS)) + 0.5;
      ctx.strokeStyle = css.dim;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(PAD.left + plotW(), y);
      ctx.stroke();
    }
  }

  function drawSeries(spec) {
    const color = colorOf(spec);
    const gap = bucketMs ? bucketMs * GAP_FACTOR : Infinity;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    // Dashes are the second channel on a series whose colour sits close to a
    // neighbour's under red-green colour blindness: the shape says which is
    // which when the hue cannot.
    ctx.setLineDash(spec.dash ?? []);
    ctx.beginPath();
    let pen = false;
    for (let i = 0; i < points.length; i++) {
      const v = points[i][spec.key];
      if (v == null) { pen = false; continue; }
      const x = xOf(stamps[i]), y = yFor(v, spec);
      // Break the line across real outages instead of drawing through them.
      if (!pen || (i > 0 && stamps[i] - stamps[i - 1] > gap)) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
      pen = true;
    }
    ctx.stroke();
    ctx.setLineDash([]);

    const step = plotW() / Math.max(1, points.length - 1);
    if (step >= DOT_MIN_STEP_PX) {
      ctx.fillStyle = color;
      const r = Math.min(3, step / 3);
      for (let i = 0; i < points.length; i++) {
        const v = points[i][spec.key];
        if (v == null || stamps[i] < t0 || stamps[i] > t1) continue;
        ctx.beginPath();
        ctx.arc(xOf(stamps[i]), yFor(v, spec), r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  function drawCrosshair(css) {
    if (hover == null || hover >= points.length) return;
    const x = xOf(stamps[hover]);
    if (x < PAD.left || x > PAD.left + plotW()) return;
    ctx.strokeStyle = css.crosshair;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, PAD.top);
    ctx.lineTo(Math.round(x) + 0.5, PAD.top + plotH());
    ctx.stroke();
    for (const s of visibleSeries()) {
      const v = points[hover][s.key];
      if (v == null) continue;
      ctx.fillStyle = colorOf(s);
      ctx.strokeStyle = css.bg;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(x, yFor(v, s), 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
  }

  function draw() {
    if (!cssW) measure();
    const styles = getComputedStyle(canvas);
    const css = {
      grid: styles.getPropertyValue("--chart-grid").trim() || "#222631",
      dim: styles.getPropertyValue("--text-dim").trim() || "#8a93a6",
      bg: styles.getPropertyValue("--surface").trim() || "#151823",
      crosshair: styles.getPropertyValue("--chart-crosshair").trim() || "#556",
    };
    ctx.clearRect(0, 0, cssW, cssH);
    rescaleWatts();
    drawGrid(css);
    if (!points.length) {
      ctx.fillStyle = css.dim;
      ctx.font = "13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("no samples in this window", PAD.left + plotW() / 2, cssH / 2);
      return;
    }
    for (const s of visibleSeries()) drawSeries(s);
    drawCrosshair(css);
  }

  // -- tooltip ------------------------------------------------------------ //
  function ensureTooltip() {
    if (tooltip) return tooltip;
    tooltip = document.createElement("div");
    tooltip.className = "chart-tip";
    tooltip.setAttribute("role", "presentation");
    wrap.appendChild(tooltip);
    return tooltip;
  }

  function showTooltip(index, clientX) {
    const tip = ensureTooltip();
    const p = points[index];
    const when = new Date(stamps[index]).toLocaleString();
    let html = `<div class="chart-tip-time">${when}</div>`;
    for (const s of visibleSeries()) {
      const v = p[s.key];
      // An explicit "+" on a signed series: "49W" beside "-49W" is ambiguous
      // about which way the pack is going until you spot the minus.
      const sign = s.signed && v > 0 ? "+" : "";
      html += `<div class="chart-tip-row"><i style="background:${colorOf(s)}"></i>` +
        `${s.label}<b>${v == null ? "–" : sign + Math.round(v) + s.unit}</b></div>`;
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const rect = wrap.getBoundingClientRect();
    let left = clientX - rect.left + 14;
    if (left + tip.offsetWidth > rect.width) {
      left = clientX - rect.left - tip.offsetWidth - 14;
    }
    tip.style.left = `${Math.max(0, left)}px`;
    emit("hover", { point: p, ts: stamps[index] });
  }

  function hideTooltip() {
    if (tooltip) tooltip.style.display = "none";
    hover = null;
    schedule();
    emit("hover", null);
  }

  // -- interaction -------------------------------------------------------- //
  const pointers = new Map();
  let drag = null;
  let pinch = null;

  function localX(e) {
    return e.clientX - canvas.getBoundingClientRect().left;
  }

  function onWheel(e) {
    // Must be non-passive: without preventDefault the page scrolls instead.
    e.preventDefault();
    let dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 16;          // lines
    else if (e.deltaMode === 2) dy *= cssH;   // pages
    zoomAbout(tOf(localX(e)), Math.exp(-dy * 0.0015));
  }

  function onPointerDown(e) {
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, localX(e));
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { dist: Math.abs(a - b) || 1, anchor: tOf((a + b) / 2), span: t1 - t0 };
      drag = null;
    } else if (pointers.size === 1) {
      drag = { x: localX(e), t0, t1 };
    }
  }

  function onPointerMove(e) {
    const x = localX(e);
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, x);

    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const dist = Math.abs(a - b) || 1;
      const span = clamp(pinch.span * (pinch.dist / dist), MIN_SPAN_MS, MAX_SPAN_MS);
      const ratio = (pinch.anchor - t0) / (t1 - t0);
      t0 = pinch.anchor - span * ratio;
      t1 = t0 + span;
      live = false;
      schedule();
      emitWindow();
      return;
    }

    if (drag) {
      const span = drag.t1 - drag.t0;
      const dt = ((drag.x - x) / plotW()) * span;
      t0 = drag.t0 + dt;
      t1 = drag.t1 + dt;
      live = t1 >= Date.now() - span * 0.02;
      schedule();
      emitWindow();
      return;
    }

    // Hover: snap the crosshair to the nearest point, and drop it over gaps.
    const i = nearest(tOf(x));
    if (i == null) return;
    if (Math.abs(xOf(stamps[i]) - x) > HIT_RADIUS_PX) { hideTooltip(); return; }
    hover = i;
    schedule();
    showTooltip(i, e.clientX);
  }

  function endPointer(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 0) {
      if (drag) emitWindow(true);
      drag = null;
    } else if (pointers.size === 1) {
      drag = { x: [...pointers.values()][0], t0, t1 };
    }
  }

  function onKeyDown(e) {
    const span = t1 - t0;
    const mid = (t0 + t1) / 2;
    const keys = {
      ArrowLeft: () => panBy(-span * 0.15),
      ArrowRight: () => panBy(span * 0.15),
      "+": () => zoomAbout(mid, 1.4),
      "=": () => zoomAbout(mid, 1.4),
      "-": () => zoomAbout(mid, 1 / 1.4),
      "_": () => zoomAbout(mid, 1 / 1.4),
      End: () => setWindow(Date.now() - span, Date.now(), { live: true, immediate: true }),
      Home: () => stamps.length && setWindow(stamps[0], stamps[0] + span,
        { live: false, immediate: true }),
    };
    const fn = keys[e.key];
    if (!fn) return;
    e.preventDefault();
    fn();
  }

  const resizeObserver = new ResizeObserver(() => { measure(); schedule(); });
  resizeObserver.observe(wrap);

  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("pointerleave", hideTooltip);
  canvas.addEventListener("keydown", onKeyDown);
  // iOS pinches the whole page unless the gesture is claimed here too.
  canvas.addEventListener("gesturestart", e => e.preventDefault());

  measure();

  return {
    on,
    setData,
    setWindow,
    setSeries(next) { series = next; schedule(); },
    setVisible(key, visible) {
      if (visible) hidden.delete(key); else hidden.add(key);
      schedule();
    },
    isVisible: key => !hidden.has(key),
    getWindow: () => ({ since: t0, until: t1, live }),
    setLive(next) { live = next; },
    isLive: () => live,
    redraw: schedule,
    destroy() {
      resizeObserver.disconnect();
      clearTimeout(windowTimer);
      cancelAnimationFrame(frame);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", endPointer);
      canvas.removeEventListener("pointercancel", endPointer);
      canvas.removeEventListener("pointerleave", hideTooltip);
      canvas.removeEventListener("keydown", onKeyDown);
      tooltip?.remove();
      listeners.clear();
    },
  };
}
