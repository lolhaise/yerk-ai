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
import { readAnswers, writtenQuestion } from "./commons.js";
import { detectChallenge, judgePage } from "./detect.js";
import { DOCUMENT_CAP, documentKind, mayBeDocument } from "./document.js";
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
export function browserHeaders(settings) {
  const lang = [settings.localLocale, settings.localLocale !== "en-US" && "en-US;q=0.9", "en;q=0.8"].filter(Boolean).join(",");
  return { ...BROWSER_HEADERS, "accept-language": lang };
}

// ---- home guards: cooldown and rate cap (both persisted, shared by every room's process) ----
let cooldownCache;

function cooldownFile(settings) {
  return path.join(settings.stateDir, "local-cooldown.json");
}

// A worker's process (where's `own`) starts from the rooms' shared file until its own folder holds one, so a host resting
// from the free rungs rests for it too and a read starts where the rooms last read that host; what it learns stays in
// its own file, since it may not write the shared one.
function loadCooldowns(settings) {
  if (!cooldownCache || cooldownCache.file !== cooldownFile(settings)) {
    const shared = settings.sharedStateDir ? path.join(settings.sharedStateDir, "local-cooldown.json") : "";
    const hosts = readJson(cooldownFile(settings), null) || (shared && readJson(shared, null)) || {};
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

// Path-class memory (2026-10-22): one host serves pages that read on different rungs (Airbnb: /stays read at
// F1 in 0.6 s, /rooms needed F2, /s went to F2), and one entry per host made every page start where the last
// page needed. Memory is now also kept per host + path class, key "www.airbnb.ca/rooms": the class is the
// first path segment that reads as a route word (letters, at most one hyphen, 20 chars; the second segment
// is tried when the first is a place slug or an id, so /6th-arrondissement-paris-france/stays/... is /stays),
// else "/". The host entry is still written as before and stands in for a class not seen yet; a new class
// entry starts from it. A class entry with no start or bb is kept while the host entry has one, so a class
// that reads at the defaults is not sent back to the host's start. Re-probe: siteMemoryRetryHours per class.
export function pathClass(url) {
  let segs = [];
  try {
    segs = new URL(url).pathname.split("/").filter(Boolean);
  } catch {
    return "/";
  }
  for (const s of segs.slice(0, 2)) {
    const w = s.toLowerCase();
    if (w.length <= 20 && /^[a-z][a-z_]*(-[a-z_]+)?$/.test(w)) return `/${w}`;
  }
  return "/";
}

export const memoryKey = (host, url) => `${host}${pathClass(url)}`;

export function siteMemoryPlan(settings, host, url) {
  if (!settings.siteMemory) return null;
  const hosts = loadCooldowns(settings);
  const rec = (url ? hosts[memoryKey(host, url)] : undefined) || hosts[host];
  if (!rec?.start && !rec?.bb) return null;
  const now = nowOf(settings);
  const fi = Math.max(0, FREE_STEPS.indexOf(rec.start));
  const bi = Math.max(0, BB_STEPS.indexOf(rec.bb));
  const due = !(Date.parse(rec.probeAt || "") > now - settings.siteMemoryRetryHours * 3600000);
  const probe = due && (fi > 0 || bi > 0);
  return { start: FREE_STEPS[probe ? Math.max(0, fi - 1) : fi], bb: BB_STEPS[probe ? Math.max(0, bi - 1) : bi], probe, remembered: `${FREE_STEPS[fi]}/${BB_STEPS[bi]}` };
}

// win: the rung that read the page (F1, F2, F3, L1, L2, L3n, L3) or null when nothing did.
function updatedMemory(prev, { win, plan, iso }) {
  const rec = { ...(prev || {}) };
  const had = Boolean(rec.start || rec.bb);
  if (FREE_STEPS.includes(win)) {
    if (win === "F1") delete rec.start;
    else rec.start = win;
  } else if (win === "L1" || win === "L2") delete rec.bb;
  else if (win === "L3n" || win === "L3") rec.bb = win;
  if (win) rec.readAt = iso;
  if (plan?.probe || (!had && (rec.start || rec.bb))) rec.probeAt = iso;
  delete rec.src;
  return rec;
}

export function rememberRead(settings, host, { win, plan, url }) {
  if (!settings.siteMemory || !host || host === "invalid") return;
  if (!win && !plan?.probe) return;
  const hosts = loadCooldowns(settings);
  const now = nowOf(settings);
  const iso = new Date(now).toISOString();
  const key = url ? memoryKey(host, url) : null;
  const was = JSON.stringify([hosts[host] ?? null, key ? hosts[key] ?? null : null]);
  const prevHost = hosts[host];
  const rec = updatedMemory(prevHost, { win, plan, iso });
  if (rec.start || rec.bb || rec.until) hosts[host] = rec;
  else delete hosts[host];
  if (key) {
    const seed = hosts[key] || (prevHost && Object.fromEntries(["start", "bb", "probeAt", "readAt"].filter((k) => prevHost[k]).map((k) => [k, prevHost[k]])));
    const cls = updatedMemory(seed, { win, plan, iso });
    if (cls.start || cls.bb || hosts[host]?.start || hosts[host]?.bb) hosts[key] = cls;
    else delete hosts[key];
  }
  const cut = now - settings.siteMemoryDays * 86400000;
  let pruned = false;
  for (const h of Object.keys(hosts)) {
    const r = hosts[h];
    if (!r.until && !(Date.parse(r.readAt || "") > cut)) {
      delete hosts[h];
      pruned = true;
    }
  }
  if (!pruned && JSON.stringify([hosts[host] ?? null, key ? hosts[key] ?? null : null]) === was) return; // a host with nothing to remember: no write
  try {
    writeJsonAtomic(cooldownFile(settings), hosts);
  } catch {
    // Memory that fails to persist still holds for this process.
  }
}

// Sliding one-minute windows in <stateDir>/local-rate.json, booked by every room's process together; a worker's
// process books in its own folder's and counts the shared one with it (takeWindow's `also`).
// Returns null when a local read may start (and books it), else the reason.
export function takeLocalSlot(settings, host) {
  const also = settings.sharedStateDir ? [path.join(settings.sharedStateDir, "local-rate.json")] : [];
  const full = takeWindow(path.join(settings.stateDir, "local-rate.json"), { key: host, perMinute: settings.localMaxPerMinute, perKey: settings.localMaxPerHostPerMinute, now: nowOf(settings), also });
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
  const titleCandidates = [p.title, ex.title, ...Array.from(String(p.html || "").matchAll(/<title[^>]*>([\s\S]*?)<\/title>/gi), (m) => m[1]), ...Array.from(String(p.html || "").matchAll(/<meta[^>]+(?:property|name)=["']og:title["'][^>]+content=["']([^"']*)["']/gi), (m) => m[1])];
  if (titleCandidates.some((t) => /^\s*(undefined|null|\[object Object\])\b/i.test(String(t || "")) || /^\s*General PDP Template\b/i.test(String(t || "")))) {
    return { rung, ok: false, wall: "empty", outcome: "thin:render_empty", status: p.status, verdict: "thin", why: "render_empty", ex };
  }
  let verdict = judged.verdict;
  if (p.challengeWait?.timedOut && verdict === "thin") verdict = "challenge";
  if (verdict === "ok") return { rung, ok: true, status: p.status, finalUrl: p.finalUrl || url, ctype: "text/html", ex, outcome: "ok", verdict };
  return { rung, ok: false, wall: verdict, outcome: `${verdict}${judged.why ? `:${judged.why}` : ""}`, status: p.status, verdict, why: judged.why, ex };
}

// ---- F1: plain local fetch -----------------------------------------------------
export async function readCapped(res, maxBytes) {
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

export function localNetWall(err) {
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

export async function fetchPublicOnly(url, init, fetchImpl, block, maxHops = 10) {
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
    buf = await readCapped(res, mayBeDocument(res.headers.get("content-type")) ? Math.max(settings.localMaxBytes, DOCUMENT_CAP) : settings.localMaxBytes);
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
  // A PDF or a Word file (need 87, document.js), known by its own first bytes: the read is its bytes, which web_fetch saves
  // for the room and reads the words of; a .docx's type says "xml" and was once read as text.
  const kind = status < 400 && !/html|^text\/|json/.test(ctype) ? documentKind(buf) : "";
  if (kind) return { rung: "F1", ok: true, status, finalUrl, ctype, ex: { content: "" }, document: { kind, buf }, ms: Date.now() - t0, outcome: "ok" };
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
  // A House of Commons written question (need 37, commons.js): its answer loads after the page opens, by a request that
  // needs the page's token and cookie, so F1 makes it here and the answer follows the question; a page it knows is a read.
  const wq = isHtml && status === 200 ? writtenQuestion(finalUrl) : null;
  const answers = wq ? await readAnswers({ wq: { ...wq, section: wq.section || writtenQuestion(url)?.section || "" }, html: content, cookies: res.headers.getSetCookie?.() ?? [], headers: browserHeaders(settings), fetchImpl, signal, timeoutMs, read: (r) => readCapped(r, settings.localMaxBytes), extractMode }) : null;
  if (answers?.calls) recordUsage(settings, { localFetchCalls: answers.calls, localBytes: answers.bytes });
  if (answers?.text) ex = { ...ex, content: [ex.content, answers.text].filter((s) => String(s ?? "").trim()).join("\n\n"), articleChars: (ex.articleChars || 0) + answers.chars };
  const judged = judgePage({ status, html: isHtml ? content : "", finalUrl, ex, minVisibleChars: isHtml ? settings.minVisibleChars : 0, hardFloor: isHtml ? settings.minVisibleCharsHard : 0 });
  const ms = Date.now() - t0;
  if (judged.verdict === "ok" || (answers?.page && judged.verdict === "thin")) return { rung: "F1", ok: true, status, finalUrl, ctype, ex, ms, outcome: "ok", ...(answers ? { answers: { note: answers.note, outcome: answers.outcome } } : {}) };
  const outcome = `${judged.verdict}${judged.why ? `:${judged.why}` : ""}`;
  // A 404 or 410 from this machine is the site's answer, as it was at L1. A login wall needs the saved
  // login that lives in the Browserbase context, so it goes straight to L3.
  const next = judged.verdict === "not_found" ? "stop" : judged.verdict === "login" ? "L3" : "F2";
  const partial = ex?.content && String(ex.content).trim() && judged.verdict === "thin" ? { status, finalUrl, ctype, ex } : undefined;
  // finalUrl on a failed read too (2026-11-08): where the HTTP redirect chain ended is the site's own answer, and
  // the ladder's off-site landing guard admits that domain for the browser rungs that follow.
  return { rung: "F1", ok: false, wall: judged.verdict === "not_found" ? `http_${status}` : judged.verdict, verdict: judged.verdict, outcome, status, ms, next, partial, finalUrl };
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
    // A modal over the page (2026-10-22, extract.js liftModal): its hide-others siblings are un-hidden and
    // the dialog dropped when they hold 1,000+ chars, or an Airbnb room page reads as "Translation on".
    const ms = [...c.querySelectorAll('[aria-modal="true"],[role="dialog"],[role="alertdialog"],dialog[open]')];
    const hid = new Set();
    for (const m of ms) for (let a = m; a && a !== c && a.parentElement; a = a.parentElement) for (const s of a.parentElement.children) if (s !== a && s.getAttribute("aria-hidden") === "true") hid.add(s);
    if ([...hid].reduce((n, s) => n + (s.textContent || "").length, 0) >= 1000) {
      hid.forEach((s) => (s.removeAttribute("aria-hidden"), s.removeAttribute("inert")));
      ms.forEach((m) => m.remove());
    }
    c.querySelectorAll("script,style,noscript,template,svg,nav,header,footer,aside,[role=navigation],[aria-hidden=true]").forEach((e) => e.remove());
    text = c.innerText || c.textContent || "";
  }
  return { html: document.documentElement ? document.documentElement.outerHTML : "", title: document.title || "", url: location.href, text };
})()`;

// The probe (2026-10-21): the title, the first 200 KB of HTML, the first 4,000 chars of the visible text
// and its whole length (detect.js reads the length; a cut text alone made every real page "short").
const PROBE_JS = `(() => { const t = document.body ? (document.body.innerText || "") : ""; return { title: document.title || "", head: document.documentElement ? document.documentElement.outerHTML.slice(0, 200000) : "", text: t.slice(0, 4000), len: t.length }; })()`;

// Main-frame requests the readable-stop waits on: a page whose data is still coming reads as its loading
// skeleton (op.gg: 2,716 visible chars at domcontentloaded, 9,230 five seconds later, 17 fetches in between).
const INFLIGHT_TYPES = new Set(["document", "script", "xhr", "fetch"]);

// Challenge kinds that clear on their own in a stealth browser (a JavaScript interstitial that sets a cookie
// and reloads). A recaptcha, hcaptcha, arkose, datadome or perimeterx page needs a human or a solver, and a
// block page never changes, so the challenge loop does not wait on those (it spent localChallengeWaitMs on each).
const SELF_CLEARING = new Set(["challenge_title", "cloudflare", "turnstile", "reddit_js_challenge", "aws_waf", "challenge_text"]);

const probeChallenge = (p) => detectChallenge({ html: p.head, title: p.title, visibleText: p.text, visibleLength: p.len });

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

// The readable-stop (2026-10-21), after domcontentloaded, in place of a flat networkidle wait: every
// localReadablePollMs the probe reads the page, and the wait ends on the first of: the page is a challenge
// (the loop below takes over); the visible text is at least localReadableMinChars, unchanged since the last
// poll, with no main-frame document, script, xhr or fetch request open at either poll ("readable");
// Playwright's networkidle; or localNetworkIdleMs, the ceiling (kept at 6,000). A page still UNDER the
// threshold with main-frame requests in flight is demonstrably still loading, so it may go on past the
// ceiling up to localReadableWaitMs ("loading" when that runs out): op.gg's summoner data landed 3.5, 5.1
// and over 6 s after domcontentloaded in three traces, and at the plain ceiling one read took its
// 3,339-char skeleton for the page. The first probe runs one poll after domcontentloaded, since a page's
// data fetches start after it.
async function settleReadable({ page, settings, deadline, inflight }) {
  const t0 = Date.now();
  const until = Math.min(deadline - 1500, t0 + settings.localNetworkIdleMs);
  const untilLoading = Math.min(deadline - 1500, t0 + Math.max(settings.localNetworkIdleMs, settings.localReadableWaitMs));
  let idle = false;
  const idleP = page.waitForLoadState("networkidle", { timeout: Math.max(1, untilLoading - t0) }).then(() => (idle = true), () => {});
  let prev = null;
  let polls = 0;
  let len = 0;
  for (;;) {
    const loading = len < settings.localReadableMinChars && inflight.size > 0;
    const left = (loading ? untilLoading : until) - Date.now();
    const done = idle ? "networkidle" : left <= 0 ? (loading ? "loading" : "ceiling") : null;
    if (!done) await Promise.race([sleep(Math.min(settings.localReadablePollMs, left)), idleP]); // an idle stop lands at once, not at the next poll
    let p;
    try {
      p = await page.evaluate(PROBE_JS);
    } catch {
      if (done) return { stop: done, polls, len };
      prev = null; // navigating mid-probe (a redirect, a reload): start the two-poll count over
      continue;
    }
    polls++;
    len = p.len;
    const seen = probeChallenge(p);
    if (seen) return { stop: "challenge", polls, len, seen };
    if (done) return { stop: done, polls, len, inflight: inflight.size };
    const quiet = inflight.size === 0;
    if (p.len >= settings.localReadableMinChars && prev?.len === p.len && prev.quiet && quiet) return { stop: "readable", polls, len };
    prev = { len: p.len, quiet };
  }
}

// Runs only after the probe called the page a challenge. Polls every second while the page is a
// self-clearing kind and clears, up to localChallengeWaitMs; returns at once on a kind that cannot clear.
async function waitOutLocalChallenge({ page, settings, deadline, seen }) {
  const until = Math.min(deadline - 1500, Date.now() + settings.localChallengeWaitMs);
  let why = seen?.why || "challenge";
  if (!SELF_CLEARING.has(why)) return { sawChallenge: true, why, timedOut: true };
  while (Date.now() < until) {
    await sleep(Math.min(1000, Math.max(1, until - Date.now())));
    let p;
    try {
      p = await page.evaluate(PROBE_JS);
    } catch {
      continue; // the challenge page navigated away mid-probe; look again
    }
    const c = probeChallenge(p);
    if (!c) return { sawChallenge: true, why };
    why = c.why;
    if (!SELF_CLEARING.has(why)) return { sawChallenge: true, why, timedOut: true };
  }
  return { sawChallenge: true, why, timedOut: true };
}

// The guest step (need 21, 2026-11-02), only when the ladder says the page is a Microsoft Bookings page: with the
// anonymous flag the page opens on "Sign in or continue as guest", and its meeting types and open times show only
// after a click on "Continue as guest", the URL unchanged (read live: 141 visible chars before the click, 464 after).
// The control is clicked once when it shows (GUEST_SHOW_MS at most), then the wait runs until it is gone and the text
// is unchanged across two polls with none of the click's own main-frame requests open, up to localReadableWaitMs: two
// scripts the choice screen asked for never finished on the live page, its guest view steady 0.5 s after the click.
// networkidle is no stop here: the page reached it on the choice screen, and a click that navigates nowhere does not
// start it again.
// "clicked": the guest view showed; "stuck": the control was still up when the wait ran out; null: nothing to click.
const GUEST_CONTROL = /^\s*continue as guest\s*$/i;
const GUEST_SHOW_MS = 5000;

async function continueAsGuest({ page, settings, deadline, inflight }) {
  const control = page.getByRole("button", { name: GUEST_CONTROL }).or(page.getByRole("link", { name: GUEST_CONTROL })).or(page.getByText(GUEST_CONTROL)).first();
  const until = Math.min(deadline - 1500, Date.now() + settings.localReadableWaitMs);
  let older;
  try {
    await control.waitFor({ state: "visible", timeout: Math.max(1, Math.min(GUEST_SHOW_MS, until - Date.now())) });
    older = new Set(inflight);
    await control.click({ timeout: Math.max(1, until - Date.now()) });
  } catch {
    return null; // no such control: the page shows its times already, or went to a sign-in
  }
  const shown = () => control.isVisible().catch(() => false);
  let prev = null;
  while (Date.now() < until) {
    await sleep(Math.min(settings.localReadablePollMs, Math.max(1, until - Date.now())));
    let p;
    try {
      p = await page.evaluate(PROBE_JS);
    } catch {
      prev = null; // navigating mid-probe: start the two-poll count over
      continue;
    }
    const quiet = [...inflight].every((r) => older.has(r));
    if (prev?.text === p.text && prev.quiet && quiet && !(await shown())) return "clicked";
    prev = { text: p.text, quiet };
  }
  return (await shown()) ? "stuck" : "clicked";
}

// phases (2026-10-21): the F2 split in ms, carried on the result and into the call log, so the next
// measurement of this rung reads off the ledger: take (the page slot), launch (cold true when the
// browser had to start), page, goto (to domcontentloaded), settle (the quiet wait), challenge (the
// probe loop, 0 when the page was no challenge), guest (the guest step, on a Bookings page only), extract (the
// snapshot), judge (extract.js + detect.js). `guest` (need 21): the ladder's word that the page is a Bookings page.
export async function localBrowserRung({ url, settings, extractMode, signal, budgetMs, guest = false }) {
  const t0 = Date.now();
  const deadline = t0 + Math.max(3000, budgetMs);
  const phases = { take: 0, launch: 0, cold: false, page: 0, goto: 0, settle: 0, challenge: 0, extract: 0, judge: 0 };
  let lapAt = t0;
  const lap = (k) => {
    const n = Date.now();
    phases[k] = n - lapAt;
    lapAt = n;
  };
  if (!(await takePage(settings, Math.min(deadline - 5000, t0 + settings.localQueueWaitMs)))) {
    return { rung: "F2", ok: false, wall: "busy", outcome: "skipped:busy", ms: Date.now() - t0, next: "BB", skipped: true };
  }
  lap("take");
  let page;
  try {
    let context;
    try {
      phases.cold = !local.context;
      context = await getLocalContext(settings);
    } catch (err) {
      return { rung: "F2", ok: false, wall: "local_launch", outcome: `launch:${String(err?.message || err).split("\n")[0].slice(0, 60)}`, ms: Date.now() - t0, next: "BB" };
    }
    lap("launch");
    page = await context.newPage();
    lap("page");
    const remaining = () => Math.max(1000, deadline - Date.now());
    let status;
    page.on("response", (r) => {
      try {
        if (r.request().isNavigationRequest() && r.frame() === page.mainFrame()) status = r.status();
      } catch {}
    });
    const inflight = new Set();
    page.on("request", (r) => {
      try {
        if (INFLIGHT_TYPES.has(r.resourceType()) && r.frame() === page.mainFrame()) inflight.add(r);
      } catch {}
    });
    page.on("requestfinished", (r) => inflight.delete(r));
    page.on("requestfailed", (r) => inflight.delete(r));
    let navError;
    try {
      const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: remaining() });
      status ??= resp?.status();
    } catch (err) {
      navError = String(err?.message || err).split("\n")[0].slice(0, 160);
    }
    lap("goto");
    if (signal?.aborted) throw signal.reason ?? new Error("aborted");
    let challengeWait = {};
    let guestStep = null;
    if (!navError) {
      let settled = await settleReadable({ page, settings, deadline, inflight });
      lap("settle");
      if (settled.stop === "challenge") {
        challengeWait = await waitOutLocalChallenge({ page, settings, deadline, seen: settled.seen });
        phases.kind = challengeWait.why;
        if (!challengeWait.timedOut) settled = await settleReadable({ page, settings, deadline, inflight });
      }
      lap("challenge");
      if (guest) {
        guestStep = await continueAsGuest({ page, settings, deadline, inflight });
        lap("guest");
      }
      phases.stop = settled.stop;
      phases.polls = settled.polls;
      phases.len = settled.len;
    }
    const snap = await page.evaluate(EXTRACT_JS).catch(() => null);
    lap("extract");
    recordUsage(settings, { localBrowserPages: 1, localBrowserSeconds: (Date.now() - t0) / 1000 });
    if (!snap || (navError && !snap.html)) {
      return { rung: "F2", ok: false, wall: "nav_error", outcome: `nav:${(navError || "no snapshot").slice(0, 60)}`, status, ms: Date.now() - t0, next: "BB", phases };
    }
    // A navigation redirect skips the route handler, so a page that landed on a private address is refused here.
    if (settings.blockPrivateHosts && privateFinal(snap.url)) return { rung: "F2", ok: false, wall: "invalid_url", outcome: "refused:private_target", status, ms: Date.now() - t0, next: "stop", phases };
    const r = await judgeRendered({ p: { status, finalUrl: snap.url, title: snap.title, html: snap.html, innerText: snap.text, challengeWait }, url, settings, extractMode, rung: "F2" });
    lap("judge");
    const ms = Date.now() - t0;
    const shown = guestStep ? { guest: guestStep } : {};
    if (r.ok) return { ...r, ms, phases, ...shown };
    // Past the guest choice a short but real page is the answer, as at L3: the guest view's meeting types and open
    // times are a few hundred chars in an app's large document, which reads as thin.
    if (guestStep === "clicked" && r.verdict === "thin" && String(r.ex?.content ?? "").trim()) {
      return { rung: "F2", ok: true, status, finalUrl: snap.url, ctype: "text/html", ex: r.ex, outcome: "ok:short", short: true, verdict: r.verdict, ms, phases, ...shown };
    }
    const next = r.verdict === "not_found" ? "stop" : "BB";
    return { ...r, wall: r.verdict === "not_found" ? `http_${status}` : r.wall, ms, next, phases, ...shown, partial: r.ex?.content && String(r.ex.content).trim() && r.verdict === "thin" ? { status, finalUrl: snap.url, ctype: "text/html", ex: r.ex } : undefined };
  } catch (err) {
    if (signal?.aborted) throw err;
    return { rung: "F2", ok: false, wall: "nav_error", outcome: `error:${String(err?.message || err).split("\n")[0].slice(0, 60)}`, ms: Date.now() - t0, next: "BB", phases };
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
