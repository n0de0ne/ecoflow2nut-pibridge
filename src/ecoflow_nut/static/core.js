/**
 * Shared plumbing: the API client, the auth token, a tiny store, formatting and
 * toasts. Everything here is view-agnostic.
 */

// Resolve against the document, never "/", so the UI works when a reverse proxy
// mounts the bridge under a sub-path.
const BASE = location.pathname.replace(/[^/]*$/, "");

const TOKEN_KEY = "ecoflow_token";
const THEME_KEY = "ecoflow_theme";
const REFRESH_KEY = "ecoflow_refresh";

export class ApiError extends Error {
  constructor(status, reason) {
    super(reason || `HTTP ${status}`);
    this.status = status;
    this.reason = reason;
  }
}

let token = localStorage.getItem(TOKEN_KEY) || "";
export const getToken = () => token;
export function setToken(next, remember = true) {
  token = next || "";
  if (remember && token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

/**
 * One fetch helper for every endpoint: attaches the token, applies a timeout,
 * and turns aiohttp's `reason` into a message worth showing the user.
 */
export async function api(path, { method = "GET", body, timeout = 8000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const resp = await fetch(BASE + path, {
      method,
      signal: controller.signal,
      headers: {
        ...(token ? { "X-Auth-Token": token } : {}),
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new ApiError(resp.status, data.reason || resp.statusText);
    }
    return await resp.json();
  } catch (err) {
    if (err.name === "AbortError") throw new ApiError(0, "request timed out");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// -- store ---------------------------------------------------------------- //

/** Minimal observable value. Views subscribe; nothing else re-renders. */
export function createStore(initial) {
  let value = initial;
  const subs = new Set();
  return {
    get: () => value,
    set(next) {
      value = typeof next === "function" ? next(value) : next;
      for (const fn of subs) fn(value);
    },
    subscribe(fn, { immediate = true } = {}) {
      subs.add(fn);
      if (immediate) fn(value);
      return () => subs.delete(fn);
    },
  };
}

export const stateStore = createStore(null);
export const autoStore = createStore(null);
export const settingsStore = createStore(null);
export const healthStore = createStore({ ok: true, reason: "" });

// -- formatting ----------------------------------------------------------- //

export function fmtMinutes(m) {
  if (m == null) return "–";
  if (m >= 6000) return "∞";
  const h = Math.floor(m / 60), mm = Math.round(m % 60);
  return h ? `${h}h ${mm}m` : `${mm}m`;
}

export function fmtRuntime(s) {
  if (s == null || s >= 99999) return "idle";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

export function fmtAgo(seconds) {
  if (seconds == null) return "never";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export const fmtMoney = (v, currency) => `${currency}${(v ?? 0).toFixed(2)}`;

// -- toasts --------------------------------------------------------------- //

export function toast(message, kind = "info") {
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = message;
  el.addEventListener("click", () => el.remove());
  host.appendChild(el);
  while (host.children.length > 3) host.firstChild.remove();
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
}

// -- theme ---------------------------------------------------------------- //

export const getTheme = () => localStorage.getItem(THEME_KEY) || "auto";

export function applyTheme(theme) {
  localStorage.setItem(THEME_KEY, theme);
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  const dark = theme === "dark" ||
    (theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? "#0f1115" : "#f5f6f8");
}

// -- refresh preference --------------------------------------------------- //

/** "auto" tracks how often the bridge publishes; anything else is milliseconds. */
export const getRefresh = () => localStorage.getItem(REFRESH_KEY) || "auto";
export const setRefresh = v => localStorage.setItem(REFRESH_KEY, v);

export function resolveRefreshMs(preference, publishIntervalSeconds) {
  if (preference !== "auto") return Number(preference);
  const base = (publishIntervalSeconds ?? 5) * 1000;
  return Math.min(Math.max(base, 1000), 10000);
}

// -- misc ----------------------------------------------------------------- //

export const el = sel => document.querySelector(sel);
export const els = sel => [...document.querySelectorAll(sel)];

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
