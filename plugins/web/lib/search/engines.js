// search-plus engines. Each returns { results: [{ title, url, snippet, published?, siteName? }] }
// or throws an EngineError. No keys anywhere except the Exa and paid Parallel paths (header x-api-key).
import { randomUUID } from "node:crypto";
import { clip, normalizePublished, siteNameOf, toHttpUrl, withTimeout } from "./util.js";

// Test hook for the offline harness only (never set by config): engine id -> mock URL.
let testEndpoints = {};
export function setTestEndpoints(o) {
  testEndpoints = { ...(o ?? {}) };
}

export class EngineError extends Error {
  constructor(engine, kind, message, status, retryAfterMs) {
    super(message);
    this.engine = engine;
    this.kind = kind; // "rate_limit" | "http" | "timeout" | "network" | "protocol" | "empty"
    this.status = status;
    this.retryAfterMs = retryAfterMs; // from Retry-After or Firecrawl's retry_after_seconds, when given
  }
}

// Firecrawl's keyless 429 (read live 2026-09-27 09:17): {"success":false,"reason":"credits",
// "retry_after_seconds":61862}, i.e. a daily credit quota, not a burst limit.
function retryAfterMsFrom(response, bodyText) {
  try {
    const j = JSON.parse(bodyText);
    const s = Number(j?.retry_after_seconds ?? j?.retryAfterSeconds);
    if (Number.isFinite(s) && s > 0) return s * 1000;
  } catch {}
  const h = response.headers.get("retry-after");
  if (h) {
    const s = Number(h);
    if (Number.isFinite(s) && s > 0) return s * 1000;
    const t = Date.parse(h);
    if (Number.isFinite(t) && t > Date.now()) return t - Date.now();
  }
  return undefined;
}

const UA_CHROME =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

async function httpCall(engine, url, init, timeoutMs, signal) {
  // An error body is never allowed to carry the paid key into a message, a log line or the model.
  const secret = init?.headers?.["x-api-key"];
  const scrub = (t) => (secret && typeof t === "string" ? t.split(secret).join("<key>") : t);
  let response;
  try {
    response = await fetch(url, { ...init, signal: withTimeout(signal, timeoutMs) });
  } catch (err) {
    if (signal?.aborted) throw err;
    const timedOut = err?.name === "TimeoutError" || err?.name === "AbortError";
    throw new EngineError(engine, timedOut ? "timeout" : "network", `${engine} ${timedOut ? `timed out after ${timeoutMs} ms` : `network error: ${err?.message ?? err}`}`);
  }
  if (response.status === 429) {
    let body = "";
    try {
      body = scrub(await response.text()).slice(0, 2000);
    } catch {}
    const retryAfterMs = retryAfterMsFrom(response, body);
    const reason = /"reason"\s*:\s*"([^"]+)"/.exec(body)?.[1];
    throw new EngineError(engine, "rate_limit", `${engine} rate limited (429${reason ? `, ${reason}` : ""}${retryAfterMs ? `, retry after ${Math.round(retryAfterMs / 1000)} s` : ""})`, 429, retryAfterMs);
  }
  if (!response.ok) {
    let detail = "";
    try {
      detail = scrub(await response.text()).slice(0, 300);
    } catch {}
    throw new EngineError(engine, "http", `${engine} HTTP ${response.status}: ${clip(detail, 200)}`, response.status);
  }
  return response;
}

// ---- freshness / domain mapping ----
const QDR = { day: "qdr:d", week: "qdr:w", month: "qdr:m", year: "qdr:y" };

function mdY(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? "");
  return m ? `${Number(m[2])}/${Number(m[3])}/${m[1]}` : undefined;
}

export function firecrawlTbs(opts) {
  if (opts.freshness && QDR[opts.freshness]) return QDR[opts.freshness];
  const min = mdY(opts.dateAfter);
  const max = mdY(opts.dateBefore);
  if (min || max) return `cdr:1${min ? `,cd_min:${min}` : ""}${max ? `,cd_max:${max}` : ""}`;
  return undefined;
}

export function splitDomains(domainFilter) {
  const include = [];
  const exclude = [];
  for (const raw of domainFilter ?? []) {
    if (typeof raw !== "string") continue;
    const d = raw.trim().toLowerCase();
    if (!d) continue;
    if (d.startsWith("-")) exclude.push(d.slice(1));
    else include.push(d);
  }
  return { include, exclude };
}

// ---- Firecrawl keyless: the same call the installed firecrawl-free provider makes ----
// firecrawl-client-7vlACkO2.js:307-383: POST https://api.firecrawl.dev/v2/search,
// no Authorization header when keyless, body { query, limit } plus optional filters.
// Firecrawl descriptions sometimes carry page markdown: drop images, keep link text, drop heading marks.
export function cleanMarkdown(s) {
  return String(s ?? "")
    .replace(/\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)/g, "$1")
    .replace(/(^|\s)#{1,6}\s+/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function firecrawlItems(payload) {
  const data = payload?.data;
  const candidates = [
    Array.isArray(data) ? data : undefined,
    payload?.results,
    data && !Array.isArray(data) ? data.results : undefined,
    data && !Array.isArray(data) ? data.data : undefined,
    data && !Array.isArray(data) ? data.web : undefined,
    payload?.web?.results,
  ];
  return candidates.find((c) => Array.isArray(c)) ?? [];
}

export async function searchFirecrawl(opts) {
  const body = { query: opts.query, limit: opts.count };
  const tbs = firecrawlTbs(opts);
  if (tbs) body.tbs = tbs;
  const { include, exclude } = splitDomains(opts.domainFilter);
  if (include.length) body.includeDomains = include;
  else if (exclude.length) body.excludeDomains = exclude;
  if (opts.country) body.country = opts.country;
  const response = await httpCall(
    "firecrawl",
    testEndpoints.firecrawl ?? "https://api.firecrawl.dev/v2/search",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    opts.timeoutMs,
    opts.signal,
  );
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new EngineError("firecrawl", "protocol", "firecrawl returned non-JSON");
  }
  if (payload?.success === false) {
    const msg = typeof payload.error === "string" ? payload.error : typeof payload.message === "string" ? payload.message : "unknown error";
    const kind = /rate limit|too many/i.test(msg) || payload.reason === "credits" ? "rate_limit" : "http";
    const s = Number(payload.retry_after_seconds);
    throw new EngineError("firecrawl", kind, `firecrawl error: ${clip(msg, 200)}`, undefined, Number.isFinite(s) && s > 0 ? s * 1000 : undefined);
  }
  const results = [];
  for (const item of firecrawlItems(payload)) {
    if (!item || typeof item !== "object") continue;
    const meta = item.metadata && typeof item.metadata === "object" ? item.metadata : {};
    const url = toHttpUrl(item.url || item.sourceURL || item.sourceUrl || meta.sourceURL || "");
    if (!url) continue;
    results.push({
      title: String(item.title || meta.title || ""),
      url,
      snippet: clip(cleanMarkdown(item.description || item.snippet || item.summary || ""), 1000),
      published: normalizePublished(item.publishedDate || item.published || meta.publishedTime || meta.publishedDate),
      siteName: siteNameOf(url),
    });
    if (results.length >= opts.count) break;
  }
  return { results };
}

// ---- Parallel free Search MCP (keyless). One stateless JSON-RPC tools/call. ----
const PARALLEL_SESSION_ID = `search-plus-${randomUUID()}`;
let rpcId = 0;

function parallelObjective(opts) {
  const parts = [opts.query];
  if (opts.freshness === "day") parts.push("Prefer sources published in the last 24 hours.");
  else if (opts.freshness === "week") parts.push("Prefer sources published in the last 7 days.");
  else if (opts.freshness === "month") parts.push("Prefer sources published in the last month.");
  else if (opts.freshness === "year") parts.push("Prefer sources published in the last year.");
  if (opts.dateAfter) parts.push(`Only sources published after ${opts.dateAfter}.`);
  if (opts.dateBefore) parts.push(`Only sources published before ${opts.dateBefore}.`);
  const { include, exclude } = splitDomains(opts.domainFilter);
  if (include.length) parts.push(`Prefer results from ${include.join(", ")}.`);
  if (exclude.length) parts.push(`Avoid results from ${exclude.join(", ")}.`);
  return parts.join(" ").slice(0, 5000);
}

async function readRpcPayload(engine, response, id) {
  const ctype = response.headers.get("content-type") || "";
  const text = await response.text();
  if (ctype.includes("text/event-stream")) {
    let last;
    for (const line of text.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      try {
        const obj = JSON.parse(line.slice(5).trim());
        if (obj && obj.id === id) last = obj;
      } catch {}
    }
    if (!last) throw new EngineError(engine, "protocol", `${engine}: no JSON-RPC reply in event stream`);
    return last;
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new EngineError(engine, "protocol", `${engine}: non-JSON reply`);
  }
}

function mapParallelResults(list, count, snippetChars) {
  const results = [];
  for (const r of Array.isArray(list) ? list : []) {
    const url = toHttpUrl(r?.url);
    if (!url) continue;
    const excerpts = Array.isArray(r.excerpts) ? r.excerpts.filter((e) => typeof e === "string") : [];
    results.push({
      // title is nullable in Parallel's schema (V1WebSearchResult); the hostname reads better than "".
      title: String(r.title || "").replace(/\s+/g, " ").trim() || siteNameOf(url) || url,
      url,
      snippet: clip(excerpts.join(" "), snippetChars),
      published: normalizePublished(r.publish_date),
      siteName: siteNameOf(url),
    });
    if (results.length >= count) break;
  }
  return results;
}

export async function searchParallelFree(opts) {
  const id = ++rpcId;
  const body = {
    jsonrpc: "2.0",
    id,
    method: "tools/call",
    params: {
      name: "web_search",
      arguments: {
        objective: parallelObjective(opts),
        search_queries: [opts.query.slice(0, 200)],
        session_id: PARALLEL_SESSION_ID,
      },
    },
  };
  const response = await httpCall(
    "parallel",
    testEndpoints.parallel ?? "https://search.parallel.ai/mcp",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
      },
      body: JSON.stringify(body),
    },
    opts.timeoutMs,
    opts.signal,
  );
  const rpc = await readRpcPayload("parallel", response, id);
  if (rpc?.error) {
    const msg = typeof rpc.error.message === "string" ? rpc.error.message : JSON.stringify(rpc.error);
    const kind = /rate|limit|too many|429/i.test(msg) ? "rate_limit" : "protocol";
    throw new EngineError("parallel", kind, `parallel MCP error: ${clip(msg, 200)}`);
  }
  const result = rpc?.result ?? {};
  let structured = result.structuredContent;
  if (!structured && Array.isArray(result.content)) {
    const text = result.content.find((c) => c?.type === "text")?.text;
    try {
      structured = JSON.parse(text);
    } catch {}
  }
  if (result.isError) {
    const text = Array.isArray(result.content) ? result.content.map((c) => c?.text || "").join(" ") : "";
    const kind = /rate|limit|too many|429/i.test(text) ? "rate_limit" : "protocol";
    throw new EngineError("parallel", kind, `parallel tool error: ${clip(text, 200)}`);
  }
  return { results: mapParallelResults(structured?.results, opts.count, opts.snippetChars) };
}

// ---- DuckDuckGo HTML (keyless). Same request as @openclaw/duckduckgo-plugin 2026.9.6
// ddg-client-D9UL3Bem.mjs:92-153: GET html.duckduckgo.com/html?q=&kp=-1, desktop Chrome UA.
const ENTITY = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", ndash: "-", mdash: "--", hellip: "..." };
function decodeEntities(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === "#") {
      const code = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return ENTITY[e.toLowerCase()] ?? m;
  });
}
function stripHtml(html) {
  return html.replace(/<\/?b\b[^>]*>/gi, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}
function decodeDdgUrl(raw) {
  try {
    const u = new URL(raw.startsWith("//") ? `https:${raw}` : raw);
    return u.searchParams.get("uddg") || raw;
  } catch {
    return raw;
  }
}

export function parseDuckDuckGoHtml(html, count) {
  const out = [];
  const resultRe = /<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")([^>]*)>([\s\S]*?)<\/a>/gi;
  const nextRe = /<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")[^>]*>/i;
  const snippetRe = /<a\b(?=[^>]*\bclass="[^"]*\bresult__snippet\b[^"]*")[^>]*>([\s\S]*?)<\/a>/i;
  for (const m of html.matchAll(resultRe)) {
    const href = /\bhref="([^"]*)"/i.exec(m[1] ?? "")?.[1] ?? "";
    const tail = html.slice((m.index ?? 0) + m[0].length);
    const nextAt = tail.search(nextRe);
    const scoped = nextAt >= 0 ? tail.slice(0, nextAt) : tail;
    const title = decodeEntities(stripHtml(m[2] ?? ""));
    const url = toHttpUrl(decodeDdgUrl(decodeEntities(href)));
    const snippet = decodeEntities(stripHtml(snippetRe.exec(scoped)?.[1] ?? ""));
    if (title && url) {
      out.push({ title, url, snippet: clip(snippet, 1000), siteName: siteNameOf(url) });
      if (out.length >= count) break;
    }
  }
  return out;
}

export async function searchDuckDuckGo(opts) {
  const url = new URL(testEndpoints.duckduckgo ?? "https://html.duckduckgo.com/html");
  url.searchParams.set("q", opts.query);
  url.searchParams.set("kp", "-1");
  if (opts.freshness && { day: "d", week: "w", month: "m", year: "y" }[opts.freshness]) {
    url.searchParams.set("df", { day: "d", week: "w", month: "m", year: "y" }[opts.freshness]);
  }
  const response = await httpCall(
    "duckduckgo",
    url.toString(),
    { method: "GET", headers: { "User-Agent": UA_CHROME } },
    opts.timeoutMs,
    opts.signal,
  );
  const html = await response.text();
  if (!/class="[^"]*\bresult__a\b/i.test(html) && /g-recaptcha|are you a human|id="challenge-form"|name="challenge"/i.test(html)) {
    throw new EngineError("duckduckgo", "rate_limit", "duckduckgo bot challenge", undefined, 30 * 60000);
  }
  return { results: parseDuckDuckGoHtml(html, opts.count) };
}

// ---- Parallel paid Search API: engine ONE whenever a key is configured ----
// Re-read 2026-09-27 20:5x EDT from docs.parallel.ai (api-reference/search/search.md, the OpenAPI
// V1SearchRequest / V1SearchResponse; search/modes.md; search/advanced-search-settings.md):
//   POST https://api.parallel.ai/v1/search, header x-api-key, additionalProperties false.
//   body: objective, search_queries (required), mode turbo|fast|basic|advanced (omitted = advanced,
//   $5 per 1,000; fast and turbo are $1 per 1,000), max_chars_total, advanced_settings { source_policy
//   { include_domains, exclude_domains (ignored when include is set), after_date YYYY-MM-DD },
//   location (ISO alpha-2, lowercase), max_results (default 10, capped at 20) }.
//   200: { search_id, results: [{ url, title?, publish_date? YYYY-MM-DD, excerpts: [] }], warnings?, usage?, session_id }.
//   errors: { error: { message, detail } } with 401 bad key, 402 no credit, 422 validation, 429 rate limit.
// Unlike the free MCP, the paid API takes domains and a start date as REAL filters.
const PAID_URL = "https://api.parallel.ai/v1/search";
const FRESHNESS_DAYS = { day: 1, week: 7, month: 31, year: 366 };

function isoDaysAgo(days, now = new Date()) {
  return new Date(now.getTime() - days * 86400000).toISOString().slice(0, 10);
}

export function paidAfterDate(opts, now = new Date()) {
  const fromFreshness = opts.freshness && FRESHNESS_DAYS[opts.freshness] ? isoDaysAgo(FRESHNESS_DAYS[opts.freshness], now) : undefined;
  const explicit = /^\d{4}-\d{2}-\d{2}$/.test(opts.dateAfter ?? "") ? opts.dateAfter : undefined;
  if (fromFreshness && explicit) return fromFreshness > explicit ? fromFreshness : explicit; // the later date is the tighter filter
  return fromFreshness ?? explicit;
}

// Source-policy entries: plain domains, domain/path prefixes, or bare extensions (.gov). No schemes,
// ports, queries or fragments. Path prefixes are not supported in turbo mode, so a path entry is
// dropped there (cutting it to the bare domain would widen an exclude past what was asked).
export function paidDomainEntry(raw, mode) {
  const d = String(raw ?? "").trim().toLowerCase().replace(/^[a-z]+:\/\//, "").replace(/^\*\./, "").replace(/[?#].*$/, "").replace(/\/+$/, "");
  if (mode === "turbo" && d.includes("/")) return undefined;
  if (/^\.[a-z0-9-]+(\.[a-z0-9-]+)*$/.test(d)) return d;
  if (/^[a-z0-9-]+(\.[a-z0-9-]+)+(\/[^\s]*)?$/.test(d)) return d;
  return undefined;
}

export function buildParallelPaidBody(opts, now = new Date()) {
  const mode = opts.mode || "fast";
  const body = {
    objective: parallelObjective(opts),
    search_queries: [opts.query.slice(0, 200)],
    mode,
    max_chars_total: Math.max(2000, opts.count * opts.snippetChars),
    advanced_settings: { max_results: Math.min(20, Math.max(1, opts.count)) },
  };
  const { include, exclude } = splitDomains(opts.domainFilter);
  const inc = [...new Set(include.map((d) => paidDomainEntry(d, mode)).filter(Boolean))].slice(0, 200);
  const exc = [...new Set(exclude.map((d) => paidDomainEntry(d, mode)).filter(Boolean))].slice(0, 200);
  const sourcePolicy = {};
  if (inc.length) sourcePolicy.include_domains = inc;
  else if (exc.length) sourcePolicy.exclude_domains = exc;
  const after = paidAfterDate(opts, now);
  if (after) sourcePolicy.after_date = after;
  if (Object.keys(sourcePolicy).length) body.advanced_settings.source_policy = sourcePolicy;
  if (opts.country) body.advanced_settings.location = opts.country.toLowerCase();
  return body;
}

export async function searchParallelPaid(opts) {
  const body = buildParallelPaidBody(opts);
  const response = await httpCall(
    "parallel-paid",
    testEndpoints["parallel-paid"] ?? PAID_URL,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": opts.apiKey },
      body: JSON.stringify(body),
    },
    opts.timeoutMs,
    opts.signal,
  );
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new EngineError("parallel-paid", "protocol", "parallel-paid returned non-JSON");
  }
  if (!Array.isArray(payload?.results)) throw new EngineError("parallel-paid", "protocol", "parallel-paid reply has no results array");
  return { results: mapParallelResults(payload.results, opts.count, opts.snippetChars) };
}

// ---- Exa: engine ONE by default since 2026-10-19 (keyed; skipped without a key) ----
// The call a quality shoot-out of search engines ranked first (2026-10-19): POST https://api.exa.ai/search, header x-api-key, body { query, type "auto",
// numResults, contents { highlights: true } }. Filters re-read 2026-10-19 from exa.ai/docs
// (reference/search, the OpenAPI SearchRequest): includeDomains / excludeDomains (a hostname, a
// hostname with a path prefix, or *.wildcard), startPublishedDate / endPublishedDate (ISO 8601
// date-time), userLocation (ISO 3166-1 alpha-2). Exa has no freshness bucket, so freshness becomes
// startPublishedDate (the later of it and date_after, as for paid Parallel); pages Exa holds no
// publish date for then drop out. 200: { requestId, results: [{ title, url, publishedDate, author,
// highlights[], summary?, text? }], costDollars { total }, searchTime }. Errors: { requestId, error,
// tag } with 400 validation, 401 bad key, 402 out of credits or a key budget reached, 403 team
// blocked, 429 over the team's 10 a second (Retry-After when present; admin/billing#rate-limits),
// 503 over capacity. $7 per 1,000 auto searches, $10 free a month.
const EXA_URL = "https://api.exa.ai/search";

// Plain hostnames, hostname/path prefixes and *.wildcards pass as given (Exa takes all three); scheme,
// query, fragment and trailing slash are stripped; anything else is dropped. A bare extension (.gov)
// is not a form Exa documents: it is sent as given, and if Exa refuses it (400) the next engine runs.
export function exaDomainEntry(raw) {
  const d = String(raw ?? "").trim().toLowerCase().replace(/^[a-z]+:\/\//, "").replace(/[?#].*$/, "").replace(/\/+$/, "");
  if (/^(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(\/[^\s]*)?$/.test(d)) return d;
  if (/^\.[a-z0-9-]+(\.[a-z0-9-]+)*$/.test(d)) return d;
  return undefined;
}

export function buildExaBody(opts, now = new Date()) {
  const body = { query: opts.query, type: "auto", numResults: Math.min(100, Math.max(1, opts.count)), contents: { highlights: true } };
  const { include, exclude } = splitDomains(opts.domainFilter);
  const inc = [...new Set(include.map(exaDomainEntry).filter(Boolean))].slice(0, 1200);
  const exc = [...new Set(exclude.map(exaDomainEntry).filter(Boolean))].slice(0, 1200);
  if (inc.length) body.includeDomains = inc;
  else if (exc.length) body.excludeDomains = exc;
  const after = paidAfterDate(opts, now);
  if (after) body.startPublishedDate = `${after}T00:00:00.000Z`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(opts.dateBefore ?? "")) body.endPublishedDate = `${opts.dateBefore}T00:00:00.000Z`;
  if (opts.country) body.userLocation = opts.country.toUpperCase();
  return body;
}

// The shoot-out's parsing: the highlights joined, else the summary, else the first 600 characters of text.
export function mapExaResults(list, count, snippetChars) {
  const results = [];
  for (const r of Array.isArray(list) ? list : []) {
    const url = toHttpUrl(r?.url);
    if (!url) continue;
    const highlights = Array.isArray(r.highlights) ? r.highlights.filter((h) => typeof h === "string") : [];
    const text = highlights.join(" ") || (typeof r.summary === "string" ? r.summary : "") || (typeof r.text === "string" ? r.text.slice(0, 600) : "");
    results.push({
      title: String(r.title || "").replace(/\s+/g, " ").trim() || siteNameOf(url) || url,
      url,
      snippet: clip(cleanMarkdown(text), snippetChars),
      published: normalizePublished(r.publishedDate),
      siteName: siteNameOf(url),
    });
    if (results.length >= count) break;
  }
  return results;
}

export async function searchExa(opts) {
  const response = await httpCall(
    "exa",
    testEndpoints.exa ?? EXA_URL,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": opts.apiKey },
      body: JSON.stringify(buildExaBody(opts)),
    },
    opts.timeoutMs,
    opts.signal,
  );
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new EngineError("exa", "protocol", "exa returned non-JSON");
  }
  if (!Array.isArray(payload?.results)) throw new EngineError("exa", "protocol", "exa reply has no results array");
  const cost = Number(payload?.costDollars?.total);
  return { results: mapExaResults(payload.results, opts.count, opts.snippetChars), ...(Number.isFinite(cost) ? { costDollars: cost } : {}) };
}

// ---- You.com keyless MCP (free profile, 100 searches a day): the shoot-out's adapter ----
// One stateless JSON-RPC tools/call of you-search at
// https://api.you.com/mcp?profile=free, no key, no initialize. you-search's schema, read live
// 2026-10-19 (tools/list): query (at most 400 characters), count, freshness (day, week, month, year or
// "YYYY-MM-DDtoYYYY-MM-DD"; it can only widen a timeframe the query itself names), exclude_domains;
// host-only overrides under params._meta["com.you.com/search"]: include_domains and country (36 listed
// codes; others are not sent). Reply, usually an event stream: structuredContent (or the same JSON as
// text) { results: { web: [{ url, title, description, snippets[], page_age }], news: [{ url, title,
// description, page_age }] } }. Path-prefix domain entries are dropped (hostnames only, as paid turbo).
const YOU_URL = "https://api.you.com/mcp?profile=free";
const YOU_COUNTRIES = new Set("AR AU AT BE BR CA CL DK FI FR DE HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA ES SE CH TW TR GB US".split(" "));
const YOU_LIMIT_RE = /rate|limit|quota|too many|429|daily|exceeded/i;

export function youFreshness(opts, now = new Date()) {
  const before = /^\d{4}-\d{2}-\d{2}$/.test(opts.dateBefore ?? "") ? opts.dateBefore : undefined;
  const explicitAfter = /^\d{4}-\d{2}-\d{2}$/.test(opts.dateAfter ?? "");
  if (!before && !explicitAfter) return opts.freshness && FRESHNESS_DAYS[opts.freshness] ? opts.freshness : undefined;
  return `${paidAfterDate(opts, now) ?? "1970-01-01"}to${before ?? now.toISOString().slice(0, 10)}`;
}

export function buildYouCall(opts, id, now = new Date()) {
  const args = { query: opts.query.slice(0, 400), count: Math.min(100, Math.max(1, opts.count)) };
  const freshness = youFreshness(opts, now);
  if (freshness) args.freshness = freshness;
  const meta = {};
  const { include, exclude } = splitDomains(opts.domainFilter);
  const inc = [...new Set(include.map((d) => paidDomainEntry(d, "turbo")).filter(Boolean))].slice(0, 500);
  const exc = [...new Set(exclude.map((d) => paidDomainEntry(d, "turbo")).filter(Boolean))].slice(0, 500);
  if (inc.length) meta.include_domains = inc;
  else if (exc.length) args.exclude_domains = exc;
  if (opts.country && YOU_COUNTRIES.has(opts.country.toUpperCase())) meta.country = opts.country.toUpperCase();
  const params = { name: "you-search", arguments: args };
  if (Object.keys(meta).length) params._meta = { "com.you.com/search": meta };
  return { jsonrpc: "2.0", id, method: "tools/call", params };
}

// Web rows first, then news, de-duplicated by URL (the shoot-out's walk took them in that order); the
// snippet is the description followed by the query-relevant snippets.
export function mapYouResults(data, count, snippetChars, text = "") {
  const sections = data?.results && typeof data.results === "object" ? data.results : data ?? {};
  const rows = [...(Array.isArray(sections.web) ? sections.web : []), ...(Array.isArray(sections.news) ? sections.news : [])];
  const results = [];
  const seen = new Set();
  for (const r of rows) {
    const url = toHttpUrl(r?.url);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    const snippets = Array.isArray(r.snippets) ? r.snippets.filter((s) => typeof s === "string") : [];
    results.push({
      title: String(r.title || "").replace(/\s+/g, " ").trim() || siteNameOf(url) || url,
      url,
      snippet: clip(cleanMarkdown([typeof r.description === "string" ? r.description : "", ...snippets].join(" ")), snippetChars),
      published: normalizePublished(r.page_age),
      siteName: siteNameOf(url),
    });
    if (results.length >= count) break;
  }
  if (!results.length && text) {
    // The shoot-out's fallback for a markdown reply: "Title: ... URL: ..." pairs.
    for (const m of text.matchAll(/(?:Title:\s*(.+?)\s+)?URL:\s*(https?:\/\/\S+)/g)) {
      const url = toHttpUrl(m[2]);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      results.push({ title: String(m[1] || "").trim() || siteNameOf(url) || url, url, siteName: siteNameOf(url) });
      if (results.length >= count) break;
    }
  }
  return results;
}

export async function searchYouFree(opts) {
  const id = ++rpcId;
  const response = await httpCall(
    "you",
    testEndpoints.you ?? YOU_URL,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
      },
      body: JSON.stringify(buildYouCall(opts, id)),
    },
    opts.timeoutMs,
    opts.signal,
  );
  const rpc = await readRpcPayload("you", response, id);
  if (rpc?.error) {
    const msg = typeof rpc.error.message === "string" ? rpc.error.message : JSON.stringify(rpc.error);
    throw new EngineError("you", YOU_LIMIT_RE.test(msg) ? "rate_limit" : "protocol", `you MCP error: ${clip(msg, 200)}`);
  }
  const result = rpc?.result ?? {};
  const text = Array.isArray(result.content) ? result.content.filter((c) => c?.type === "text").map((c) => c.text || "").join(" ") : "";
  if (result.isError) throw new EngineError("you", YOU_LIMIT_RE.test(text) ? "rate_limit" : "protocol", `you tool error: ${clip(text, 200)}`);
  let data = result.structuredContent;
  if (!data && text) {
    try {
      data = JSON.parse(text);
    } catch {}
  }
  return { results: mapYouResults(data, opts.count, opts.snippetChars, data ? "" : text) };
}

// Every engine by id; searchOrder (util.js) decides which run and in what order. exa and
// parallel-paid run only with a key.
export const ENGINE_FUNCS = {
  exa: searchExa,
  "parallel-paid": searchParallelPaid,
  you: searchYouFree,
  parallel: searchParallelFree,
  firecrawl: searchFirecrawl,
  duckduckgo: searchDuckDuckGo,
};
