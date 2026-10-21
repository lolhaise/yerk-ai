// Free rungs, run from this machine itself (2026-10-19). On a home connection a read from here is
// already a residential visitor and needs no paid proxy:
//   F1  a plain local fetch with browser headers, judged by detect.js and extract.js
//   F2  a local headless browser (CloakBrowser's stealth Chromium by default, plain Chromium as
//       the fallback engine), one process, launched on demand, at most localMaxPages pages at
//       once, pictures off, closed after localIdleCloseMs with nothing open
//   F3  trawl's browser tier (Camoufox, captchas solved on this machine, no paid solver), only after F2
//       met a challenge, and only when the trawl service answers on localTrawlUrl (127.0.0.1)
// Home guards: a host that blocked a local rung skips both for localCooldownHours (persisted in
// the state dir), and a per-host and overall rate cap keeps a burst of automated reads from
// getting the home connection flagged. Hosts in l3FirstHosts never reach these rungs.
// Site memory (phase 2): the same file remembers per host where the last read succeeded; see below.
// An engine that is not on this machine (CloakBrowser, Chromium, trawl) is skipped, never tried.
import dns from "node:dns/promises";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { takeWindow } from "../common.mjs";
import { detectChallenge, judgePage } from "./detect.js";
import { collapse, extractHtml } from "./extract.js";
import { isPrivateHost, readJson, recordUsage, sleep, writeJsonAtomic } from "./util.js";

const CHROME_MAJOR = "146";
const BROWSER_HEADERS = {
  "user-agent": `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${CHROME_MAJOR}.0.0.0 Safari/537.36`,
  accept: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
  "cache-control": "no-cache",
  "upgrade-insecure-requests": "1",
  "sec-ch-ua": `"Chromium";v="${CHROME_MAJOR}", "Not)A;Brand";v="24", "Google Chrome";v="${CHROME_MAJOR}"`,
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"Windows"',
  "sec-fetch-dest": "document",
  "sec-fetch-mode": "navigate",
  "sec-fetch-site": "none",
  "sec-fetch-user": "?1",
};

// The headers with the machine's language first (settings.localLocale), then US English.
function browserHeaders(settings) {
  const lang = [settings.localLocale, settings.localLocale !== "en-US" && "en-US;q=0.9", "en;q=0.8"].filter(Boolean).join(",");
  return { ...BROWSER_HEADERS, "accept-language": lang };
}

// ---- home guards: cooldown and rate cap (both persisted, shared by every room's process) ----
let cooldownCache;

function cooldownFile(settings) {
  return path.join(settings.stateDir, "local-cooldown.json");
}

function loadCooldowns(settings) {
  if (!cooldownCache || cooldownCache.file !== cooldownFile(settings)) {
    const hosts = readJson(cooldownFile(settings), {}) || {};
    // A site-memory seed (scripts/seed-site-memory.mjs, written before the deploy) is merged once
    // at first load, then renamed: the older plugin rewrites this file and would drop it.
    const seedFile = path.join(settings.stateDir, "site-memory-seed.json");
    const seed = readJson(seedFile, null);
    if (seed && typeof seed === "object" && !Array.isArray(seed)) {
      for (const [h, rec] of Object.entries(seed)) if (rec && !hosts[h]?.start && !hosts[h]?.bb) hosts[h] = { ...(hosts[h] || {}), ...rec };
      try {
        writeJsonAtomic(cooldownFile(settings), hosts);
        fs.renameSync(seedFile, `${seedFile}.applied`);
      } catch {}
    }
    cooldownCache = { file: cooldownFile(settings), hosts };
  }
  return cooldownCache.hosts;
}

function nowOf(settings) {
  return typeof settings._now === "function" ? settings._now() : Date.now();
}

// The same file holds the site memory (below): an expired cooldown loses its guard fields only.
const GUARD_FIELDS = ["until", "skip", "why", "at"];

function dropGuard(hosts, host) {
  const rec = hosts[host];
  if (!rec) return;
  for (const k of GUARD_FIELDS) delete rec[k];
  if (!rec.start && !rec.bb) delete hosts[host];
}

export function localCooldownFor(settings, host) {
  const hosts = loadCooldowns(settings);
  const rec = hosts[host];
  if (!rec?.until) return null;
  if (rec.until > nowOf(settings)) return rec;
  dropGuard(hosts, host);
  return null;
}

// skip "all": the local browser was refused too, so both local rungs rest for localCooldownHours.
// skip "F1": only the plain fetch was refused (the browser read it), so the next read starts at F2.
// skip "F12": F1 and F2 were refused but trawl read it, so the next read starts at F3.
export function markLocalBlocked(settings, host, why, skip = "all") {
  if (!(settings.localCooldownHours > 0)) return;
  const hosts = loadCooldowns(settings);
  const now = nowOf(settings);
  for (const h of Object.keys(hosts)) if (hosts[h]?.until && !(hosts[h].until > now)) dropGuard(hosts, h);
  hosts[host] = { ...(hosts[host] || {}), until: now + settings.localCooldownHours * 3600000, skip: skip === "F1" || skip === "F12" ? skip : "all", why: String(why || "blocked").slice(0, 60), at: new Date(now).toISOString() };
  try {
    writeJsonAtomic(cooldownFile(settings), hosts);
  } catch {
    // A cooldown that fails to persist still holds in memory for this process.
  }
}

// ---- site memory (2026-10-19 phase 2), in the same file as the cooldowns --------
// Per host: `start`, the free step that read it last time (F2 or F3; F1 is the default, not
// stored), and `bb`, the Browserbase step that read it the last time a read got that far (L3n,
// the browser without proxies, or L3, with proxies; L1 is the default, not stored; an L2 read
// counts as L1). The next read starts at `start` and, if the free steps fail, enters Browserbase
// at `bb`. Once every siteMemoryRetryHours it starts one step cheaper on both, because sites change.
// Two rules keep it from costing money: memory never skips the free steps to start on a paid one
// (only the home guards above do that), and a read lands straight on proxied L3 only when proxies
// were needed there last time. On October's paid reads, starting at the remembered paid step cost
// more than it saved ($57.8 against $47.6, scripts/sim-memory-*.py in the phase 2 staging dir): a
// host that needed L3 for one page sent its cheaper pages to L3. Every read is still judged by the
// same blocked-page check; memory only picks where the climb starts.
export const FREE_STEPS = ["F1", "F2", "F3"];
export const BB_STEPS = ["L1", "L3n", "L3"];

export function siteMemoryPlan(settings, host) {
  if (!settings.siteMemory) return null;
  const rec = loadCooldowns(settings)[host];
  if (!rec?.start && !rec?.bb) return null;
  const now = nowOf(settings);
  const fi = Math.max(0, FREE_STEPS.indexOf(rec.start));
  const bi = Math.max(0, BB_STEPS.indexOf(rec.bb));
  const due = !(Date.parse(rec.probeAt || "") > now - settings.siteMemoryRetryHours * 3600000);
  const probe = due && (fi > 0 || bi > 0);
  return { start: FREE_STEPS[probe ? Math.max(0, fi - 1) : fi], bb: BB_STEPS[probe ? Math.max(0, bi - 1) : bi], probe, remembered: `${FREE_STEPS[fi]}/${BB_STEPS[bi]}` };
}

// win: the rung that read the page (F1, F2, F3, L1, L2, L3n, L3) or null when nothing did.
export function rememberRead(settings, host, { win, plan }) {
  if (!settings.siteMemory || !host || host === "invalid") return;
  if (!win && !plan?.probe) return;
  const hosts = loadCooldowns(settings);
  const now = nowOf(settings);
  const iso = new Date(now).toISOString();
  const rec = { ...(hosts[host] || {}) };
  const was = JSON.stringify(hosts[host] ?? null);
  const had = Boolean(rec.start || rec.bb);
  if (FREE_STEPS.includes(win)) {
    if (win === "F1") delete rec.start;
    else rec.start = win;
  } else if (win === "L1" || win === "L2") delete rec.bb;
  else if (win === "L3n" || win === "L3") rec.bb = win;
  if (win) rec.readAt = iso;
  if (plan?.probe || (!had && (rec.start || rec.bb))) rec.probeAt = iso;
  delete rec.src;
  if (rec.start || rec.bb || rec.until) hosts[host] = rec;
  else delete hosts[host];
  const cut = now - settings.siteMemoryDays * 86400000;
  let pruned = false;
  for (const h of Object.keys(hosts)) {
    const r = hosts[h];
    if (!r.until && !(Date.parse(r.readAt || "") > cut)) {
      delete hosts[h];
      pruned = true;
    }
  }
  if (!pruned && JSON.stringify(hosts[host] ?? null) === was) return; // a host with nothing to remember: no write
  try {
    writeJsonAtomic(cooldownFile(settings), hosts);
  } catch {
    // Memory that fails to persist still holds for this process.
  }
}

// Sliding one-minute windows in <stateDir>/local-rate.json, booked by every room's process together.
// Returns null when a local read may start (and books it), else the reason.
export function takeLocalSlot(settings, host) {
  const full = takeWindow(path.join(settings.stateDir, "local-rate.json"), { key: host, perMinute: settings.localMaxPerMinute, perKey: settings.localMaxPerHostPerMinute, now: nowOf(settings) });
  return full === "all" ? "rate_cap_all" : full === "key" ? "rate_cap_host" : null;
}

// ---- shared verdict for a rendered page (same rules as L3's) ---------------------
export async function judgeRendered({ p, url, settings, extractMode, rung }) {
  let ex = await extractHtml(p.html || "", { url: p.finalUrl || url, extractMode });
  const inner = collapse(p.innerText || "");
  if (inner.length > (ex.articleChars || 0) * 1.5 && inner.length > 200) {
    ex = { ...ex, content: extractMode === "text" ? inner : inner.split("\n").filter(Boolean).join("\n\n"), articleChars: inner.length, extractor: "innertext" };
  }
  const judged = judgePage({ status: p.status, html: p.html || "", finalUrl: p.finalUrl || url, ex, minVisibleChars: settings.minVisibleChars, hardFloor: settings.minVisibleCharsHard });
  const titleCandidates = [p.title, ex.title, ...Array.from(String(p.html || "").matchAll(/<title[^>]*>([\s\S]*?)<\/title>/gi), (m) => m[1])];
  if (titleCandidates.some((t) => /^\s*(undefined|null|\[object Object\])\b/i.test(String(t || "")) || /^\s*General PDP Template\b/i.test(String(t || "")))) {
    return { rung, ok: false, wall: "empty", outcome: "thin:render_empty", status: p.status, verdict: "thin", why: "render_empty", ex };
  }
  let verdict = judged.verdict;
  if (p.challengeWait?.timedOut && verdict === "thin") verdict = "challenge";
  if (verdict === "ok") return { rung, ok: true, status: p.status, finalUrl: p.finalUrl || url, ctype: "text/html", ex, outcome: "ok", verdict };
  return { rung, ok: false, wall: verdict, outcome: `${verdict}${judged.why ? `:${judged.why}` : ""}`, status: p.status, verdict, why: judged.why, ex };
}

// ---- F1: plain local fetch -----------------------------------------------------
async function readCapped(res, maxBytes) {
  const reader = res.body?.getReader?.();
  if (!reader) return Buffer.alloc(0);
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxBytes) {
      await reader.cancel().catch(() => {});
      const e = new Error("too large");
      e.code = "TOO_LARGE";
      throw e;
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks.map((c) => Buffer.from(c)));
}

function decodeBody(buf, ctype) {
  const cs = /charset=([\w-]+)/i.exec(ctype || "")?.[1] || /<meta[^>]+charset=["']?([\w-]+)/i.exec(buf.subarray(0, 4096).toString("latin1"))?.[1] || "utf-8";
  try {
    return new TextDecoder(cs.toLowerCase()).decode(buf);
  } catch {
    return new TextDecoder("utf-8").decode(buf);
  }
}

function localNetWall(err) {
  const code = String(err?.cause?.code || err?.code || "");
  const msg = `${code} ${err?.cause?.message || ""} ${err?.message || ""}`;
  if (err?.name === "TimeoutError" || /UND_ERR_.*TIMEOUT|ETIMEDOUT|aborted due to timeout/i.test(msg)) return "timeout";
  if (/ENOTFOUND|EAI_AGAIN/.test(msg)) return "dns_error";
  if (/CERT|SSL|TLS|self.signed|unable to verify/i.test(msg)) return "tls_error";
  if (code === "TOO_LARGE") return "too_large";
  if (code === "PRIVATE_HOST") return "invalid_url";
  if (code === "TOO_MANY_REDIRECTS") return "connection_failed";
  return "connection_failed";
}

// The free rungs run ON this machine, so unlike Browserbase they can reach its own services
// (Yerk's daemon, trawl, anything on the local network). Every hop is checked: the ladder's entry check sees
// only the first URL, and a public page may redirect to a private one (review, 2026-10-19).
// DNS is checked on the real network only; the harness passes a mock fetch and fake hosts.
async function isPublicTarget(hostname, checkDns, block) {
  if (!block) return true; // blockPrivateHosts off (the harness: its mock targets live on 127.0.0.1)
  if (isPrivateHost(hostname)) return false;
  if (!checkDns || /^[\d.]+$/.test(hostname) || hostname.includes(":")) return true;
  try {
    const addrs = await dns.lookup(hostname, { all: true });
    return !addrs.some((a) => isPrivateHost(a.address));
  } catch {
    return true; // the fetch itself reports the DNS failure
  }
}

async function fetchPublicOnly(url, init, fetchImpl, block, maxHops = 10) {
  let cur = url;
  for (let hop = 0; hop <= maxHops; hop++) {
    const u = new URL(cur);
    if (!/^https?:$/.test(u.protocol) || !(await isPublicTarget(u.hostname, fetchImpl === fetch, block))) {
      const e = new Error(`refused a private or non-web target: ${u.protocol}//${u.hostname}`);
      e.code = "PRIVATE_HOST";
      throw e;
    }
    const res = await fetchImpl(cur, { ...init, redirect: "manual" });
    const loc = res.status >= 300 && res.status < 400 ? res.headers.get("location") : null;
    if (!loc) return { res, finalUrl: cur };
    try {
      await res.body?.cancel();
    } catch {}
    cur = new URL(loc, cur).toString();
  }
  const e = new Error("too many redirects");
  e.code = "TOO_MANY_REDIRECTS";
  throw e;
}

function privateFinal(u) {
  try {
    return isPrivateHost(new URL(u).hostname);
  } catch {
    return false;
  }
}

export async function localFetchRung({ url, settings, extractMode, signal, timeoutMs, fetchImpl = fetch }) {
  const t0 = Date.now();
  let res;
  let buf;
  let hopFinal;
  try {
    const sig = signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs);
    ({ res, finalUrl: hopFinal } = await fetchPublicOnly(url, { headers: browserHeaders(settings), signal: sig }, fetchImpl, settings.blockPrivateHosts));
    buf = await readCapped(res, settings.localMaxBytes);
  } catch (err) {
    if (signal?.aborted) throw err;
    const wall = localNetWall(err);
    // A DNS failure from this machine is a DNS failure everywhere, and a private target is refused on
    // every rung; anything else may be a bot wall (TLS fingerprint resets) or Node's missing
    // intermediate certificates, so let a browser try.
    return { rung: "F1", ok: false, wall, outcome: `net:${wall}`, ms: Date.now() - t0, next: wall === "dns_error" || wall === "invalid_url" ? "stop" : "F2" };
  }
  recordUsage(settings, { localFetchCalls: 1, localBytes: buf.length });
  const status = res.status;
  const ctype = String(res.headers.get("content-type") || "").toLowerCase();
  const finalUrl = hopFinal || res.url || url;
  if (/^(image|audio|video|font)\/|application\/(pdf|zip|octet-stream|x-|vnd\.)/.test(ctype) && !/xml|json/.test(ctype)) {
    return { rung: "F1", ok: false, wall: "binary_content", outcome: `binary:${ctype.split(";")[0]}`, status, ms: Date.now() - t0, next: "stop" };
  }
  const content = decodeBody(buf, ctype);
  const isHtml = /html|xml/.test(ctype) || (!ctype && /^\s*</.test(content));
  let ex;
  if (isHtml) ex = await extractHtml(content, { url: finalUrl, extractMode });
  else {
    const text = collapse(content);
    ex = { title: undefined, content: /json/.test(ctype) ? content : text, visibleText: text, articleChars: text.length, extractor: /json/.test(ctype) ? "json" : "raw", htmlLength: content.length, scriptCount: 0, hasPasswordInput: false, noscriptText: "" };
  }
  const judged = judgePage({ status, html: isHtml ? content : "", finalUrl, ex, minVisibleChars: isHtml ? settings.minVisibleChars : 0, hardFloor: isHtml ? settings.minVisibleCharsHard : 0 });
  const ms = Date.now() - t0;
  if (judged.verdict === "ok") return { rung: "F1", ok: true, status, finalUrl, ctype, ex, ms, outcome: "ok" };
  const outcome = `${judged.verdict}${judged.why ? `:${judged.why}` : ""}`;
  // A 404 or 410 from this machine is the site's answer, as it was at L1. A login wall needs the saved
  // login that lives in the Browserbase context, so it goes straight to L3.
  const next = judged.verdict === "not_found" ? "stop" : judged.verdict === "login" ? "L3" : "F2";
  const partial = ex?.content && String(ex.content).trim() && judged.verdict === "thin" ? { status, finalUrl, ctype, ex } : undefined;
  return { rung: "F1", ok: false, wall: judged.verdict === "not_found" ? `http_${status}` : judged.verdict, verdict: judged.verdict, outcome, status, ms, next, partial };
}

// ---- F2: one local browser, on demand --------------------------------------------
const PICTURE_TYPES = new Set(["image", "media", "font"]);

const EXTRACT_JS = `(() => {
  const b = document.body;
  let text = "";
  if (b) {
    // Import into an inert document: a clone in the live page starts loading every <img> it holds,
    // and the disconnect after the snapshot lets those paused requests out through the proxy
    // (2026-10-19, measured on the mock). A detached clone's innerText was its textContent anyway.
    const c = document.implementation.createHTMLDocument("").importNode(b, true);
    c.querySelectorAll("script,style,noscript,template,svg,nav,header,footer,aside,[role=navigation],[aria-hidden=true]").forEach((e) => e.remove());
    text = c.innerText || c.textContent || "";
  }
  return { html: document.documentElement ? document.documentElement.outerHTML : "", title: document.title || "", url: location.href, text };
})()`;

const PROBE_JS = `(() => ({ title: document.title || "", head: document.documentElement ? document.documentElement.outerHTML.slice(0, 200000) : "", text: document.body ? (document.body.innerText || "").slice(0, 3000) : "" }))()`;

const local = { context: null, launching: null, engine: null, active: 0, waiters: [], idleTimer: null, launches: 0, pid: null };

// Chromium lets one process hold a profile at a time, and every room runs its own yerk-web process: each takes a
// profile slot of its own, <stateDir>/local-profile-<engine>-<n>, claimed by pid in <slot>.owner and taken over once
// its owner is gone (a second launch on a held profile fails "Failed to create a ProcessSingleton").
let mySlot;

function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err?.code === "EPERM";
  }
}

export function claimSlot(base, slots = 8) {
  if (mySlot?.startsWith(base)) return mySlot;
  fs.mkdirSync(path.dirname(base), { recursive: true, mode: 0o700 });
  for (let n = 0; n < slots; n++) {
    const dir = `${base}-${n}`;
    const owner = `${dir}.owner`;
    try {
      fs.writeFileSync(owner, String(process.pid), { flag: "wx", mode: 0o600 });
      return (mySlot = dir);
    } catch (err) {
      if (err?.code !== "EEXIST") throw err;
    }
    const held = Number(fs.readFileSync(owner, "utf8").trim());
    if (held === process.pid) return (mySlot = dir);
    if (alive(held)) continue;
    // The owner is gone: take the slot over with an atomic rename, then check the claim held.
    const tmp = `${owner}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, String(process.pid), { mode: 0o600 });
    fs.renameSync(tmp, owner);
    if (Number(fs.readFileSync(owner, "utf8").trim()) === process.pid) return (mySlot = dir);
  }
  throw new Error(`all ${slots} local browser profile slots are held by live processes`);
}

// The engine F2 needs is on this machine: CloakBrowser's module, or playwright-core and a Chromium. Without it F2 is
// skipped before it starts, so a machine with neither goes from F1 straight on (a stranger's install).
const present = new Map();
const require = createRequire(import.meta.url);

export function localBrowserPresent(settings) {
  const key = [settings.localBrowserEngine, settings.localCloakModulePath, settings.localChromiumPath, settings.playwrightCorePath].join("|");
  if (!present.has(key)) {
    let ok = false;
    try {
      if (settings.localBrowserEngine === "cloak") ok = fs.existsSync(settings.localCloakModulePath);
      else ok = fs.existsSync(settings.localChromiumPath) && Boolean(settings.playwrightCorePath ? fs.existsSync(settings.playwrightCorePath) : require.resolve("playwright-core"));
    } catch {}
    present.set(key, ok);
  }
  return present.get(key);
}

async function launchLocal(settings) {
  const userDataDir = claimSlot(path.join(settings.stateDir, `local-profile-${settings.localBrowserEngine}`));
  fs.mkdirSync(userDataDir, { recursive: true, mode: 0o700 });
  const common = { headless: true, viewport: { width: 1366, height: 900 } };
  let context;
  if (settings.localBrowserEngine === "cloak") {
    process.env.CLOAKBROWSER_AUTO_UPDATE ??= "false";
    if (settings.localCloakCacheDir) process.env.CLOAKBROWSER_CACHE_DIR ??= settings.localCloakCacheDir;
    const cloak = await import(settings.localCloakModulePath);
    context = await cloak.launchPersistentContext({ userDataDir, headless: true, locale: settings.localLocale, timezone: settings.localTimezone });
  } else {
    const pw = await import(settings.playwrightCorePath || "playwright-core");
    context = await pw.chromium.launchPersistentContext(userDataDir, { ...common, executablePath: settings.localChromiumPath || undefined, locale: settings.localLocale, timezoneId: settings.localTimezone, args: ["--disable-blink-features=AutomationControlled"] });
  }
  // Requests to private addresses are refused on this machine (see fetchPublicOnly); pictures too when set.
  await context
    .route("**/*", (route) => {
      const req = route.request();
      let h = "";
      try {
        h = new URL(req.url()).hostname;
      } catch {}
      if (h && settings.blockPrivateHosts && isPrivateHost(h)) return route.abort("blockedbyclient");
      if (settings.localBlockPictures && PICTURE_TYPES.has(req.resourceType())) return route.abort("blockedbyclient");
      return route.continue();
    })
    .catch(() => {});
  local.launches++;
  try {
    local.pid = context.browser?.()?.process?.()?.pid ?? null;
  } catch {}
  context.on("close", () => {
    if (local.context === context) local.context = null;
  });
  return context;
}

async function getLocalContext(settings) {
  if (local.context && local.engine === settings.localBrowserEngine) return local.context;
  if (local.context) await closeLocalBrowser();
  if (!local.launching) {
    local.engine = settings.localBrowserEngine;
    local.launching = launchLocal(settings).then((c) => (local.context = c)).finally(() => (local.launching = null));
  }
  return local.launching;
}

export async function closeLocalBrowser() {
  clearTimeout(local.idleTimer);
  local.idleTimer = null;
  const c = local.context;
  local.context = null;
  if (c) await c.close().catch(() => {});
}

export function localBrowserState() {
  return { open: Boolean(local.context), active: local.active, launches: local.launches, engine: local.engine, waiting: local.waiters.length };
}

async function takePage(settings, deadline) {
  clearTimeout(local.idleTimer);
  local.idleTimer = null;
  while (local.active >= settings.localMaxPages) {
    const left = deadline - Date.now();
    if (left <= 0) return false;
    await new Promise((resolve) => {
      const t = setTimeout(resolve, left);
      local.waiters.push(() => (clearTimeout(t), resolve()));
    });
  }
  local.active++;
  return true;
}

function releasePage(settings) {
  local.active = Math.max(0, local.active - 1);
  const w = local.waiters.shift();
  if (w) w();
  if (local.active === 0 && !local.waiters.length) {
    clearTimeout(local.idleTimer);
    local.idleTimer = setTimeout(() => {
      if (local.active === 0) closeLocalBrowser();
    }, settings.localIdleCloseMs);
    local.idleTimer.unref?.();
  }
}

async function waitOutLocalChallenge({ page, settings, deadline }) {
  const until = Math.min(deadline - 1500, Date.now() + settings.localChallengeWaitMs);
  let sawChallenge = false;
  while (Date.now() < until) {
    let p;
    try {
      p = await page.evaluate(PROBE_JS);
    } catch {
      await sleep(500);
      continue; // the challenge page navigated away mid-probe; look again
    }
    if (!detectChallenge({ html: p.head, title: p.title, visibleText: p.text })) {
      if (sawChallenge) await page.waitForLoadState("networkidle", { timeout: Math.max(1, Math.min(6000, deadline - Date.now() - 1500)) }).catch(() => {});
      return { sawChallenge };
    }
    sawChallenge = true;
    await sleep(1000);
  }
  return { sawChallenge, timedOut: true };
}

export async function localBrowserRung({ url, settings, extractMode, signal, budgetMs }) {
  const t0 = Date.now();
  const deadline = t0 + Math.max(3000, budgetMs);
  if (!(await takePage(settings, Math.min(deadline - 5000, t0 + settings.localQueueWaitMs)))) {
    return { rung: "F2", ok: false, wall: "busy", outcome: "skipped:busy", ms: Date.now() - t0, next: "BB", skipped: true };
  }
  let page;
  try {
    let context;
    try {
      context = await getLocalContext(settings);
    } catch (err) {
      return { rung: "F2", ok: false, wall: "local_launch", outcome: `launch:${String(err?.message || err).split("\n")[0].slice(0, 60)}`, ms: Date.now() - t0, next: "BB" };
    }
    page = await context.newPage();
    const remaining = () => Math.max(1000, deadline - Date.now());
    let status;
    page.on("response", (r) => {
      try {
        if (r.request().isNavigationRequest() && r.frame() === page.mainFrame()) status = r.status();
      } catch {}
    });
    let navError;
    try {
      const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: remaining() });
      status ??= resp?.status();
    } catch (err) {
      navError = String(err?.message || err).split("\n")[0].slice(0, 160);
    }
    if (signal?.aborted) throw signal.reason ?? new Error("aborted");
    let challengeWait = {};
    if (!navError) {
      await page.waitForLoadState("networkidle", { timeout: Math.max(1, Math.min(settings.localNetworkIdleMs, remaining() - 1500)) }).catch(() => {});
      challengeWait = await waitOutLocalChallenge({ page, settings, deadline });
    }
    const snap = await page.evaluate(EXTRACT_JS).catch(() => null);
    recordUsage(settings, { localBrowserPages: 1, localBrowserSeconds: (Date.now() - t0) / 1000 });
    if (!snap || (navError && !snap.html)) {
      return { rung: "F2", ok: false, wall: "nav_error", outcome: `nav:${(navError || "no snapshot").slice(0, 60)}`, status, ms: Date.now() - t0, next: "BB" };
    }
    // A navigation redirect skips the route handler, so a page that landed on a private address is refused here.
    if (settings.blockPrivateHosts && privateFinal(snap.url)) return { rung: "F2", ok: false, wall: "invalid_url", outcome: "refused:private_target", status, ms: Date.now() - t0, next: "stop" };
    const r = await judgeRendered({ p: { status, finalUrl: snap.url, title: snap.title, html: snap.html, innerText: snap.text, challengeWait }, url, settings, extractMode, rung: "F2" });
    const ms = Date.now() - t0;
    if (r.ok) return { ...r, ms };
    const next = r.verdict === "not_found" ? "stop" : "BB";
    return { ...r, wall: r.verdict === "not_found" ? `http_${status}` : r.wall, ms, next, partial: r.ex?.content && String(r.ex.content).trim() && r.verdict === "thin" ? { status, finalUrl: snap.url, ctype: "text/html", ex: r.ex } : undefined };
  } catch (err) {
    if (signal?.aborted) throw err;
    return { rung: "F2", ok: false, wall: "nav_error", outcome: `error:${String(err?.message || err).split("\n")[0].slice(0, 60)}`, ms: Date.now() - t0, next: "BB" };
  } finally {
    await page?.close().catch(() => {});
    releasePage(settings);
  }
}

// ---- F3: trawl's browser tier, after F2 met a challenge --------------------------
export async function localTrawlRung({ url, settings, extractMode, signal, budgetMs, fetchImpl = fetch }) {
  const t0 = Date.now();
  let res;
  let j;
  try {
    const sig = AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(Math.max(3000, budgetMs))]);
    res = await fetchImpl(`${settings.localTrawlUrl}/scrape`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url, skipHttp: true, solveEmbeddedCaptchas: true, timeout: Math.max(5000, budgetMs - 2000) }), signal: sig });
    j = await res.json().catch(() => ({}));
  } catch (err) {
    if (signal?.aborted) throw err;
    const down = /ECONNREFUSED|ECONNRESET|fetch failed/i.test(`${err?.cause?.code || ""} ${err?.message || ""}`);
    return { rung: "F3", ok: false, wall: down ? "trawl_down" : "timeout", outcome: down ? "skipped:trawl_not_running" : "timeout", ms: Date.now() - t0, skipped: down };
  }
  recordUsage(settings, { localTrawlCalls: 1, localTrawlSeconds: (Date.now() - t0) / 1000 });
  if (settings.blockPrivateHosts && j?.url && privateFinal(j.url)) return { rung: "F3", ok: false, wall: "invalid_url", outcome: "refused:private_target", ms: Date.now() - t0, next: "stop" };
  if (!res.ok || typeof j?.html !== "string") {
    return { rung: "F3", ok: false, wall: "blocked", verdict: "blocked", outcome: `trawl:http_${res.status}`, status: typeof j?.statusCode === "number" ? j.statusCode : undefined, ms: Date.now() - t0 };
  }
  const r = await judgeRendered({ p: { status: typeof j.statusCode === "number" ? j.statusCode : 200, finalUrl: j.url || url, title: "", html: j.html, innerText: "" }, url, settings, extractMode, rung: "F3" });
  return { ...r, ms: Date.now() - t0 };
}

export const _test = { local, present, resetGuards: () => ((cooldownCache = undefined), (mySlot = undefined), present.clear()) };
