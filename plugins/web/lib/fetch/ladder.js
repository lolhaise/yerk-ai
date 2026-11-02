// The ladder. Runs in code on every web_fetch call (tools.mjs):
//   F1  plain fetch from this machine, free                (block or JS shell -> F2)
//   F2  local headless browser on this machine, free       (a challenge -> F3, else -> L1)
//   F3  trawl's Camoufox on this machine, free, solves captchas locally (when its service runs)
//   L1  Browserbase Fetch, no proxies           (block signature -> L2)
//   L2  Browserbase Fetch, proxies on           (still walled    -> L3)
//   L3n Browserbase browser session WITHOUT proxies, pictures on (unproxied bytes are not billed)
//   L3  Browserbase browser session: proxies, captcha solving, persistent context, pictures off
//       (retried once with pictures on when it reads as a captcha, a challenge or thin)
// Site memory (local.js) picks where a remembered host's climb starts; the checks are unchanged.
// Success returns the page with extractor "fetch-plus:<rung>"; failure names the wall and every
// rung tried. Both carry `paid`, the Browserbase calls the read paid for (tools.mjs books them).
import { BrowserbaseError, bbFetch } from "./browserbase.js";
import { judgePage } from "./detect.js";
import { collapse, extractHtml, extractMarkdown, markdownToText } from "./extract.js";
import { runSessionRung, wallFromNavError } from "./l3.js";
import { closeLocalBrowser, localBrowserPresent, localBrowserRung, localBrowserState, localCooldownFor, localFetchRung, localTrawlRung, markLocalBlocked, rememberRead, siteMemoryPlan, takeLocalSlot } from "./local.js";
import { appendCallLog, hostOf, isPrivateHost, recordUsage, resolveSettings, syncProjectUsage } from "./util.js";

const WALL_TEXT = {
  no_key: ["No Browserbase API key is configured, so no rung could run.", "no", "Put a Browserbase key in the private folder's secrets/browserbase.key, or turn the free rungs on (localEnabled)."],
  bb_auth: ["Browserbase rejected the API key.", "no", "Check secrets/browserbase.key and that the Browserbase account is active."],
  bb_quota: ["Browserbase says the plan quota is used up.", "no", "Check the Browserbase billing page; overage may be switched off."],
  bb_unreachable: ["Browserbase's API could not be reached from this machine.", "yes", "Retry in a minute; if it persists this machine's network or Browserbase is down."],
  bb_unavailable: ["Browserbase's service answered 503 (temporarily unavailable).", "yes", "Retry shortly."],
  bb_concurrency: ["Browserbase's concurrent browser or fetch limit was hit.", "yes", "Retry shortly; another job is using the browsers."],
  bb_timeout: ["Browserbase did not answer in time.", "yes", "Retry."],
  login_required: ["The site wants a login before it shows this page.", "after login", "Log in once inside this site's fetch-plus context (see contexts.json for its id) and every later read reuses that login."],
  captcha_unsolved: ["A captcha or bot challenge was still up after the browser and captcha solver ran.", "later", "Retry later, or open the page in the owner's own browser."],
  geo_blocked: ["The site says this content is not available in the proxy's region.", "maybe", "Retry with a geolocated proxy, or read it from the owner's own browser."],
  proxy_restricted: ["Browserbase's residential proxy provider refuses this site category, and the no-proxy browser also failed.", "no", "Open it in the owner's own browser."],
  timeout: ["The page did not finish loading inside the time cap.", "yes", "Retry; the site may be slow right now."],
  tls_error: ["The site's TLS certificate failed verification.", "no", "Only if the site is trusted: set allowInsecureSsl true in web.json's fetch settings."],
  dns_error: ["The hostname does not resolve.", "no", "Check the URL spelling."],
  connection_failed: ["The site refused or dropped the connection.", "yes", "Retry; the site may be down."],
  too_large: ["The page is over Browserbase Fetch's 5 MB limit and the browser rung could not read it either.", "no", "Fetch a smaller page or a specific section."],
  binary_content: ["The URL returns a binary file, not a web page.", "no", "An image: use web_image(url), then Read the file it saves; these tools read no other binary file."],
  invalid_url: ["The URL is not a public http(s) address.", "no", "Use a public http or https URL."],
  guest_choice: ["This Bookings page stayed on Microsoft's \"Sign in or continue as guest\" screen: its meeting types and open times show only after a click on Continue as guest, which only the machine's own browser (F2) makes, and no rung got past it.", "maybe", "Retry in a minute (the machine's browser may have been busy); if it stays, ask the page's owner for the open times in words."],
  empty: ["Every rung loaded the page but found no readable text.", "maybe", "The content may be inside an image, a canvas or an app that needs interaction."],
};

// A Microsoft Bookings page is read as a guest (need 21, 2026-10-24): every share link Microsoft makes for a Bookings
// with me page carries `anonymous` (.../bookwithme/user/<id>@<domain>?anonymous&ep=pcard), and the same page without it
// sends a reader to Microsoft's sign-in, so every rung met login_required on a page anyone may book. The flag goes
// first when it is missing, as Microsoft writes it; the page's owner still decides, and a guest view that asks for a
// sign-in is named as such. Hosts: Outlook's (office.com, office365.com and the government cloud's) and cloud.microsoft.
// Read live on 2026-11-02, the flagged page opens on "Sign in or continue as guest", and its meeting types and open
// times show only after a click on "Continue as guest": F2 makes it (local.js continueAsGuest), and a read that is
// still that screen is no answer at any rung (guest_choice; L3's short-page rule took it for one).
const BOOKINGS_HOSTS = ["office.com", "office365.com", "office365.us", "cloud.microsoft"];

// The guest view went on to a sign-in: no saved login is the way to a page Microsoft shows a guest that way.
const BOOKINGS_SIGNIN = ["This Bookings page went to Microsoft's sign-in even in its guest view (anonymous).", "no", "Ask the page's owner for the share link of a public meeting type, or for the open times in words."];

export function isBookingsPage(url) {
  try {
    const u = new URL(url);
    const h = u.hostname.toLowerCase();
    return BOOKINGS_HOSTS.some((d) => h === d || h.endsWith(`.${d}`)) && /^\/bookwithme\//i.test(u.pathname);
  } catch {
    return false;
  }
}

// The guest choice screen's own words; the guest view past it has no such line.
export function onGuestChoice(text) {
  return /\bcontinue as guest\b/i.test(String(text ?? ""));
}

export function bookingsGuestUrl(url) {
  if (!isBookingsPage(url)) return url;
  const u = new URL(url);
  const q = u.search.slice(1);
  if (q.split("&").some((kv) => kv.split("=")[0].toLowerCase() === "anonymous")) return url;
  u.search = `?anonymous${q ? `&${q}` : ""}`;
  return u.toString();
}

export function wallFromBbError(err) {
  if (!(err instanceof BrowserbaseError)) return "bb_unreachable";
  switch (err.kind) {
    case "auth": return "bb_auth";
    case "quota": return "bb_quota";
    case "concurrency": return "bb_concurrency";
    case "unavailable": return "bb_unavailable";
    case "timeout": return "bb_timeout";
    case "unreachable": return "bb_unreachable";
    case "too_large": return "too_large";
    case "tls": return "tls_error";
    case "target_timeout": return "timeout";
    default: return `bb_${err.kind}${err.httpStatus ? `_${err.httpStatus}` : ""}`;
  }
}

function wallFromVerdict(verdict, why, status) {
  switch (verdict) {
    case "challenge": return "captcha_unsolved";
    case "login": return "login_required";
    case "geo": return "geo_blocked";
    case "not_found": return `http_${status || 404}`;
    case "blocked": return typeof status === "number" && status >= 400 ? `http_${status}` : "blocked";
    case "http_error": return `http_${status}`;
    case "thin": return why === "empty" ? "empty" : "thin";
    default: return verdict;
  }
}

export function describeWall(wall) {
  if (WALL_TEXT[wall]) return WALL_TEXT[wall];
  const http = /^http_(\d{3})$/.exec(wall);
  if (http) {
    const code = Number(http[1]);
    if (code === 404 || code === 410) return [`The site answered HTTP ${code}: the page does not exist.`, "no", "Check the URL."];
    return [`The site answered HTTP ${code} on every rung.`, code === 429 || code >= 500 ? "later" : "maybe", "Retry later, or open it in the owner's own browser."];
  }
  return [`The ladder ended on "${wall}".`, "maybe", "Retry, or open it in the owner's own browser."];
}

// ---- one Fetch rung (L1 or L2) ----------------------------------------------
async function fetchRung({ rung, url, proxies, timeoutMs, settings, apiKey, extractMode, signal, fetchImpl }) {
  const t0 = Date.now();
  let format = settings.fetchFormat;
  let res;
  let tries = 0;
  for (;;) {
    tries++;
    try {
      recordUsage(settings, { fetchCalls: 1, ...(proxies ? { fetchProxiedCalls: 1 } : {}) });
      res = await bbFetch({ base: settings.apiBaseUrl, apiKey, url, proxies, format, allowInsecureSsl: settings.allowInsecureSsl, timeoutMs, signal, fetchImpl });
      break;
    } catch (err) {
      if (signal?.aborted) throw err;
      if (err?.kind === "format" && format === "markdown") {
        format = "raw"; // markdown not enabled for this project, or not convertible: fall back to raw
        continue;
      }
      if (err?.kind === "concurrency" && tries < 2) {
        await new Promise((r) => setTimeout(r, 1200));
        continue;
      }
      const wall = wallFromBbError(err);
      const fatal = ["bb_auth", "bb_quota", "bb_unreachable"].includes(wall);
      const toL3 = ["too_large", "timeout", "bb_timeout", "bb_unavailable", "bb_concurrency"].includes(wall);
      return { rung, ok: false, wall, outcome: wall, ms: Date.now() - t0, fatal, next: fatal ? "stop" : toL3 ? "L3" : wall === "tls_error" ? "stop" : "L3" };
    }
  }
  if (proxies) recordUsage(settings, { proxyBytesEst: res.responseBytes + 1024 });
  const paid = { billed: true, bytes: res.responseBytes + 1024 }; // a Fetch call Browserbase answered, for the spend row
  const status = res.statusCode;
  const ctype = String(res.contentType || res.headers?.["content-type"] || res.headers?.["Content-Type"] || "").toLowerCase();
  // The Fetch API does not report the post-redirect URL, so finalUrl stays the requested one.
  const finalUrl = url;
  if (res.encoding === "base64") {
    return { rung, ok: false, wall: "binary_content", outcome: `binary:${ctype.split(";")[0] || "unknown"}`, status, ms: Date.now() - t0, next: "stop", ...paid };
  }
  const content = typeof res.content === "string" ? res.content : JSON.stringify(res.content ?? "", null, 2);
  const isHtml = /html|xml/.test(ctype) || (!ctype && /^\s*</.test(content));
  let ex;
  if (format === "markdown" && !/^\s*</.test(content)) ex = extractMarkdown(content, { extractMode });
  else if (isHtml) ex = await extractHtml(content, { url: finalUrl, extractMode });
  else {
    // JSON, plain text, markdown served by the site: pass through as-is.
    const text = collapse(content);
    ex = { title: undefined, content: /json/.test(ctype) ? content : text, visibleText: text, articleChars: text.length, extractor: /json/.test(ctype) ? "json" : "raw", htmlLength: content.length, scriptCount: 0, hasPasswordInput: false, noscriptText: "" };
  }
  const judged = judgePage({ status, html: isHtml ? content : "", finalUrl, ex, minVisibleChars: isHtml || format === "markdown" ? settings.minVisibleChars : 0, hardFloor: isHtml ? settings.minVisibleCharsHard : 0 });
  const ms = Date.now() - t0;
  if (judged.verdict === "ok") return { rung, ok: true, status, finalUrl, ctype, ex, ms, outcome: "ok", ...paid };
  const wall = wallFromVerdict(judged.verdict, judged.why, status);
  let next;
  if (judged.verdict === "not_found") next = "stop";
  else if (judged.verdict === "blocked" || judged.verdict === "challenge" || judged.verdict === "geo") next = rung === "L1" ? "L2" : "L3";
  else if (judged.verdict === "thin") next = rung === "L1" && !(settings.skipL2ForJsShell && judged.why === "js_shell") ? "L2" : "L3";
  else next = "L3"; // login, http_error
  const partial = ex?.content && String(ex.content).trim() && judged.verdict === "thin" ? { status, finalUrl, ctype, ex } : undefined;
  return { rung, ok: false, wall, outcome: `${judged.verdict}${judged.why ? `:${judged.why}` : ""}`, status, ms, next, partial, ...paid };
}

// ---- L3 ---------------------------------------------------------------------
async function sessionRung({ url, hostname, settings, apiKey, projectId, extractMode, budgetMs, signal, fetchImpl, blockPictures = false, proxies }) {
  const t0 = Date.now();
  const run = await runSessionRung({ base: settings.apiBaseUrl, apiKey, projectId, url, hostname, settings, budgetMs, signal, fetchImpl, blockPictures, proxiesOverride: proxies });
  const ms = Date.now() - t0;
  const extra = { ctxNote: run.ctxNote, proxies: run.proxies, proxyRetry: run.proxyRetry, driver: run.page?.driver, pictures: blockPictures ? "off" : "on", settled: run.settled, captchaSeen: Boolean(run.page?.captcha?.started), billed: Boolean(run.sessionId), seconds: run.sessionSeconds || 0, bytes: run.proxyBytesEst || 0 };
  if (run.error) {
    const wall = run.error instanceof BrowserbaseError ? wallFromBbError(run.error) : "nav_error";
    return { rung: "L3", ok: false, wall, outcome: `session:${wall}`, ms, ...extra };
  }
  if (!run.page) return { rung: "L3", ok: false, wall: "nav_error", outcome: `driver:${(run.driveError || "failed").slice(0, 80)}`, ms, ...extra };
  const p = run.page;
  if (p.navError && !p.html) {
    const wall = wallFromNavError(p.navError);
    return { rung: "L3", ok: false, wall, outcome: `nav:${wall}`, status: p.status, ms, ...extra };
  }
  let ex = await extractHtml(p.html || "", { url: p.finalUrl || url, extractMode });
  // Rendered innerText beats a weak Readability pass on app-like pages.
  const inner = collapse(p.innerText || "");
  if (inner.length > (ex.articleChars || 0) * 1.5 && inner.length > 200) {
    ex = { ...ex, content: extractMode === "text" ? inner : inner.split("\n").filter(Boolean).join("\n\n"), articleChars: inner.length, extractor: "innertext" };
  }
  const judged = judgePage({ status: p.status, html: p.html || "", finalUrl: p.finalUrl || url, ex, minVisibleChars: settings.minVisibleChars, hardFloor: settings.minVisibleCharsHard });
  let verdict = judged.verdict;
  // render_empty (2026-09-28): a client-side app that rendered WITHOUT its data leaves a title such as
  // "undefined | <shop name>" and only footer/legal boilerplate as text (a retailer's product pages, 70 KB,
  // zero product words). Boilerplate clears every char floor, so the title is the tell; not a read.
  // v2 (2026-09-28 19:5x): test EVERY title candidate, not the first truthy one. On that retailer the live
  // document.title is "General PDP Template | <shop>" while the extracted <title>/og:title reads
  // "undefined | <shop>"; `p.title || ex.title` picked the former and the 04:12 proof stayed red.
  const titleCandidates = [p.title, ex.title, ...Array.from(String(p.html || "").matchAll(/<title[^>]*>([\s\S]*?)<\/title>/gi), (m) => m[1]), ...Array.from(String(p.html || "").matchAll(/<meta[^>]+(?:property|name)=["']og:title["'][^>]+content=["']([^"']*)["']/gi), (m) => m[1])];
  if (titleCandidates.some((t) => /^\s*(undefined|null|\[object Object\])\b/i.test(String(t || "")) || /^\s*General PDP Template\b/i.test(String(t || "")))) {
    return { rung: "L3", ok: false, wall: "empty", outcome: "thin:render_empty", status: p.status, ms, ...extra };
  }
  if (p.captcha?.started && !p.captcha?.finished) verdict = "challenge";
  if (p.challengeWait?.timedOut && verdict !== "ok") verdict = verdict === "thin" ? "challenge" : verdict;
  // Last rung: a short but real page is still an answer.
  if (verdict === "ok" || (verdict === "thin" && ex.content && String(ex.content).trim())) {
    return { rung: "L3", ok: true, status: p.status, finalUrl: p.finalUrl || url, ctype: "text/html", ex, ms, outcome: verdict === "ok" ? "ok" : "ok:short", short: verdict !== "ok", ...extra };
  }
  const wall = wallFromVerdict(verdict, judged.why, p.status);
  return { rung: "L3", ok: false, wall: wall === "thin" ? "empty" : wall, outcome: `${verdict}${judged.why ? `:${judged.why}` : ""}`, status: p.status, ms, ...extra };
}

// ---- result shapes ------------------------------------------------------------
function trail(attempts) {
  return attempts.map((a) => `${a.rung}:${a.outcome}`).join(",");
}

function rungLabel(a) {
  if (a.rung === "F1") return "F1 local fetch from this machine";
  if (a.rung === "F2") return "F2 local browser on this machine";
  if (a.rung === "F3") return "F3 trawl (Camoufox) on this machine";
  if (a.rung === "L1") return "L1 Browserbase Fetch";
  if (a.rung === "L2") return "L2 Browserbase Fetch + proxies";
  if (a.rung === "L3n") return `L3n browser session without proxies + captcha solver, pictures on${a.ctxNote ? ` (${a.ctxNote})` : ""}`;
  return `L3 browser session${a.proxies ? " + proxies" : ""} + captcha solver${a.pictures === "off" ? ", pictures off" : ""}${a.ctxNote ? ` (${a.ctxNote})` : ""}${a.proxyRetry ? ", retried without proxies" : ""}`;
}

// The Browserbase calls a read paid for, one per billed rung: tools.mjs books each as a spend row.
function paidRungs(attempts) {
  return attempts.filter((a) => a.billed).map((a) => ({ rung: a.rung, proxies: a.rung === "L2" || (/^L3/.test(a.rung) && a.proxies === true), seconds: a.seconds || 0, bytes: a.bytes || 0 }));
}

function successPayload({ url, win, attempts, extractMode, startedAt }) {
  const text = String(win.ex.content ?? "");
  const climbed = attempts.length > 1;
  const warnings = [];
  if (climbed) warnings.push(`fetch-plus read this at ${win.rung} after ${trail(attempts.slice(0, -1))}`);
  if (win.short) warnings.push("fetch-plus: the rendered page is short; it may be partial");
  if (isBookingsPage(url)) warnings.push(`fetch-plus read this Bookings page as a guest, in the anonymous view its share links open, with no sign-in${win.guest === "clicked" ? ", past its Continue as guest button" : ""}`); // the gate in runFetchPlus keeps its choice screen out of here
  return {
    url,
    finalUrl: win.finalUrl || url,
    status: typeof win.status === "number" ? win.status : 200,
    ...(win.ctype ? { contentType: win.ctype.split(";")[0].trim() } : {}),
    ...(win.ex.title ? { title: win.ex.title } : {}),
    extractor: `fetch-plus:${win.rung}`,
    extractMode,
    text,
    rawLength: text.length,
    tookMs: Date.now() - startedAt,
    fetchedAt: new Date().toISOString(),
    ...(warnings.length ? { warning: warnings.join("; ") } : {}),
    paid: paidRungs(attempts),
  };
}

function failurePayload({ url, wall, attempts, extractMode, startedAt, host, partial, hopeless }) {
  const [what, retry, next] = /^login(_required)?$/.test(wall) && isBookingsPage(url) ? BOOKINGS_SIGNIN : describeWall(wall); // a free rung names it login
  const lastStatus = [...attempts].reverse().find((a) => typeof a.status === "number")?.status;
  const lines = [
    `fetch-plus could not read this page. wall=${wall} host=${host} rungs=${trail(attempts) || "none"} retry=${retry}`,
    "",
    `What stopped it: ${what}`,
    `Rungs tried: ${attempts.length ? attempts.map((a) => `${rungLabel(a)} -> ${a.outcome}${typeof a.status === "number" ? ` (HTTP ${a.status})` : ""}, ${a.ms} ms`).join("; ") : "none"}.`,
    `Next move: ${next}`,
  ];
  if (hopeless) lines.push(`Ceiling: two free rungs met the same ${hopeless.kind} wall, so the fetch rungs L1 and L2 were skipped and the call was capped at ${hopeless.seconds} s (hopelessMaxSeconds).`);
  if (partial?.ex?.content) {
    lines.push("", "Partial text from the best rung (short, may be a stub):", "", String(partial.ex.content).slice(0, 4000));
  }
  const text = lines.join("\n");
  return {
    url,
    finalUrl: partial?.finalUrl || url,
    status: typeof lastStatus === "number" ? lastStatus : 0,
    contentType: "text/plain",
    title: `fetch-plus: ${wall}`,
    extractor: `fetch-plus:failed:${wall}`,
    extractMode,
    text: extractMode === "text" ? markdownToText(text) : text,
    rawLength: text.length,
    tookMs: Date.now() - startedAt,
    fetchedAt: new Date().toISOString(),
    warning: `fetch-plus wall=${wall} rungs=${trail(attempts) || "none"}`,
    paid: paidRungs(attempts),
  };
}

function pickWall(attempts) {
  const last = attempts[attempts.length - 1];
  const l3 = attempts.find((a) => a.rung === "L3") || attempts.find((a) => a.rung === "L3n");
  // The ladder stopped because Browserbase refused (auth, quota, unreachable): name that,
  // not the site's wall from an earlier rung, so a billing or key problem is never hidden.
  if (last?.fatal && /^bb_/.test(last.wall)) return last.wall;
  // L3 reached the site: its finding is the most informed one.
  if (l3 && !/^bb_|^nav_error$/.test(l3.wall)) return l3.wall;
  // L3 never started because Browserbase itself failed or refused (outage, concurrency,
  // quota, auth): that is what stopped the ladder. Naming the earlier rung's captcha here
  // would say a solver ran when none did, and would hide a quota or billing problem.
  if (l3 && /^bb_/.test(l3.wall)) return l3.wall;
  const order = ["guest_choice", "login_required", "captcha_unsolved", "geo_blocked", "proxy_restricted", "tls_error", "dns_error", "too_large", "binary_content"];
  for (const w of order) if (attempts.some((a) => a.wall === w)) return w;
  const http = [...attempts].reverse().find((a) => /^http_\d{3}$/.test(a.wall));
  if (http) return http.wall;
  const blocked = [...attempts].reverse().find((a) => a.wall === "blocked");
  if (blocked) return typeof blocked.status === "number" ? `http_${blocked.status}` : "blocked";
  return last?.wall === "thin" ? "empty" : last?.wall || "empty";
}

// A local rung was refused (not merely given a JS shell): this host should rest the home line.
function isLocalBlock(a) {
  return a?.verdict === "challenge" || a?.verdict === "blocked" || a?.status === 429 || a?.status === 403;
}

// ---- entry -----------------------------------------------------------------------
// A challenge's kind, for the hopeless rule: Cloudflare's title, script and Turnstile markers are one wall.
const CHALLENGE_FAMILY = { challenge_title: "cloudflare", turnstile: "cloudflare" };

function challengeKind(a) {
  if (a?.verdict !== "challenge") return null;
  const why = String(a.outcome || "").split(":")[1] || "challenge";
  return CHALLENGE_FAMILY[why] || why;
}

export async function runFetchPlus(args, ctx, executionContext = {}, deps = {}) {
  const startedAt = Date.now();
  const signal = executionContext?.signal;
  signal?.throwIfAborted?.();
  const settings = resolveSettings(ctx?.config, deps.settingsOverrides);
  const clock = () => (typeof settings._now === "function" ? settings._now() : Date.now()); // test seam, as local.js nowOf: budgets and the ledger's ms follow it
  const startedTick = clock();
  const fetchImpl = deps.fetchImpl || fetch;
  const url = bookingsGuestUrl(typeof args?.url === "string" ? args.url.trim() : ""); // a Bookings page as a guest, every rung
  const extractMode = args?.extractMode === "text" ? "text" : "markdown";
  const host = hostOf(url) || "invalid";
  const attempts = [];
  let partial;
  // A Bookings page (need 21): F2 is told to take its guest step, and a rung's read that is still the guest choice
  // screen is no answer; once F2 has clicked, or met that screen, the climb stops: no paid rung clicks.
  const bookings = isBookingsPage(url);
  const guestGate = (r) => {
    if (!bookings || !r) return r;
    const text = r.ex?.content ?? r.partial?.ex?.content;
    if (r.guest === "stuck" || (r.guest !== "clicked" && onGuestChoice(text))) return { ...r, ok: false, wall: "guest_choice", outcome: "guest_choice", next: "stop", partial: r.partial || (String(text ?? "").trim() ? { status: r.status, finalUrl: r.finalUrl, ctype: r.ctype || "text/html", ex: r.ex } : undefined) };
    return r.rung === "F2" && r.guest && !r.ok ? { ...r, next: "stop" } : r;
  };

  let creds = {};
  let memPlan = null; // site memory's plan for this host: { start, bb, probe, remembered }
  let hopeless = null; // the challenge kind two free rungs met (2026-10-21): L1 and L2 skipped, the call capped at hopelessMaxSeconds
  const finish = (result, wall) => {
    appendCallLog(settings, {
      host,
      rung: result.ok ? result.rung : "none",
      outcome: result.ok ? "ok" : "fail",
      ...(wall ? { wall } : {}),
      status: typeof result.status === "number" ? result.status : null,
      ms: clock() - startedTick,
      rungs: trail(attempts),
      ...(memPlan ? { mem: `${memPlan.start}/${memPlan.bb}${memPlan.probe ? "/retry" : ""}` } : {}),
      ...(hopeless ? { hopeless, cap: settings.hopelessMaxSeconds } : {}),
      // The F2 split in ms (local.js phases, 2026-10-21): numbers only, so the next measurement reads off this log.
      ...(attempts.find((a) => a.rung === "F2" && a.phases) ? { f2: attempts.find((a) => a.rung === "F2").phases } : {}),
    });
    rememberRead(settings, host, { win: result.ok ? result.rung : null, plan: memPlan, url });
    if (attempts.some((a) => /^L[12]$/.test(a.rung))) syncProjectUsage(settings, creds, fetchImpl);
  };
  const fail = (wall) => {
    finish({ ok: false, status: attempts.at(-1)?.status }, wall);
    return failurePayload({ url, wall, attempts, extractMode, startedAt, host, partial, hopeless: hopeless ? { kind: hopeless, seconds: settings.hopelessMaxSeconds } : null });
  };

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return fail("invalid_url");
  }
  if (!/^https?:$/.test(parsed.protocol) || (settings.blockPrivateHosts && isPrivateHost(parsed.hostname))) return fail("invalid_url");

  creds = deps.credentials || {}; // the keys, read by tools.mjs from the private folder's secrets
  // The free rungs need no key; a missing key stops the ladder only where Browserbase starts.
  if (!creds.apiKey && !settings.localEnabled) return fail("no_key");

  let total = settings.totalMaxSeconds * 1000;
  const left = () => total - (clock() - startedTick);
  const common = { url, settings, apiKey: creds.apiKey, extractMode, signal, fetchImpl };

  // l3FirstHosts (2026-09-27): sites that serve a JavaScript shell with enough boilerplate to pass every
  // text check (a retailer's product pages: a 70 KB "General PDP Template" whose only text is the
  // footer and the mega-menu) start at the real browser; L1 and L2 cannot render them.
  const hostLc = parsed.hostname.toLowerCase();
  const l3First = settings.l3FirstHosts.some((h) => hostLc === h || hostLc.endsWith("." + h));
  let next = "L3";

  // F1 and F2 (2026-10-19): free, from this machine on its own connection. Skipped for l3FirstHosts,
  // for a host that blocked them inside localCooldownHours, and past the rate cap; then the
  // Browserbase ladder runs as before. After a real local wall the paid ladder starts at L1 (the
  // default, bbFetchAfterLocal true: in October L1 read Etsy and LinkedIn pages the free rungs
  // missed in the replay, and Fetch calls stay inside the included 1,000) or straight at L3.
  // Site memory (phase 2, local.js): a remembered free step (F2, F3) skips the cheaper free steps,
  // and a remembered Browserbase step (L3n, L3) skips L1 and L2 once Browserbase is reached; L3
  // (proxies needed last time) also skips L3n. The home guards still apply on top.
  memPlan = siteMemoryPlan(settings, hostLc, url); // per host + path class since 2026-10-22 (local.js pathClass)
  const memBb = memPlan?.bb === "L3n" || memPlan?.bb === "L3" ? "L3" : "L1";
  const proxiesNeeded = memPlan?.bb === "L3";
  const memSkipF1 = memPlan?.start === "F2" || memPlan?.start === "F3";
  const memSkipF2 = memPlan?.start === "F3";
  let bbFrom = memBb;
  // Hopeless (2026-10-21): two free rungs of THIS call met the same challenge kind (F1 and F2, or F2 and F3).
  // L1 and L2 are fetches: they cannot run a challenge's JavaScript or solve a captcha, so they are skipped,
  // Browserbase starts at the solver rung, and the whole call is capped at hopelessMaxSeconds from its start.
  // The home guards (cooldown, rate cap) are untouched, and a call that starts inside a cooldown keeps the
  // 2026-10-19 ladder from L1 (October's L1 read Etsy and LinkedIn pages the free rungs had missed).
  const capHopeless = (kind) => {
    hopeless = kind;
    bbFrom = "L3";
    total = Math.min(total, settings.hopelessMaxSeconds * 1000);
  };
  const cool = settings.localEnabled && !l3First ? localCooldownFor(settings, hostLc) : null;
  if (cool?.skip === "all") bbFrom = settings.bbFetchAfterLocal ? memBb : "L3";
  const capped = settings.localEnabled && !l3First && cool?.skip !== "all" ? takeLocalSlot(settings, hostLc) : "off";
  if (!capped) {
    let f1;
    if (cool?.skip !== "F1" && cool?.skip !== "F12" && !memSkipF1) {
      f1 = guestGate(await localFetchRung({ url, settings, extractMode, signal, timeoutMs: Math.max(2000, Math.min(settings.localFetchTimeoutMs, left() - 20000)), fetchImpl: deps.localFetchImpl || fetch }));
      attempts.push(f1);
      if (f1.ok) {
        finish(f1);
        return successPayload({ url, win: f1, attempts, extractMode, startedAt });
      }
      partial = f1.partial || partial;
      if (f1.next === "stop") return fail(pickWall(attempts));
    }
    let f2;
    if (f1?.next === "L3") {
      bbFrom = "L3"; // a login wall: the saved login lives in the site's Browserbase context
    } else if (settings.localBrowserEnabled && (deps.localBrowserRung || localBrowserPresent(settings)) && cool?.skip !== "F12" && !memSkipF2 && left() > 20000) {
      f2 = guestGate(await (deps.localBrowserRung || localBrowserRung)({ url, settings, extractMode, signal, budgetMs: Math.min(settings.localBrowserMaxSeconds * 1000, left() - 15000), guest: bookings }));
      attempts.push(f2);
      if (f2.ok) {
        if (f1 && isLocalBlock(f1)) markLocalBlocked(settings, hostLc, f1.outcome, "F1");
        finish(f2);
        return successPayload({ url, win: f2, attempts, extractMode, startedAt });
      }
      partial = f2.partial || partial;
      if (f2.next === "stop") return fail(pickWall(attempts));
      if (!f2.skipped && f2.wall !== "local_launch") bbFrom = settings.bbFetchAfterLocal ? memBb : "L3";
      if (f1 && challengeKind(f1) && challengeKind(f1) === challengeKind(f2)) capHopeless(challengeKind(f2));
    }
    // F3: a challenge the stealth browser did not pass goes to trawl's Camoufox, which solves
    // captchas on this machine. The idle CloakBrowser closes first: one local browser at a time.
    const walled = (f2 && (f2.verdict === "challenge" || f2.verdict === "blocked")) || cool?.skip === "F12" || memSkipF2;
    if (settings.localTrawlEnabled && walled && left() > 25000) {
      if (localBrowserState().active === 0) await closeLocalBrowser();
      const f3 = guestGate(await localTrawlRung({ url, settings, extractMode, signal, budgetMs: Math.min(settings.localTrawlMaxSeconds * 1000, left() - 20000), fetchImpl: deps.trawlFetchImpl || fetch }));
      if (!f3.skipped) attempts.push(f3);
      if (f3.next === "stop") return fail(pickWall(attempts));
      if (f3.ok) {
        if (f2 && isLocalBlock(f2)) markLocalBlocked(settings, hostLc, f2.outcome, "F12");
        finish(f3);
        return successPayload({ url, win: f3, attempts, extractMode, startedAt });
      }
      partial = f3.partial || partial;
      if (!hopeless && !f3.skipped && f2 && challengeKind(f2) && challengeKind(f2) === challengeKind(f3)) capHopeless(challengeKind(f3));
    }
    if (f2 && isLocalBlock(f2)) markLocalBlocked(settings, hostLc, f2.outcome, "all");
  }

  if (!creds.apiKey) return fail("no_key");
  if (!l3First && bbFrom === "L1") {
  // L1
  const r1 = guestGate(await fetchRung({ ...common, rung: "L1", proxies: false, timeoutMs: Math.min(settings.l1TimeoutMs, left()) }));
  attempts.push(r1);
  if (r1.ok) {
    finish(r1);
    return successPayload({ url, win: r1, attempts, extractMode, startedAt });
  }
  partial = r1.partial || partial;
  if (r1.next === "stop") return fail(pickWall(attempts));
  next = r1.next;

  // L2
  if (next === "L2" && settings.l2Proxies && left() > 5000) {
    const r2 = guestGate(await fetchRung({ ...common, rung: "L2", proxies: true, timeoutMs: Math.min(settings.l2TimeoutMs, left() - 2000) }));
    attempts.push(r2);
    if (r2.ok) {
      finish(r2);
      return successPayload({ url, win: r2, attempts, extractMode, startedAt });
    }
    partial = r2.partial || partial;
    if (r2.next === "stop" || r2.fatal) return fail(pickWall(attempts));
    next = "L3";
  } else if (next === "L2") {
    next = "L3";
  }
  }

  // L3, pictures off first; once more with pictures on when that read as a captcha, a challenge or
  // thin (captchas are pictures, and the solver needs them).
  if (next === "L3" && settings.l3Enabled && left() > 8000) {
    // L3n (phase 2): the same browser WITHOUT proxies first, pictures on, since unproxied bytes are
    // not billed and captchas are pictures. M3 (2026-10-19, 10 hosts October sent to L3): 4 read
    // without proxies at 0 bytes; of the 5 misses retried with proxies, 3 read. Skipped when the
    // host's memory says proxies were needed (until its daily retry) or the proxied pass would lose its time.
    if (settings.l3NoProxyFirst && !proxiesNeeded && left() > settings.l3NoProxyReserveMs + 15000) {
      const budgetMs = Math.min(settings.l3NoProxyMaxSeconds * 1000, left() - settings.l3NoProxyReserveMs);
      const r3n = guestGate(await sessionRung({ url, hostname: parsed.hostname, settings, apiKey: creds.apiKey, projectId: creds.projectId, extractMode, budgetMs, signal, fetchImpl, blockPictures: false, proxies: false }));
      r3n.rung = "L3n";
      attempts.push(r3n);
      syncProjectUsage(settings, creds, fetchImpl);
      if (r3n.ok) {
        finish(r3n);
        return successPayload({ url, win: r3n, attempts, extractMode, startedAt });
      }
      // Browserbase itself refused, or the site says the page does not exist: proxies change neither (nor a guest choice).
      if (/^bb_/.test(r3n.wall || "") || /^http_(404|410)$/.test(r3n.wall || "") || r3n.next === "stop" || signal?.aborted) return fail(pickWall(attempts));
    }
    let blockPictures = settings.l3BlockPictures;
    for (let pass = 0; pass < 2; pass++) {
      const budgetMs = Math.min(settings.l3MaxSeconds * 1000, left() - 1000);
      const r3 = guestGate(await sessionRung({ url, hostname: parsed.hostname, settings, apiKey: creds.apiKey, projectId: creds.projectId, extractMode, budgetMs, signal, fetchImpl, blockPictures }));
      attempts.push(r3);
      r3.settled?.then(() => syncProjectUsage(settings, creds, fetchImpl)).catch(() => {});
      if (r3.ok) {
        finish(r3);
        return successPayload({ url, win: r3, attempts, extractMode, startedAt });
      }
      const picturesMayMatter = /^(challenge|thin|blocked)\b/.test(r3.outcome || "") || r3.captchaSeen;
      if (!blockPictures || !picturesMayMatter || r3.next === "stop" || left() <= 20000 || signal?.aborted) break;
      blockPictures = false;
    }
  }
  if (signal?.aborted) signal.throwIfAborted();
  const wall = attempts.length ? pickWall(attempts) : hopeless ? "captcha_unsolved" : "timeout";
  // Under the hopeless cap the site's own wall is the answer, never "timeout".
  if (!hopeless && left() <= 8000 && attempts.every((a) => a.rung !== "L3") && settings.l3Enabled && next === "L3") {
    return fail("timeout");
  }
  return fail(wall);
}
