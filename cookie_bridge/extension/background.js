// Gemini Cookie Bridge — background script (Firefox and Chromium).
//
// Hands this browser's Google session cookies to the local Gemini MCP server
// through native messaging, so the server always uses the browser's current,
// signed-in session. Cookies go only to the native host registered on this
// computer (cookie_bridge/native_host.py); nothing is sent over the network.
// No cookie value is ever logged. The toolbar button shows the last result:
// no badge when synced, "!" with the reason (on hover) when something failed.

const api = globalThis.browser ?? globalThis.chrome;
const action = api.browserAction ?? api.action;
const HOST = "com.gemini_webapi_mcp.cookie_bridge";
const DOMAINS = new Set([".google.com", "gemini.google.com", ".gemini.google.com"]);
const ALARM = "gemini-cookie-bridge-resync";
const DEFAULT_STORES = new Set(["firefox-default", "0"]);
const IS_FIREFOX = typeof globalThis.browser !== "undefined"
  && typeof globalThis.browser.runtime.getBrowserInfo === "function";
const SOURCE = IS_FIREFOX ? "firefox" : (navigator.userAgent.includes("Edg/") ? "edge" : "chrome");

let timer = null;

const hasSession = (cookies) => cookies.some((c) => c.name === "__Secure-1PSID" && c.domain === ".google.com");

async function cookiesIn(storeId, extra = {}) {
  const query = { domain: "google.com", storeId, ...extra };
  let all;
  try {
    all = await api.cookies.getAll(query);
  } catch (err) {
    if (!IS_FIREFOX) throw err;
    all = await api.cookies.getAll({ ...query, firstPartyDomain: null }); // first-party isolation
  }
  return all.filter((c) => DOMAINS.has(c.domain) && c.value);
}

// Find the store holding the signed-in Google session: the default store first,
// then containers; partitioned cookies only as a last resort (Firefox).
async function findSession() {
  const stores = (await api.cookies.getAllCookieStores())
    .filter((s) => !s.incognito)
    .sort((a, b) => (DEFAULT_STORES.has(b.id) ? 1 : 0) - (DEFAULT_STORES.has(a.id) ? 1 : 0));
  const diag = [];
  let chosen = null;
  for (const store of stores) {
    const cookies = await cookiesIn(store.id);
    diag.push({ id: store.id, n: cookies.length, psid: hasSession(cookies) });
    if (!chosen && hasSession(cookies)) chosen = cookies;
  }
  if (!chosen && IS_FIREFOX) {
    for (const store of stores) {
      const cookies = await cookiesIn(store.id, { partitionKey: {} });
      diag.push({ id: `${store.id}+partitioned`, n: cookies.length, psid: hasSession(cookies) });
      if (hasSession(cookies)) { chosen = cookies; break; }
    }
  }
  return { chosen, diag };
}

function setStatus(badge, title) {
  try {
    action.setBadgeText({ text: badge });
    if (badge) action.setBadgeBackgroundColor({ color: "#d93025" });
    action.setTitle({ title });
  } catch (_) { /* no toolbar button */ }
}

async function push(reason) {
  try {
    const { chosen, diag } = await findSession();
    const cookies = (chosen || []).map((c) => ({
      domain: c.domain,
      name: c.name,
      value: c.value,
      path: c.path || "/",
      ...(c.expirationDate ? { expirationDate: c.expirationDate } : {}),
    }));
    // Sent even without a session: the host logs the per-store counts (no values).
    const reply = await api.runtime.sendNativeMessage(HOST, { type: "cookies", source: SOURCE, reason, cookies, diag });
    const time = new Date().toLocaleTimeString();
    if (reply && reply.ok) {
      setStatus("", `Gemini Cookie Bridge: ${reply.unchanged ? "up to date" : "synced"} (${reply.count} cookies) at ${time}`);
    } else {
      setStatus("!", `Gemini Cookie Bridge: ${reply ? reply.error : "no reply from the helper"} (${time})`);
    }
  } catch (err) {
    setStatus("!", `Gemini Cookie Bridge: ${err && err.message ? err.message : "failed"}`);
    console.warn("Gemini cookie bridge: push failed —", err && err.message);
  }
}

function schedule(reason) {
  clearTimeout(timer);
  timer = setTimeout(() => push(reason), 3000); // Google rotates several cookies at once
}

api.cookies.onChanged.addListener((info) => {
  if (info && info.cookie && DOMAINS.has(info.cookie.domain)) schedule("changed");
});
api.runtime.onStartup.addListener(() => push("startup"));
api.runtime.onInstalled.addListener(() => push("installed"));
if (action && action.onClicked) action.onClicked.addListener(() => push("clicked")); // click = sync now

// Safety resync every 15 minutes; created once so worker restarts don't reset it.
Promise.resolve(api.alarms.get(ALARM)).then((existing) => {
  if (!existing) api.alarms.create(ALARM, { periodInMinutes: 15 });
});
api.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) push("periodic");
});
