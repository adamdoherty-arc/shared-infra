// Shared token handling + fetch wrapper for every infractl UI page.
// Token lives in localStorage ONLY (never sent anywhere but this origin's
// own API, never logged, never rendered back to the DOM).
function getToken() {
  return localStorage.getItem("infractl_token") || "";
}
function setToken(t) {
  localStorage.setItem("infractl_token", t);
}
async function ensureToken() {
  let t = getToken();
  if (!t) {
    t = prompt("infractl token (X-Infractl-Token) — stored only in this browser's localStorage:");
    if (t) setToken(t);
  }
  return t;
}
async function api(path, opts) {
  opts = opts || {};
  const token = await ensureToken();
  const headers = Object.assign({ "X-Infractl-Token": token, "Content-Type": "application/json" }, opts.headers || {});
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (resp.status === 401) {
    localStorage.removeItem("infractl_token");
    throw new Error("invalid token — cleared, reload to re-enter");
  }
  const body = await resp.json().catch(() => ({ ok: false, error: { message: resp.statusText } }));
  if (!body.ok) {
    throw new Error((body.error && body.error.message) || "request failed");
  }
  return body.data;
}
function fmtTs(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19) + "Z";
}
function el(tag, attrs, children) {
  const e = document.createElement(tag);
  for (const k in attrs || {}) {
    if (k === "text") e.textContent = attrs[k];
    else if (k === "class") e.className = attrs[k];
    else e.setAttribute(k, attrs[k]);
  }
  (children || []).forEach((c) => e.appendChild(c));
  return e;
}
