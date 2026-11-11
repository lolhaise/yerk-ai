// The fetch ladder's shared helpers: settings, private state files, monthly usage
// counters, budget alerts and the per-call log.
//
// Privacy rules for everything written here: hostnames only, never a URL, never
// page text, never a key. State dir is 0700, files are 0600.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const PROVIDER_ID = "fetch-plus";
// The machine's own time zone and locale: the local browser presents them, matching the connection it reads from.
const MACHINE = Intl.DateTimeFormat().resolvedOptions();

export const DEFAULTS = Object.freeze({
  apiBaseUrl: "https://api.browserbase.com",
  fetchFormat: "raw", // raw = normal Fetch (cheapest, no project enablement); markdown = Fetch Extract
  l1TimeoutMs: 20000,
  l2TimeoutMs: 25000,
  l2Proxies: true,
  skipL2ForJsShell: false,
  l3Enabled: true,
  l3Proxies: true,
  l3MaxSeconds: 60,
  l3SolveCaptchas: true,
  l3Verified: false, // Verified browser mode is a Scale-plan feature
  l3BlockAds: true,
  l3RecordSession: false,
  l3LogSession: false,
  l3Region: "",
  l3NetworkIdleMs: 15000,
  l3CaptchaWaitMs: 30000,
  l3Driver: "auto", // auto | playwright | cdp
  contextStrategy: "site", // site | host | none
  contextGroups: {},
  contextPersist: true,
  contextWaitMs: 8000,
  contextSettleMs: 3000,
  minVisibleChars: 500,
  minVisibleCharsHard: 200,
  l3FirstHosts: [],
  totalMaxSeconds: 140, // F1 + F2 + L3 + one pictures-on L3 retry (was 110 before the free rungs)
  blockPrivateHosts: true,
  allowInsecureSsl: false,
  alertFetchCalls: 5000,
  alertProxyBytes: 1000000000, // the Developer plan's included 1 GB; past it every GB is $12
  alertBrowserHours: 150,
  stateDir: "~/.local/state/yerk/web/fetch", // tools.mjs always passes <state>/web/fetch
  playwrightCorePath: "",
  // Free rungs from this machine: F1 local fetch, F2 local browser, F3 trawl, then Browserbase.
  localEnabled: true,
  localLocale: "", // the local rungs' language and the F2 browser's locale; empty: the machine's own
  localTimezone: "", // the F2 browser's time zone; empty: the machine's own
  localFetchTimeoutMs: 12000,
  localMaxBytes: 5 * 1024 * 1024,
  localBrowserEnabled: true,
  localBrowserEngine: "cloak", // cloak (CloakBrowser stealth Chromium) | chromium (plain Chromium)
  localCloakModulePath: "~/.local/share/fetch-plus-local/cloak/node_modules/cloakbrowser/dist/index.js",
  localCloakCacheDir: "~/.local/share/fetch-plus-local/cloakbrowser-bin",
  localChromiumPath: "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
  localBrowserMaxSeconds: 35,
  localMaxPages: 2,
  localIdleCloseMs: 120000,
  localQueueWaitMs: 15000,
  localNetworkIdleMs: 6000, // the ceiling of F2's readable-stop wait (2026-10-21; measured: op.gg's content lands up to 5.1 s after domcontentloaded)
  localChallengeWaitMs: 15000,
  localReadableMinChars: 3500, // F2 readable-stop (2026-10-21): visible chars before the wait may end early; op.gg's loading skeleton is 2,716 to 3,351, the smallest ready page measured (lotterypost) 4,026
  localReadablePollMs: 500, // how often F2 probes the page after domcontentloaded
  localReadableWaitMs: 10000, // a page still under localReadableMinChars with main-frame requests in flight may keep loading this long after domcontentloaded (op.gg's data landed 3.5 to over 6 s after it)
  hopelessMaxSeconds: 45, // the whole call's cap once two free rungs met the same challenge kind (2026-10-21): L1 and L2 skipped, the solver rung gets what is left
  localBlockPictures: true,
  localCooldownHours: 24,
  localMaxPerHostPerMinute: 6,
  localMaxPerMinute: 30,
  localTrawlEnabled: true, // F3; answers only when the trawl service runs (systemd unit in the staging dir)
  localTrawlUrl: "http://127.0.0.1:8191",
  localTrawlMaxSeconds: 45,
  bbFetchAfterLocal: true, // after F1+F2 met a wall, L1 and L2 before L3: October's L1 read Etsy and LinkedIn pages the free rungs missed, inside the included Fetch calls
  l3BlockPictures: true, // abort image, media and font requests in the Browserbase browser; retried with pictures on for a captcha
  usageSyncMinutes: 10, // refresh Browserbase's own project usage this often after a paid rung ran
  // Phase 2 (2026-10-19): site memory in local-cooldown.json, and Browserbase's browser without proxies first.
  siteMemory: true, // per host: the free step and the Browserbase step that read it last time; the next read starts there
  siteMemoryRetryHours: 24, // this often a remembered host starts one step cheaper, because sites change
  siteMemoryDays: 90, // a host not read for this long is forgotten
  l3NoProxyFirst: true, // L3n: Browserbase's browser without proxies before L3 with proxies (M3: 4 of 10 October L3 hosts read without)
  l3NoProxyMaxSeconds: 35, // cap for the L3n pass (M3 reads took 7.5 to 29 s)
  l3NoProxyReserveMs: 45000, // time kept for the proxied L3 pass; L3n is skipped when less than this plus 15 s is left
});

const HOME = os.homedir();

export function expandHome(p) {
  if (typeof p !== "string" || !p) return p;
  return p === "~" ? HOME : p.startsWith("~/") ? path.join(HOME, p.slice(2)) : p;
}

function intIn(value, min, max, fallback) {
  const n = typeof value === "number" && Number.isFinite(value) ? Math.floor(value) : NaN;
  return Number.isNaN(n) ? fallback : Math.min(max, Math.max(min, n));
}

function bool(value, fallback) {
  return typeof value === "boolean" ? value : fallback;
}

function oneOf(value, allowed, fallback) {
  return typeof value === "string" && allowed.includes(value) ? value : fallback;
}

export function pluginConfigOf(config) {
  return config && typeof config === "object" && !Array.isArray(config) ? config : {};
}

// Settings are read on every call from the "fetch" part of the private web.json
// (tools.mjs), so an edit applies to the next call without a restart.
export function resolveSettings(config, overrides = {}) {
  const m = { ...pluginConfigOf(config), ...overrides };
  const groups = {};
  if (m.contextGroups && typeof m.contextGroups === "object" && !Array.isArray(m.contextGroups)) {
    for (const [name, list] of Object.entries(m.contextGroups)) {
      if (Array.isArray(list)) groups[name] = list.filter((d) => typeof d === "string" && d.trim()).map((d) => d.trim().toLowerCase());
    }
  }
  const base = typeof m.apiBaseUrl === "string" && /^https?:\/\//.test(m.apiBaseUrl.trim()) ? m.apiBaseUrl.trim().replace(/\/+$/, "") : DEFAULTS.apiBaseUrl;
  return {
    apiBaseUrl: base,
    fetchFormat: oneOf(m.fetchFormat, ["raw", "markdown"], DEFAULTS.fetchFormat),
    l1TimeoutMs: intIn(m.l1TimeoutMs, 2000, 60000, DEFAULTS.l1TimeoutMs),
    l2TimeoutMs: intIn(m.l2TimeoutMs, 2000, 60000, DEFAULTS.l2TimeoutMs),
    l2Proxies: bool(m.l2Proxies, DEFAULTS.l2Proxies),
    skipL2ForJsShell: bool(m.skipL2ForJsShell, DEFAULTS.skipL2ForJsShell),
    l3Enabled: bool(m.l3Enabled, DEFAULTS.l3Enabled),
    l3Proxies: bool(m.l3Proxies, DEFAULTS.l3Proxies),
    l3MaxSeconds: intIn(m.l3MaxSeconds, 10, 300, DEFAULTS.l3MaxSeconds),
    l3SolveCaptchas: bool(m.l3SolveCaptchas, DEFAULTS.l3SolveCaptchas),
    l3Verified: bool(m.l3Verified, DEFAULTS.l3Verified),
    l3BlockAds: bool(m.l3BlockAds, DEFAULTS.l3BlockAds),
    l3RecordSession: bool(m.l3RecordSession, DEFAULTS.l3RecordSession),
    l3LogSession: bool(m.l3LogSession, DEFAULTS.l3LogSession),
    l3Region: oneOf(m.l3Region, ["us-west-2", "us-east-1", "eu-central-1", "ap-southeast-1"], ""),
    l3NetworkIdleMs: intIn(m.l3NetworkIdleMs, 1000, 60000, DEFAULTS.l3NetworkIdleMs),
    l3CaptchaWaitMs: intIn(m.l3CaptchaWaitMs, 0, 120000, DEFAULTS.l3CaptchaWaitMs),
    l3Driver: oneOf(m.l3Driver, ["auto", "playwright", "cdp"], DEFAULTS.l3Driver),
    contextStrategy: oneOf(m.contextStrategy, ["site", "host", "none"], DEFAULTS.contextStrategy),
    contextGroups: groups,
    l3FirstHosts: Array.isArray(m.l3FirstHosts) ? m.l3FirstHosts.filter((h) => typeof h === "string" && h.trim()).map((h) => h.trim().toLowerCase().replace(/^\*?\./, "")) : DEFAULTS.l3FirstHosts,
    contextPersist: bool(m.contextPersist, DEFAULTS.contextPersist),
    contextWaitMs: intIn(m.contextWaitMs, 0, 60000, DEFAULTS.contextWaitMs),
    contextSettleMs: intIn(m.contextSettleMs, 0, 30000, DEFAULTS.contextSettleMs),
    minVisibleChars: intIn(m.minVisibleChars, 0, 20000, DEFAULTS.minVisibleChars),
    minVisibleCharsHard: intIn(m.minVisibleCharsHard, 0, 5000, DEFAULTS.minVisibleCharsHard),
    totalMaxSeconds: intIn(m.totalMaxSeconds, 20, 600, DEFAULTS.totalMaxSeconds),
    blockPrivateHosts: bool(m.blockPrivateHosts, DEFAULTS.blockPrivateHosts),
    allowInsecureSsl: bool(m.allowInsecureSsl, DEFAULTS.allowInsecureSsl),
    alertFetchCalls: intIn(m.alertFetchCalls, 1, 1e9, DEFAULTS.alertFetchCalls),
    alertProxyBytes: intIn(m.alertProxyBytes, 1, Number.MAX_SAFE_INTEGER, DEFAULTS.alertProxyBytes),
    alertBrowserHours: intIn(m.alertBrowserHours, 1, 1e6, DEFAULTS.alertBrowserHours),
    stateDir: expandHome(typeof m.stateDir === "string" && m.stateDir.trim() ? m.stateDir.trim() : DEFAULTS.stateDir),
    playwrightCorePath: typeof m.playwrightCorePath === "string" ? expandHome(m.playwrightCorePath.trim()) : "",
    localEnabled: bool(m.localEnabled, DEFAULTS.localEnabled),
    localLocale: typeof m.localLocale === "string" && /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$/.test(m.localLocale.trim()) ? m.localLocale.trim() : MACHINE.locale || "en-US",
    localTimezone: typeof m.localTimezone === "string" && m.localTimezone.trim() ? m.localTimezone.trim() : MACHINE.timeZone || "UTC",
    localFetchTimeoutMs: intIn(m.localFetchTimeoutMs, 2000, 60000, DEFAULTS.localFetchTimeoutMs),
    localMaxBytes: intIn(m.localMaxBytes, 65536, 50 * 1024 * 1024, DEFAULTS.localMaxBytes),
    localBrowserEnabled: bool(m.localBrowserEnabled, DEFAULTS.localBrowserEnabled),
    localBrowserEngine: oneOf(m.localBrowserEngine, ["cloak", "chromium"], DEFAULTS.localBrowserEngine),
    localCloakModulePath: expandHome(typeof m.localCloakModulePath === "string" && m.localCloakModulePath.trim() ? m.localCloakModulePath.trim() : DEFAULTS.localCloakModulePath),
    localCloakCacheDir: expandHome(typeof m.localCloakCacheDir === "string" && m.localCloakCacheDir.trim() ? m.localCloakCacheDir.trim() : DEFAULTS.localCloakCacheDir),
    localChromiumPath: expandHome(typeof m.localChromiumPath === "string" && m.localChromiumPath.trim() ? m.localChromiumPath.trim() : DEFAULTS.localChromiumPath),
    localBrowserMaxSeconds: intIn(m.localBrowserMaxSeconds, 5, 120, DEFAULTS.localBrowserMaxSeconds),
    localMaxPages: intIn(m.localMaxPages, 1, 4, DEFAULTS.localMaxPages),
    localIdleCloseMs: intIn(m.localIdleCloseMs, 1000, 3600000, DEFAULTS.localIdleCloseMs),
    localQueueWaitMs: intIn(m.localQueueWaitMs, 0, 120000, DEFAULTS.localQueueWaitMs),
    localNetworkIdleMs: intIn(m.localNetworkIdleMs, 500, 60000, DEFAULTS.localNetworkIdleMs),
    localChallengeWaitMs: intIn(m.localChallengeWaitMs, 0, 120000, DEFAULTS.localChallengeWaitMs),
    localReadableMinChars: intIn(m.localReadableMinChars, 0, 100000, DEFAULTS.localReadableMinChars),
    localReadablePollMs: intIn(m.localReadablePollMs, 100, 5000, DEFAULTS.localReadablePollMs),
    localReadableWaitMs: intIn(m.localReadableWaitMs, 500, 60000, DEFAULTS.localReadableWaitMs),
    hopelessMaxSeconds: intIn(m.hopelessMaxSeconds, 10, 600, DEFAULTS.hopelessMaxSeconds),
    localBlockPictures: bool(m.localBlockPictures, DEFAULTS.localBlockPictures),
    localCooldownHours: intIn(m.localCooldownHours, 0, 720, DEFAULTS.localCooldownHours),
    localMaxPerHostPerMinute: intIn(m.localMaxPerHostPerMinute, 0, 600, DEFAULTS.localMaxPerHostPerMinute),
    localMaxPerMinute: intIn(m.localMaxPerMinute, 0, 6000, DEFAULTS.localMaxPerMinute),
    localTrawlEnabled: bool(m.localTrawlEnabled, DEFAULTS.localTrawlEnabled),
    localTrawlUrl: typeof m.localTrawlUrl === "string" && /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(m.localTrawlUrl.trim()) ? m.localTrawlUrl.trim() : DEFAULTS.localTrawlUrl,
    localTrawlMaxSeconds: intIn(m.localTrawlMaxSeconds, 5, 120, DEFAULTS.localTrawlMaxSeconds),
    bbFetchAfterLocal: bool(m.bbFetchAfterLocal, DEFAULTS.bbFetchAfterLocal),
    l3BlockPictures: bool(m.l3BlockPictures, DEFAULTS.l3BlockPictures),
    usageSyncMinutes: intIn(m.usageSyncMinutes, 0, 1440, DEFAULTS.usageSyncMinutes),
    siteMemory: bool(m.siteMemory, DEFAULTS.siteMemory),
    siteMemoryRetryHours: intIn(m.siteMemoryRetryHours, 1, 720, DEFAULTS.siteMemoryRetryHours),
    siteMemoryDays: intIn(m.siteMemoryDays, 1, 3650, DEFAULTS.siteMemoryDays),
    l3NoProxyFirst: bool(m.l3NoProxyFirst, DEFAULTS.l3NoProxyFirst),
    l3NoProxyMaxSeconds: intIn(m.l3NoProxyMaxSeconds, 5, 120, DEFAULTS.l3NoProxyMaxSeconds),
    l3NoProxyReserveMs: intIn(m.l3NoProxyReserveMs, 0, 120000, DEFAULTS.l3NoProxyReserveMs),
    noLog: m.noLog === true,
    // Test seam only: lets the harness point the rungs at a mock without env games.
    _now: typeof overrides._now === "function" ? overrides._now : undefined,
    _settleDelaysMs: Array.isArray(m._settleDelaysMs) ? m._settleDelaysMs : undefined,
  };
}

// ---- private state files ---------------------------------------------------
export function ensureStateDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}

export function writeJsonAtomic(file, value) {
  ensureStateDir(path.dirname(file));
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 1) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, file);
}

export function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function appendLine(file, line) {
  ensureStateDir(path.dirname(file));
  const fd = fs.openSync(file, "a", 0o600);
  try {
    fs.writeSync(fd, line.endsWith("\n") ? line : line + "\n");
  } finally {
    fs.closeSync(fd);
  }
}

export function monthStamp(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function isoLocal(d = new Date()) {
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${sign}${pad(off / 60)}:${pad(off % 60)}`;
}

// One line per ladder run. Hostname only; no URL, no page text, no key.
export function appendCallLog(settings, entry) {
  if (settings.noLog) return;
  try {
    appendLine(path.join(settings.stateDir, "calls.jsonl"), JSON.stringify({ ts: isoLocal(), ...entry }));
  } catch {
    // Telemetry never breaks a fetch.
  }
}

// ---- monthly usage + budget alerts -----------------------------------------
// Updates are serialized in-process so parallel fetches do not lose counts.
let usageChain = Promise.resolve();
const pending = new Set();

export function usageFile(settings, month = monthStamp()) {
  return path.join(settings.stateDir, `usage-${month}.json`);
}

function emptyUsage(month) {
  return {
    month,
    fetchCalls: 0,
    fetchProxiedCalls: 0,
    proxyBytesEst: 0,
    proxyBytesMeasured: 0,
    proxySessionsSettled: 0,
    browserSessions: 0,
    browserSeconds: 0,
    localFetchCalls: 0,
    localBytes: 0,
    localBrowserPages: 0,
    localBrowserSeconds: 0,
    localTrawlCalls: 0,
    localTrawlSeconds: 0,
    bbProjectProxyBytes: null,
    bbProjectBrowserMinutes: null,
    bbProjectSyncedAt: null,
    alerts: { fetchCalls: false, proxyBytes: false, browserHours: false },
    updatedAt: null,
  };
}

// delta: { fetchCalls, fetchProxiedCalls, proxyBytesEst, proxyBytesMeasured, browserSessions, browserSeconds, local* }
// set: absolute fields, Browserbase's own project meter { bbProjectProxyBytes, bbProjectBrowserMinutes, bbProjectSyncedAt }
export function recordUsage(settings, delta, set = {}) {
  const job = usageChain.then(() => {
    try {
      const month = monthStamp();
      const file = usageFile(settings, month);
      const u = { ...emptyUsage(month), ...readJson(file, {}) };
      u.alerts = { ...emptyUsage(month).alerts, ...(u.alerts || {}) };
      for (const [k, v] of Object.entries(delta)) {
        if (typeof v === "number" && Number.isFinite(v)) u[k] = Math.round(((u[k] || 0) + v) * 1000) / 1000;
      }
      for (const [k, v] of Object.entries(set)) u[k] = v;
      u.updatedAt = isoLocal();
      const alerts = [];
      // True numbers first (2026-10-19): Browserbase's own meter when synced, else the settled
      // per-session bytes; the estimate only when neither exists (it read 2.09 GB against a real 3.352).
      const proxyTotal = Number.isFinite(u.bbProjectProxyBytes) ? Math.max(u.bbProjectProxyBytes, u.proxyBytesMeasured) : u.proxyBytesMeasured > 0 ? u.proxyBytesMeasured : u.proxyBytesEst;
      const browserHours = Math.max(u.browserSeconds / 3600, Number.isFinite(u.bbProjectBrowserMinutes) ? u.bbProjectBrowserMinutes / 60 : 0);
      if (!u.alerts.fetchCalls && u.fetchCalls > settings.alertFetchCalls) {
        u.alerts.fetchCalls = true;
        alerts.push(`fetchCalls passed ${settings.alertFetchCalls} (now ${u.fetchCalls}, of which proxied ${u.fetchProxiedCalls})`);
      }
      if (!u.alerts.proxyBytes && proxyTotal > settings.alertProxyBytes) {
        u.alerts.proxyBytes = true;
        alerts.push(`proxy traffic passed ${(settings.alertProxyBytes / 1e9).toFixed(2)} GB (now ${(proxyTotal / 1e9).toFixed(3)} GB${Number.isFinite(u.bbProjectProxyBytes) ? ", Browserbase's own meter" : ""})`);
      }
      if (!u.alerts.browserHours && browserHours > settings.alertBrowserHours) {
        u.alerts.browserHours = true;
        alerts.push(`browser time passed ${settings.alertBrowserHours} h (now ${browserHours.toFixed(2)} h over ${u.browserSessions} sessions)`);
      }
      writeJsonAtomic(file, u);
      for (const a of alerts) appendLine(path.join(settings.stateDir, "alerts.log"), `${isoLocal()} fetch-plus month=${month} ${a}. Nothing was refused; this is a budget heads-up only.`);
    } catch {
      // Usage accounting never breaks a fetch.
    }
  });
  usageChain = job;
  pending.add(job);
  job.finally(() => pending.delete(job));
  return job;
}

// Browserbase's own project meter (GET /v1/projects/{id}/usage: browserMinutes, proxyBytes), the
// figure the bill is made of. Read after paid rungs, at most every usageSyncMinutes; never throws.
let lastProjectSync = 0;

export function syncProjectUsage(settings, creds, fetchImpl = fetch, { force = false } = {}) {
  if (!(settings.usageSyncMinutes > 0) || !creds?.apiKey || !creds?.projectId) return null;
  const now = Date.now();
  if (!force && now - lastProjectSync < settings.usageSyncMinutes * 60000) return null;
  lastProjectSync = now;
  return trackPending(
    (async () => {
      const res = await fetchImpl(`${settings.apiBaseUrl}/v1/projects/${encodeURIComponent(creds.projectId)}/usage`, { headers: { "X-BB-API-Key": creds.apiKey, Accept: "application/json" }, signal: AbortSignal.timeout(15000) });
      if (!res.ok) return;
      const j = await res.json();
      if (!Number.isFinite(j?.proxyBytes) && !Number.isFinite(j?.browserMinutes)) return;
      await recordUsage(settings, {}, { bbProjectProxyBytes: Number.isFinite(j.proxyBytes) ? j.proxyBytes : null, bbProjectBrowserMinutes: Number.isFinite(j.browserMinutes) ? j.browserMinutes : null, bbProjectSyncedAt: isoLocal() });
    })().catch(() => {}),
  );
}

// Test seam: wait for queued usage writes and fire-and-forget session reads.
export async function flushPending() {
  while (pending.size) await Promise.allSettled([...pending]);
}

export function trackPending(promise) {
  pending.add(promise);
  promise.finally(() => pending.delete(promise)).catch(() => {});
  return promise;
}

// ---- misc ------------------------------------------------------------------
export function withTimeout(signal, ms) {
  const t = AbortSignal.timeout(Math.max(1, ms));
  return signal ? AbortSignal.any([signal, t]) : t;
}

export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new Error("aborted"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener?.("abort", () => {
      clearTimeout(t);
      reject(signal.reason ?? new Error("aborted"));
    }, { once: true });
  });
}

export function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }
}

const PRIVATE_HOST_RE = /^(localhost|localhost\.localdomain|ip6-localhost|ip6-loopback)$|\.local$|\.internal$|\.lan$|\.home\.arpa$/i;

export function isPrivateHost(hostname) {
  const h = String(hostname || "").replace(/^\[|\]$/g, "").toLowerCase();
  if (!h) return true;
  if (PRIVATE_HOST_RE.test(h)) return true;
  const m = h.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (m) {
    const [a, b] = [Number(m[1]), Number(m[2])];
    return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  // An IPv4-mapped address (::ffff:127.0.0.1, which a URL writes as ::ffff:7f00:1) connects to its IPv4 one: judged as that.
  const v4 = /^(?:::|(?:0{1,4}:){5})ffff:(?:(\d{1,3}(?:\.\d{1,3}){3})|([0-9a-f]{1,4}):([0-9a-f]{1,4}))$/.exec(h);
  if (v4) return isPrivateHost(v4[1] ?? [v4[2], v4[3]].flatMap((g) => [parseInt(g, 16) >> 8, parseInt(g, 16) & 255]).join("."));
  if (h.includes(":")) return h === "::1" || h.startsWith("fc") || h.startsWith("fd") || h.startsWith("fe80") || h === "::";
  return false;
}
