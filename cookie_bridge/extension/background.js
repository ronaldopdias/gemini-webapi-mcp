// Gemini Cookie Bridge — background script (Firefox and Chromium).
//
// Hands this browser's Google session cookies to the local Gemini MCP server
// through native messaging, so the server always uses the browser's current,
// signed-in session. Cookies go only to the native host registered on this
// computer (cookie_bridge/native_host.py); nothing is sent over the network.
// No cookie value is ever logged.

const api = globalThis.browser ?? globalThis.chrome;
const HOST = "com.gemini_webapi_mcp.cookie_bridge";
const DOMAINS = new Set([".google.com", "gemini.google.com", ".gemini.google.com"]);
const ALARM = "gemini-cookie-bridge-resync";
const IS_FIREFOX = typeof globalThis.browser !== "undefined"
  && typeof globalThis.browser.runtime.getBrowserInfo === "function";
const SOURCE = IS_FIREFOX ? "firefox" : (navigator.userAgent.includes("Edg/") ? "edge" : "chrome");

let timer = null;

async function getGoogleCookies() {
  try {
    return await api.cookies.getAll({ domain: "google.com" });
  } catch (err) {
    // Firefox with first-party isolation requires firstPartyDomain.
    if (IS_FIREFOX) return await api.cookies.getAll({ domain: "google.com", firstPartyDomain: null });
    throw err;
  }
}

async function push(reason) {
  try {
    const cookies = (await getGoogleCookies())
      .filter((c) => DOMAINS.has(c.domain) && c.value)
      .map((c) => ({
        domain: c.domain,
        name: c.name,
        value: c.value,
        path: c.path || "/",
        ...(c.expirationDate ? { expirationDate: c.expirationDate } : {}),
      }));
    if (!cookies.some((c) => c.name === "__Secure-1PSID" && c.domain === ".google.com")) return;
    const reply = await api.runtime.sendNativeMessage(HOST, { type: "cookies", source: SOURCE, reason, cookies });
    if (!reply || !reply.ok) console.warn("Gemini cookie bridge:", reply ? reply.error : "no reply");
  } catch (err) {
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

// Safety resync every 15 minutes; created once so worker restarts don't reset it.
Promise.resolve(api.alarms.get(ALARM)).then((existing) => {
  if (!existing) api.alarms.create(ALARM, { periodInMinutes: 15 });
});
api.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) push("periodic");
});
