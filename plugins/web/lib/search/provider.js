// One web_search call = engine chain + (when the rule fires) a YouTube leg with transcript excerpts.
import path from "node:path";
import { takeWindow } from "../common.mjs";
import { ENGINE_FUNCS, EngineError } from "./engines.js";
import {
  PROVIDER_ID,
  appendCallLog,
  cacheGet,
  cacheKeyFor,
  cacheSet,
  cacheTtlMs,
  clip,
  normalizePublished,
  normalizeQuery,
  countExaCall,
  countPaidCall,
  queryHash,
  resolvePluginSettings,
  takeYouSlot,
  toHttpUrl,
} from "./util.js";
import {
  countInWindow,
  excerptBlock,
  freshnessSignal,
  isYouTubeVideoUrl,
  pickVideos,
  resolveYtTranscript,
  videoIntent,
  videoMetaLine,
  youTubeId,
  ytSearch,
  ytTranscript,
} from "./video.js";

const MAX_ROWS = 10; // web_search returns at most 10 rows
const OUTPUT_BUDGET = 19000; // and keeps the whole answer under 20,000 characters

const FRESHNESS_ALIASES = {
  day: "day", d: "day", pd: "day", "24h": "day", past_day: "day", today: "day",
  week: "week", w: "week", pw: "week", "7d": "week", past_week: "week",
  month: "month", m: "month", pm: "month", "30d": "month", past_month: "month",
  year: "year", y: "year", py: "year", past_year: "year",
};

function readArgs(args) {
  const query = typeof args?.query === "string" ? args.query.trim() : "";
  const rawCount = Number(args?.count);
  const count = Number.isFinite(rawCount) ? Math.min(10, Math.max(1, Math.floor(rawCount))) : 5;
  const freshness = typeof args?.freshness === "string" ? FRESHNESS_ALIASES[args.freshness.trim().toLowerCase()] : undefined;
  const iso = (v) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v.trim()) ? v.trim() : undefined);
  const domainFilter = Array.isArray(args?.domain_filter)
    ? args.domain_filter.filter((d) => typeof d === "string" && d.trim()).map((d) => d.trim()).slice(0, 20)
    : [];
  const country = typeof args?.country === "string" && /^[a-z]{2}$/i.test(args.country.trim()) ? args.country.trim().toUpperCase() : undefined;
  return { query, count, freshness, dateAfter: iso(args?.date_after), dateBefore: iso(args?.date_before), domainFilter, country };
}

// Per-engine cooldown after an explicit rate limit, so an engine that is out of quota is skipped
// instead of being hit on every call: the server's retry-after when it gives one, else a default.
const COOLDOWN_UNTIL = new Map();
// Exa's limit is 10 a second for the whole team (exa.ai/docs, admin/billing#rate-limits), so a 429
// without Retry-After benches it 10 s; You.com's free profile is a daily quota, so it sits out an hour.
const DEFAULT_COOLDOWN_MS = { firecrawl: 10 * 60000, parallel: 60000, duckduckgo: 30 * 60000, "parallel-paid": 60000, exa: 10000, you: 60 * 60000 };
// A keyed call refused for its key or its credit (401 bad key, 402 no credit or a key budget, 403) is
// not retried on every search: that engine sits out 5 minutes while the next ones answer, then is tried again.
const PAID_ACCOUNT_COOLDOWN_MS = 5 * 60000;
// The keyed engines: the settings fields holding each one's key and where it came from.
const KEYED = { exa: ["exaApiKey", "exaKeySource"], "parallel-paid": ["parallelApiKey", "parallelKeySource"] };
const MAX_COOLDOWN_MS = 24 * 3600000;
// Paces paid Parallel under its 600-a-minute limit, in one window every room's process books
// (<stateDir>/paid-rate.json, a worker's counted with the shared one); a full window falls through to the free engines.
export function resetCooldowns() {
  COOLDOWN_UNTIL.clear();
}
function noteFailure(engine, err) {
  if (!(err instanceof EngineError)) return;
  let ms;
  if (err.kind === "rate_limit") ms = err.retryAfterMs ?? DEFAULT_COOLDOWN_MS[engine] ?? 60000;
  else if (KEYED[engine] && [401, 402, 403].includes(err.status)) ms = PAID_ACCOUNT_COOLDOWN_MS;
  else return;
  COOLDOWN_UNTIL.set(engine, Date.now() + Math.min(MAX_COOLDOWN_MS, ms));
}

// Exa in searchOrder with no key: one stderr line per process and key state (stderr, never stdout:
// the web-ladder MCP server runs this code and its stdout is the MCP protocol). Each call's log line
// also carries the skip as an attempt, { engine: "exa", error: "no_key", key_source }.
const NO_KEY_NOTED = new Set();
function noteNoKey(engine, keySource) {
  const k = `${engine}:${keySource}`;
  if (NO_KEY_NOTED.has(k)) return;
  NO_KEY_NOTED.add(k);
  try {
    process.stderr.write(`yerk-web: ${engine} skipped, no key (key_source ${keySource}); the next engine in searchOrder answers\n`);
  } catch {}
}

// searchOrder, default exa, parallel-paid, you, parallel: the next engine runs on an error, a rate
// limit, a cooldown or zero results. A keyed engine without a key is skipped with no request. Exa and
// paid Parallel are never refused for budget (their monthly counters only write alert lines);
// You.com is skipped once the local day's youDailyCap is spent.
async function runEngineChain(a, settings, signal) {
  const attempts = [];
  const keys = [settings.exaApiKey, settings.parallelApiKey].filter(Boolean);
  const scrubKey = (t) => (typeof t === "string" ? keys.reduce((s, k) => s.split(k).join("<key>"), t) : t);
  for (const engine of settings.searchOrder) {
    const fn = ENGINE_FUNCS[engine];
    if (!fn) continue;
    const keyed = KEYED[engine];
    const apiKey = keyed ? settings[keyed[0]] : undefined;
    if (keyed && !apiKey) {
      attempts.push({ engine, ms: 0, error: "no_key", key_source: settings[keyed[1]] });
      if (engine === "exa") noteNoKey(engine, settings[keyed[1]]);
      continue;
    }
    const until = COOLDOWN_UNTIL.get(engine);
    if (until && until > Date.now()) {
      attempts.push({ engine, ms: 0, error: "cooldown", until: new Date(until).toISOString() });
      continue;
    }
    const also = settings.sharedStateDir ? [path.join(settings.sharedStateDir, "paid-rate.json")] : [];
    if (engine === "parallel-paid" && takeWindow(path.join(settings.stateDir, "paid-rate.json"), { perMinute: settings.paidRatePerMinute, also })) {
      attempts.push({ engine, ms: 0, error: "local_rate_limit" });
      continue;
    }
    // Counted before the request, so a crash over-counts and never under-counts.
    let counted = {};
    if (engine === "you") {
      const slot = takeYouSlot(settings);
      if (!slot.ok) {
        attempts.push({ engine, ms: 0, error: "daily_cap", day_count: slot.count });
        continue;
      }
      counted = { day_count: slot.count };
    }
    const opts = { ...a, timeoutMs: settings.engineTimeoutMs, snippetChars: settings.fallbackSnippetChars, signal };
    if (engine === "parallel-paid") {
      opts.apiKey = apiKey;
      opts.mode = settings.paidMode;
      counted = { month_count: countPaidCall(settings) };
    } else if (engine === "exa") {
      opts.apiKey = apiKey;
      counted = { month_count: countExaCall(settings) };
    }
    const t0 = Date.now();
    try {
      const r = await fn(opts);
      const ms = Date.now() - t0;
      const cost = typeof r.costDollars === "number" ? { cost_usd: r.costDollars } : {};
      if (r.results.length > 0) {
        attempts.push({ engine, ms, n: r.results.length, ...counted, ...cost });
        return { engine, results: r.results, attempts };
      }
      attempts.push({ engine, ms, n: 0, error: "empty", ...counted, ...cost });
    } catch (err) {
      if (signal?.aborted) throw err;
      noteFailure(engine, err);
      attempts.push({
        engine,
        ms: Date.now() - t0,
        error: err instanceof EngineError ? err.kind : "error",
        ...(err instanceof EngineError && err.status ? { status: err.status } : {}),
        message: clip(scrubKey(String(err?.message ?? err)), 160),
        ...counted,
      });
    }
  }
  return { engine: null, results: [], attempts, allRefused: attempts.length > 0 && attempts.every((x) => x.error && x.error !== "empty") };
}

// Rough size of the answer against its 20,000-character budget.
function outputSize(rows) {
  return rows.reduce((n, r) => n + r.url.length + (r.title?.length ?? 0) + (r.snippet?.length ?? 0) + (r.siteName?.length ?? 0), 0);
}

export async function runSearchPlus(args, ctx, execCtx) {
  const started = Date.now();
  const signal = execCtx?.signal;
  signal?.throwIfAborted?.();
  const settings = resolvePluginSettings(ctx?.config);
  const a = readArgs(args);
  const log = {
    ts: new Date().toISOString(),
    qhash: queryHash(a.query),
    engine: null,
    attempts: [],
    latency_ms: 0,
    result_count: 0,
    youtube_links: 0,
    leg: { fired: false, rules: [], searched: false, leg_items: 0, leg_ms: null, leg_error: null },
    transcripts: { attempted: 0, attached: 0, codes: [], ms: null, wall_hit: false },
    cached: false,
    key_source: settings.parallelKeySource,
    exa_key_source: settings.exaKeySource,
    error: null,
  };
  const finish = (payload) => {
    log.latency_ms = Date.now() - started;
    // The log keeps only the query hash: scrub the query text out of any engine or child error text.
    if (a.query) {
      const scrub = (t) => (typeof t === "string" ? t.split(a.query).join("<query>") : t);
      for (const x of log.attempts) if (x.message) x.message = scrub(x.message);
      log.leg.leg_error = scrub(log.leg.leg_error);
    }
    if (!payload.error) {
      log.result_count = payload.results.length;
      log.youtube_links = payload.results.filter((r) => isYouTubeVideoUrl(r.url)).length;
    }
    appendCallLog(settings, log);
    // The engines this call tried (a cached answer tried none): tools.mjs names them and books the paid ones.
    return { ...payload, engine: log.engine, attempts: log.attempts };
  };

  if (!a.query) {
    log.error = "invalid_query";
    return finish({ error: "invalid_query", message: "web_search needs a non-empty query." });
  }

  const ttl = settings.noCache ? 0 : cacheTtlMs(ctx?.searchConfig);
  const key = cacheKeyFor({
    v: 1,
    q: normalizeQuery(a.query),
    count: a.count,
    f: a.freshness ?? null,
    da: a.dateAfter ?? null,
    db: a.dateBefore ?? null,
    dom: a.domainFilter,
    c: a.country ?? null,
    leg: settings.videoLeg,
    tpc: settings.transcriptsPerCall,
    order: settings.searchOrder,
    paid: settings.parallelApiKey ? settings.paidMode : null,
    exa: Boolean(settings.exaApiKey),
  });
  const hit = cacheGet(key, ttl);
  if (hit) {
    log.cached = true;
    log.engine = hit.__engine ?? null;
    const { __engine, ...payload } = hit;
    return finish({ ...payload, cached: true });
  }

  // Decide the pre-search half of the rule, then start the base engine and (if needed) the leg together.
  const intent = videoIntent(a.query);
  const fresh = freshnessSignal(a.query, a.freshness);
  const ytBin = settings.videoLeg && settings.transcriptsPerCall > 0 ? resolveYtTranscript(settings) : null;
  const startLeg = Boolean(ytBin) && (intent.match || fresh.sensitive);
  log.leg.searched = startLeg;
  const legAbort = new AbortController();
  const legSignal = signal ? AbortSignal.any([signal, legAbort.signal]) : legAbort.signal;
  const legPromise = startLeg ? ytSearch(ytBin, a.query, 5, settings.videoSearchTimeoutMs, legSignal) : Promise.resolve(null);
  const base = await runEngineChain(a, settings, signal);
  // The leg normally finishes inside the base call. If it is still running once the base engine
  // has answered, give it videoLegGraceMs more, then abort it and go on without leg results.
  let legSettled = false;
  legPromise.then(() => (legSettled = true));
  if (startLeg) {
    let graceTimer;
    await Promise.race([legPromise, new Promise((r) => (graceTimer = setTimeout(r, settings.videoLegGraceMs)))]);
    clearTimeout(graceTimer);
    if (!legSettled) legAbort.abort();
  }
  const leg = await legPromise;
  if (leg && !leg.ok && legAbort.signal.aborted && !signal?.aborted) leg.error = `aborted after ${settings.videoLegGraceMs} ms grace`;
  log.engine = base.engine;
  log.attempts = base.attempts;
  if (leg) {
    log.leg.leg_items = leg.items.length;
    log.leg.leg_ms = leg.ms;
    log.leg.leg_error = leg.error;
  }

  let rows = base.results.map((r) => ({ ...r }));
  const ytTop5 = rows.slice(0, 5).filter((r) => isYouTubeVideoUrl(r.url));
  const rules = [];
  if (ytTop5.length > 0) rules.push("i");
  if (intent.match) rules.push(`ii:${intent.trigger}`);
  if (fresh.sensitive && countInWindow(rows, fresh.windowDays) < 3) rules.push(`iii:${fresh.trigger}`);
  const fired = Boolean(ytBin) && rules.length > 0;
  log.leg.fired = fired;
  log.leg.rules = rules;
  if (!ytBin && settings.videoLeg && rules.length > 0) log.leg.leg_error = "yt-transcript not found";

  const appended = [];
  if (fired) {
    let order = 0;
    const candidates = [];
    for (const r of rows) {
      const id = youTubeId(r.url);
      if (id) candidates.push({ id, url: r.url, title: r.title, published: r.published, source: "base", order: order++ });
    }
    for (const v of leg?.items ?? []) {
      const url = toHttpUrl(v.url) ?? `https://www.youtube.com/watch?v=${v.id}`;
      candidates.push({ ...v, url, source: "leg", order: order++ });
    }
    const preferWindowDays = rules.some((r) => r.startsWith("iii")) ? fresh.windowDays : 0;
    const picks = pickVideos(candidates, a.query, { max: settings.transcriptsPerCall, preferWindowDays });
    log.transcripts.attempted = picks.length;
    if (picks.length > 0) {
      // One hard wall for all transcript work combined; past it we return without excerpts.
      const t0 = Date.now();
      const wall = AbortSignal.timeout(settings.transcriptWallMs);
      const both = signal ? AbortSignal.any([signal, wall]) : wall;
      const pulled = await Promise.all(
        picks.map((p) =>
          ytTranscript(ytBin, p.id, a.query, settings.transcriptWallMs, both, {
            allowLong: typeof p.duration_s === "number" && p.duration_s > 5400,
          }),
        ),
      );
      log.transcripts.ms = Date.now() - t0;
      log.transcripts.wall_hit = wall.aborted;
      signal?.throwIfAborted?.();
      pulled.forEach((t, i) => {
        const pick = picks[i];
        log.transcripts.codes.push(t.obj?.error?.code ?? t.code ?? (t.timedOut ? "timeout" : null));
        const block = t.obj?.ok ? excerptBlock({ ...t.obj, id: t.obj.id ?? pick.id }) : null;
        if (!block) return;
        log.transcripts.attached++;
        const existing = rows.find((r) => youTubeId(r.url) === pick.id);
        if (existing) {
          existing.snippet = existing.snippet ? `${existing.snippet}\n\n${block}` : block;
          existing.__excerpt = true;
        } else {
          const meta = { ...pick, channel: pick.channel ?? t.obj.channel, duration_s: pick.duration_s ?? t.obj.duration_s, upload_date: pick.upload_date ?? t.obj.upload_date };
          appended.push({
            title: String(pick.title || t.obj.title || "YouTube video"),
            url: pick.url,
            snippet: `${videoMetaLine(meta)}\n${block}`,
            published: normalizePublished(meta.upload_date),
            siteName: "youtube.com",
            __excerpt: true,
          });
        }
      });
    }
  }

  // Fit appended videos inside the 10-row cap by dropping plain rows from the end.
  if (appended.length > 0) {
    while (rows.length + appended.length > MAX_ROWS) {
      const idx = rows.map((r) => Boolean(r.__excerpt)).lastIndexOf(false);
      if (idx < 0) break;
      rows.splice(idx, 1);
    }
    rows = [...rows, ...appended].slice(0, MAX_ROWS);
  }
  // Keep the whole payload under the 20,000-character budget: shrink plain snippets first.
  for (const cap of [600, 300, 120]) {
    if (outputSize(rows) <= OUTPUT_BUDGET) break;
    for (const r of rows) if (!r.__excerpt && r.snippet && r.snippet.length > cap) r.snippet = clip(r.snippet, cap);
  }

  if (rows.length === 0) {
    if (base.allRefused) {
      const why = base.attempts.map((x) => `${x.engine}: ${x.message ?? x.error}`).join("; ");
      log.error = "all_engines_failed";
      return finish({ error: "search_failed", message: `All search engines failed (${why}).` });
    }
  }

  const results = rows.map((r) => {
    const out = { title: String(r.title ?? ""), url: r.url };
    if (r.snippet) out.snippet = r.snippet;
    if (r.published) out.published = r.published;
    if (r.siteName) out.siteName = r.siteName;
    return out;
  });
  const payload = {
    query: a.query,
    provider: PROVIDER_ID,
    count: results.length,
    tookMs: Date.now() - started,
    results,
    externalContent: { untrusted: true, source: "web_search", provider: PROVIDER_ID, wrapped: true },
  };
  if (results.length > 0) cacheSet(key, { ...payload, __engine: base.engine }, ttl);
  return finish(payload);
}
