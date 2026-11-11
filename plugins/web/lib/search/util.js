// The search code's shared helpers: settings, cache, private state files, text.
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const PROVIDER_ID = "search-plus";
// Every engine searchOrder may name. exa and parallel-paid need a key and are skipped without one.
export const ENGINES = ["exa", "parallel-paid", "you", "parallel", "firecrawl", "duckduckgo"];

// Since 2026-10-19 (a quality shoot-out of every engine that could be tried for free): one order for
// every engine, Exa (auto) first, Parallel paid (fast) second, You.com keyless third, Parallel's free
// MCP last. Firecrawl keyless and DuckDuckGo left the default order (both failed in the shoot-out) but
// stay selectable. Keyed engines are never refused for budget (alert lines only); paid Parallel is
// paced under its own limit in a window every room's process shares (provider.js).
export const PAID_MODES = ["turbo", "fast", "basic", "advanced"];
export const DEFAULTS = Object.freeze({
  searchOrder: ["exa", "parallel-paid", "you", "parallel"],
  engineTimeoutMs: 8000,
  videoLeg: true,
  transcriptsPerCall: 2,
  transcriptWallMs: 6000,
  videoSearchTimeoutMs: 6000,
  videoLegGraceMs: 2500,
  paidAlertThresholds: [20000, 50000],
  paidRatePerMinute: 500,
  paidMode: "fast",
  exaAlertThresholds: [5000, 10000],
  youDailyCap: 100,
  stateDir: "~/.local/state/yerk/web/search", // tools.mjs always passes <state>/web/search
  fallbackSnippetChars: 700,
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

// A key as tools.mjs hands it over, read from the private folder's secrets (secrets/parallel.key,
// secrets/exa.key). keySource names where it came from for the call log; the key itself is never logged.
export function resolveApiKey(rawValue) {
  const key = typeof rawValue === "string" ? rawValue.trim() : "";
  return { key, keySource: key ? "secrets" : "none" };
}

function thresholdList(value, fallback = DEFAULTS.paidAlertThresholds) {
  const list = Array.isArray(value) ? value : fallback;
  const nums = list.map((n) => (typeof n === "number" && Number.isFinite(n) ? Math.floor(n) : NaN)).filter((n) => n > 0);
  return [...new Set(nums)].sort((a, b) => a - b);
}

// Settings are the "search" part of the private web.json (tools.mjs), read on every call, so an
// edit applies to the next call.
export function resolvePluginSettings(config, overrides = {}) {
  const raw = config && typeof config === "object" && !Array.isArray(config) ? config : {};
  const merged = { ...raw, ...overrides };
  const order = Array.isArray(merged.searchOrder)
    ? merged.searchOrder.filter((e) => ENGINES.includes(e))
    : [];
  const { key, keySource } = resolveApiKey(merged.parallelApiKey);
  const exa = resolveApiKey(merged.exaApiKey);
  return {
    searchOrder: order.length ? [...new Set(order)] : [...DEFAULTS.searchOrder],
    engineTimeoutMs: intIn(merged.engineTimeoutMs, 1000, 30000, DEFAULTS.engineTimeoutMs),
    videoLeg: typeof merged.videoLeg === "boolean" ? merged.videoLeg : DEFAULTS.videoLeg,
    transcriptsPerCall: intIn(merged.transcriptsPerCall, 0, 2, DEFAULTS.transcriptsPerCall),
    transcriptWallMs: intIn(merged.transcriptWallMs, 1000, 15000, DEFAULTS.transcriptWallMs),
    videoSearchTimeoutMs: intIn(merged.videoSearchTimeoutMs, 1000, 15000, DEFAULTS.videoSearchTimeoutMs),
    videoLegGraceMs: intIn(merged.videoLegGraceMs, 0, 15000, DEFAULTS.videoLegGraceMs),
    // paidMonthlyCap is accepted for old configs and ignored: paid calls are never refused for budget.
    paidAlertThresholds: thresholdList(merged.paidAlertThresholds),
    paidRatePerMinute: intIn(merged.paidRatePerMinute, 1, 100000, DEFAULTS.paidRatePerMinute),
    paidMode: PAID_MODES.includes(merged.paidMode) ? merged.paidMode : DEFAULTS.paidMode,
    parallelApiKey: key,
    parallelKeySource: keySource,
    exaApiKey: exa.key,
    exaKeySource: exa.keySource,
    exaAlertThresholds: thresholdList(merged.exaAlertThresholds, DEFAULTS.exaAlertThresholds),
    youDailyCap: intIn(merged.youDailyCap, 1, 100000, DEFAULTS.youDailyCap),
    ytTranscriptPath: typeof merged.ytTranscriptPath === "string" ? expandHome(merged.ytTranscriptPath.trim()) : "",
    ytTranscriptBin: typeof merged.ytTranscriptBin === "string" ? merged.ytTranscriptBin : "",
    stateDir: expandHome(typeof merged.stateDir === "string" && merged.stateDir.trim() ? merged.stateDir.trim() : DEFAULTS.stateDir),
    fallbackSnippetChars: intIn(merged.fallbackSnippetChars, 200, 3000, DEFAULTS.fallbackSnippetChars),
    noLog: merged.noLog === true,
    noCache: merged.noCache === true,
  };
}

// ---- in-memory result cache, this process's own ----
const CACHE = new Map();
const CACHE_MAX_ENTRIES = 500;

export function cacheTtlMs(searchConfig) {
  const minutes = searchConfig?.cacheTtlMinutes;
  if (typeof minutes === "number" && Number.isFinite(minutes) && minutes >= 0) return Math.floor(minutes * 60000);
  return 15 * 60000;
}

export function normalizeQuery(q) {
  return String(q ?? "").toLowerCase().replace(/\s+/g, " ").trim();
}

export function cacheKeyFor(parts) {
  return createHash("sha256").update(JSON.stringify(parts)).digest("hex");
}

export function cacheGet(key, ttlMs) {
  if (!(ttlMs > 0)) return undefined;
  const hit = CACHE.get(key);
  if (!hit) return undefined;
  if (Date.now() - hit.at > ttlMs) {
    CACHE.delete(key);
    return undefined;
  }
  return hit.value;
}

export function cacheSet(key, value, ttlMs) {
  if (!(ttlMs > 0)) return;
  if (CACHE.size >= CACHE_MAX_ENTRIES) {
    const oldest = CACHE.keys().next().value;
    CACHE.delete(oldest);
  }
  CACHE.set(key, { at: Date.now(), value });
}

// ---- private state files: dir 0700, files 0600 ----
export function ensureStateDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}

export function queryHash(q) {
  return createHash("sha256").update(normalizeQuery(q)).digest("hex").slice(0, 16);
}

export function appendCallLog(settings, entry) {
  if (settings.noLog) return;
  try {
    ensureStateDir(settings.stateDir);
    const file = path.join(settings.stateDir, "calls.jsonl");
    const fd = fs.openSync(file, "a", 0o600);
    try {
      fs.writeSync(fd, JSON.stringify(entry) + "\n");
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    // Logging is telemetry; it never breaks a search.
  }
}

function monthStamp(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

// Alert lines go to <stateDir>/alerts.log, 0600.
export function appendAlert(settings, entry) {
  try {
    ensureStateDir(settings.stateDir);
    const fd = fs.openSync(path.join(settings.stateDir, "alerts.log"), "a", 0o600);
    try {
      fs.writeSync(fd, JSON.stringify(entry) + "\n");
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    // An alert that cannot be written never blocks a search.
  }
}

function writeCounter(settings, file, text) {
  try {
    ensureStateDir(settings.stateDir);
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, text + "\n", { mode: 0o600 });
    fs.renameSync(tmp, file);
  } catch {
    // A counter that cannot be written never blocks a search.
  }
}

// Counts BEFORE the keyed request goes out, so a crash mid-call over-counts rather than
// under-counts. Never refuses (the owner's order: search is paid and unlimited):
// when the monthly count reaches a threshold one alert line ({ts, engine, count, month}) is
// appended and the call goes out anyway. Returns the new monthly count. One file per engine and
// month: paid-YYYY-MM (Parallel paid, default alerts 20,000 and 50,000) and exa-YYYY-MM (Exa,
// default alerts 5,000 and 10,000).
function countMonthly(settings, prefix, engine, thresholds, now) {
  const month = monthStamp(now);
  const file = path.join(settings.stateDir, `${prefix}-${month}`);
  let current = 0;
  try {
    const n = parseInt(fs.readFileSync(file, "utf8").trim(), 10);
    current = Number.isFinite(n) && n >= 0 ? n : 0;
  } catch {}
  const next = current + 1;
  writeCounter(settings, file, String(next));
  for (const t of thresholds ?? []) {
    if (current < t && next >= t) appendAlert(settings, { ts: now.toISOString(), engine, count: next, month });
  }
  return next;
}

export function countPaidCall(settings, now = new Date()) {
  return countMonthly(settings, "paid", "parallel-paid", settings.paidAlertThresholds, now);
}

export function countExaCall(settings, now = new Date()) {
  return countMonthly(settings, "exa", "exa", settings.exaAlertThresholds, now);
}

// You.com's keyless MCP allows 100 searches a day (you.com/docs/agents/mcp-server, read 2026-10-19).
// <stateDir>/you-daily holds "YYYY-MM-DD N" for the local calendar day. A slot is booked BEFORE the
// request (a crash over-counts, like the monthly counters); once the day's count reaches
// youDailyCap the engine is skipped without a request until the local date changes.
function dayStamp(d = new Date()) {
  return `${monthStamp(d)}-${String(d.getDate()).padStart(2, "0")}`;
}

export function readYouDay(settings, now = new Date()) {
  const day = dayStamp(now);
  try {
    const [d, n] = fs.readFileSync(path.join(settings.stateDir, "you-daily"), "utf8").trim().split(/\s+/);
    const count = parseInt(n, 10);
    return { day, count: d === day && Number.isFinite(count) && count >= 0 ? count : 0 };
  } catch {
    return { day, count: 0 };
  }
}

// Returns { ok: true, count } with the slot booked, or { ok: false, count } once the day is spent.
export function takeYouSlot(settings, now = new Date()) {
  const { day, count } = readYouDay(settings, now);
  if (count >= settings.youDailyCap) return { ok: false, count };
  writeCounter(settings, path.join(settings.stateDir, "you-daily"), `${day} ${count + 1}`);
  return { ok: true, count: count + 1 };
}

// ---- text helpers ----
export function clip(text, max) {
  const s = String(text ?? "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : s.slice(0, Math.max(0, max - 3)).trimEnd() + "...";
}

export function toHttpUrl(value) {
  if (typeof value !== "string" || value.length > 2048) return undefined;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}

export function siteNameOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return undefined;
  }
}

const PUBLISHED_RE = /^\d{4}-\d{2}-\d{2}(?:[T ][\d:.+Z-]{0,20})?$/u;
export function normalizePublished(value) {
  if (typeof value !== "string") return undefined;
  const v = value.trim();
  if (/^\d{8}$/.test(v)) return `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)}`;
  return PUBLISHED_RE.test(v) ? v : undefined;
}

export function withTimeout(signal, ms) {
  const t = AbortSignal.timeout(ms);
  return signal ? AbortSignal.any([signal, t]) : t;
}
