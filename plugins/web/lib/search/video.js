// The search's video leg: the decision rule, the yt-transcript calls, the pick and the splice.
// The model never decides any of this; it only ever sees the finished snippet. yt-transcript is a
// command of this machine's own (settings.ytTranscriptPath, else one named yt-transcript on PATH);
// without one the leg is skipped and search answers as before.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { clip, normalizePublished } from "./util.js";

const HOME = os.homedir();

// ---- rule (ii): video intent (REPORT.md section 1 item 3 (ii)) ----
// The list, literally: how to, tutorial, walkthrough, guide, fix, setup, install, form,
// technique, workout, recipe, product review, vs, comparison, unboxing, hands-on, teardown,
// demo, gameplay, build, patch notes, keynote, talk, podcast, lecture, earnings call,
// a named person's interview, "what did X say". Plus the literal words youtube / video.
const INTENT_RE = new RegExp(
  [
    String.raw`\bhow to\b`,
    String.raw`\btutorials?\b`,
    String.raw`\bwalk ?throughs?\b`,
    String.raw`\bguides?\b`,
    String.raw`\bfix(es)?\b`,
    String.raw`\bset ?up\b`,
    String.raw`\binstall(ation)?\b`,
    String.raw`\btechniques?\b`,
    String.raw`\bworkouts?\b`,
    String.raw`\brecipes?\b`,
    String.raw`\breviews?\b`,
    String.raw`\bvs\.?(?=\s|$)`,
    String.raw`\bversus\b`,
    String.raw`\bcomparisons?\b`,
    String.raw`\bunboxing\b`,
    String.raw`\bhands[- ]on\b`,
    String.raw`\bteardowns?\b`,
    String.raw`\bdemos?\b`,
    String.raw`\bgameplay\b`,
    String.raw`\bbuilds?\b`,
    String.raw`\bpatch notes\b`,
    String.raw`\bkeynotes?\b`,
    String.raw`\btalks?\b`,
    String.raw`\bpodcasts?\b`,
    String.raw`\blectures?\b`,
    String.raw`\bearnings call\b`,
    String.raw`\binterviews?\b`,
    String.raw`\bwhat did .{2,60}? say\b`,
    String.raw`\byoutube\b`,
    String.raw`\bvideos?\b`,
  ].join("|"),
  "i",
);
// "form" is on the list as exercise form; narrowed so application, tax and arrival forms do not fire.
const FORM_RE = /\b(proper|good|correct|bad|perfect|strict|squat|deadlift|bench|press|running|swim(ming)?|golf|lifting|exercise|pull ?up|push ?up|row)\s+form\b|\bform\s+(check|tips|cues|breakdown)\b/i;
// Named false positives from our own queries: tenant or employee reviews, job interview process.
const REVIEW_FP_RE = /\b(tenant|tenants|employee|employees|employer|landlord|glassdoor|indeed|workplace)\b/i;
const INTERVIEW_FP_RE = /\binterview (process|stages|rounds)\b/i;

export function videoIntent(query) {
  const q = String(query ?? "");
  if (FORM_RE.test(q)) return { match: true, trigger: "form" };
  const re = new RegExp(INTENT_RE.source, "gi");
  for (const m of q.matchAll(re)) {
    const word = m[0].toLowerCase();
    if (/^reviews?$/.test(word) && REVIEW_FP_RE.test(q)) continue;
    if (/^interviews?$/.test(word) && INTERVIEW_FP_RE.test(q)) continue;
    return { match: true, trigger: word.replace(/\s+/g, " ").slice(0, 40) };
  }
  return { match: false, trigger: null };
}

// ---- rule (iii): freshness ----
const FRESH_WORDS = [
  [/\btoday\b/i, 2, "today"],
  [/\byesterday\b/i, 2, "yesterday"],
  [/\bthis week\b/i, 7, "this week"],
  [/\bjust announced\b/i, 7, "just announced"],
  [/\blaunch(es|ed)?\b/i, 7, "launch"],
  [/\bleak(s|ed)?\b/i, 7, "leak"],
];

export function freshnessSignal(query, freshness) {
  if (freshness === "day") return { sensitive: true, windowDays: 1, trigger: "freshness=day" };
  if (freshness === "week") return { sensitive: true, windowDays: 7, trigger: "freshness=week" };
  for (const [re, days, label] of FRESH_WORDS) {
    if (re.test(String(query ?? ""))) return { sensitive: true, windowDays: days, trigger: label };
  }
  return { sensitive: false, windowDays: 0, trigger: null };
}

function windowCutoffMs(days) {
  // Dates are day-granular, so allow half a day of slack on top of the window.
  return Date.now() - days * 86400000 - 12 * 3600000;
}

export function isoInWindow(iso, days) {
  const p = normalizePublished(iso);
  if (!p) return false;
  const t = Date.parse(p.length === 10 ? `${p}T00:00:00Z` : p);
  return Number.isFinite(t) && t >= windowCutoffMs(days);
}

export function countInWindow(results, days) {
  return results.filter((r) => isoInWindow(r.published, days)).length;
}

// ---- YouTube URL handling ----
export function youTubeId(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  const host = u.hostname.toLowerCase().replace(/^www\.|^m\./, "");
  let id = null;
  if (host === "youtu.be") id = u.pathname.split("/")[1] || null;
  else if (host === "youtube.com" || host === "music.youtube.com") {
    if (u.pathname === "/watch") id = u.searchParams.get("v");
    else {
      const m = /^\/(shorts|live|embed)\/([^/?#]+)/.exec(u.pathname);
      if (m) id = m[2];
    }
  }
  return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
}

export function isYouTubeVideoUrl(url) {
  return youTubeId(url) !== null;
}

function isLiveUrl(url) {
  try {
    return /^\/live\//.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

// ---- yt-transcript binary: the setting, then PATH ----
function isExecutable(p) {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

export function resolveYtTranscript(settings) {
  if (settings.ytTranscriptBin && isExecutable(settings.ytTranscriptBin)) return settings.ytTranscriptBin; // test hook only
  if (settings.ytTranscriptPath) return isExecutable(settings.ytTranscriptPath) ? settings.ytTranscriptPath : null;
  for (const dir of childEnv().PATH.split(":")) {
    if (dir && isExecutable(path.join(dir, "yt-transcript"))) return path.join(dir, "yt-transcript");
  }
  return null;
}

// A service's PATH may not include ~/.local/bin, where pip puts yt-dlp.
function childEnv() {
  const local = path.join(HOME, ".local/bin");
  const parts = [local, ...(process.env.PATH || "").split(":").filter((p) => p && p !== local)];
  for (const p of ["/usr/local/bin", "/usr/bin", "/bin"]) if (!parts.includes(p)) parts.push(p);
  return { ...process.env, PATH: parts.join(":") };
}

export function runChild(bin, args, { timeoutMs, signal, maxBytes = 4 * 1024 * 1024 }) {
  return new Promise((resolve) => {
    const started = Date.now();
    if (signal?.aborted) {
      resolve({ code: null, stdout: "", stderr: "aborted before start", timedOut: false, ms: 0 });
      return;
    }
    let child;
    try {
      child = spawn(bin, args, { env: childEnv(), stdio: ["ignore", "pipe", "pipe"] });
    } catch (err) {
      resolve({ code: null, stdout: "", stderr: String(err?.message ?? err), timedOut: false, ms: 0, spawnError: true });
      return;
    }
    let stdout = "";
    let stderr = "";
    let done = false;
    let timedOut = false;
    const finish = (code) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      signal?.removeEventListener?.("abort", onAbort);
      resolve({ code, stdout, stderr, timedOut, ms: Date.now() - started });
    };
    const kill = () => {
      try {
        child.kill("SIGKILL");
      } catch {}
    };
    const timer = setTimeout(() => {
      timedOut = true;
      kill();
      finish(null);
    }, timeoutMs);
    const onAbort = () => {
      kill();
      finish(null);
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
    child.stdout.on("data", (d) => {
      if (stdout.length < maxBytes) stdout += d.toString("utf8");
    });
    child.stderr.on("data", (d) => {
      if (stderr.length < 65536) stderr += d.toString("utf8");
    });
    child.on("error", (err) => {
      stderr += String(err?.message ?? err);
      finish(null);
    });
    child.on("close", (code) => finish(code));
  });
}

// yt-transcript --search "<query>" --n 5 --json  ->  [{ id, title, channel, duration_s, upload_date, live, url }]
export async function ytSearch(bin, query, n, timeoutMs, signal) {
  const r = await runChild(bin, ["--search", query, "--n", String(n), "--json"], { timeoutMs, signal });
  let items = [];
  try {
    const parsed = JSON.parse(r.stdout);
    if (Array.isArray(parsed)) items = parsed.filter((x) => x && typeof x === "object" && typeof x.id === "string");
  } catch {}
  return {
    ok: items.length > 0,
    items,
    ms: r.ms,
    code: r.code,
    error: items.length ? null : r.timedOut ? "timeout" : `exit ${r.code}${r.stderr ? `: ${clip(r.stderr, 160)}` : ""}`,
  };
}

// yt-transcript <id> --query "<query>" --json  ->  one object, always (see brief B for the shape)
// allowLong: the only matching video runs past 90 minutes, so lift yt-transcript's own 90-minute cap
// (--max-minutes 0); only the query-matched windows are spliced either way.
export async function ytTranscript(bin, id, query, timeoutMs, signal, { allowLong = false } = {}) {
  const args = [id, "--query", query, "--json"];
  if (allowLong) args.push("--max-minutes", "0");
  const r = await runChild(bin, args, { timeoutMs, signal });
  let obj = null;
  try {
    obj = JSON.parse(r.stdout);
  } catch {}
  return { obj: obj && typeof obj === "object" && !Array.isArray(obj) ? obj : null, code: r.code, ms: r.ms, timedOut: r.timedOut };
}

// ---- the pick: titles must share at least half of the query's content words ----
const STOP = new Set(
  "a an the and or of to in on for with by at from is are was were be been being how do does did i you we me my your our what which who whom when where why can could should would will it its this that these those vs versus about into up over as not no yes if then than there here get got any some more most best top vs. way ways".split(
    " ",
  ),
);

function stem(w) {
  if (w.length > 4 && w.endsWith("ies")) return w.slice(0, -3) + "y";
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) return w.slice(0, -1);
  return w;
}

export function contentWords(text) {
  const out = new Set();
  for (const raw of String(text ?? "").toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
    if (!raw || raw.length < 2 || STOP.has(raw)) continue;
    out.add(stem(raw));
  }
  return out;
}

export function titleOverlap(query, title) {
  const q = contentWords(query);
  if (q.size === 0) return { ratio: 0, matched: 0, needed: Infinity };
  const t = contentWords(String(title ?? "").replace(/\s+-\s+YouTube\s*$/i, ""));
  let matched = 0;
  for (const w of q) if (t.has(w)) matched++;
  return { ratio: matched / q.size, matched, needed: Math.ceil(q.size / 2) };
}

// candidates: [{ id, url, title, channel?, duration_s?, upload_date?, live?, source: "base" | "leg", order }]
export function pickVideos(candidates, query, { max, preferWindowDays = 0 }) {
  const seen = new Set();
  const scored = [];
  for (const c of candidates) {
    if (!c?.id || seen.has(c.id)) continue;
    seen.add(c.id);
    if (c.live === true || isLiveUrl(c.url)) continue;
    const ov = titleOverlap(query, c.title);
    if (ov.matched < ov.needed) continue;
    scored.push({ ...c, overlap: ov.ratio, inWindow: preferWindowDays ? isoInWindow(c.upload_date ?? c.published, preferWindowDays) : false });
  }
  const short = scored.filter((c) => !(typeof c.duration_s === "number" && c.duration_s > 5400));
  const pool = short.length > 0 ? short : scored.slice(0, 1); // a long video only when it is the only match
  pool.sort((a, b) => Number(b.inWindow) - Number(a.inWindow) || b.overlap - a.overlap || a.order - b.order);
  return pool.slice(0, Math.max(0, max));
}

// ---- the splice ----
export function fmtStamp(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function excerptBlock(t, maxChars = 2800) {
  const windows = Array.isArray(t?.windows) ? t.windows : [];
  const lines = ["Transcript excerpts:"];
  let used = lines[0].length;
  for (const w of windows) {
    const text = clip(w?.text ?? "", 1200);
    if (!text) continue;
    const link = typeof w.deep_link === "string" && w.deep_link ? w.deep_link : t.id ? `https://youtu.be/${t.id}?t=${Math.floor(Number(w.start_s) || 0)}` : "";
    const pair = `[${fmtStamp(w.start_s)}] ${text}${link ? `\n${link}` : ""}`;
    if (used + pair.length + 1 > maxChars && lines.length > 1) break;
    lines.push(pair);
    used += pair.length + 1;
  }
  if (lines.length === 1) return null;
  if (typeof t.cache_path === "string" && t.cache_path) lines.push(`Full transcript: ${t.cache_path}`);
  return lines.join("\n");
}

export function videoMetaLine(v) {
  const bits = ["YouTube video"];
  if (v.channel) bits.push(`channel ${v.channel}`);
  if (typeof v.duration_s === "number" && v.duration_s > 0) bits.push(`duration ${fmtStamp(v.duration_s)}`);
  const up = normalizePublished(v.upload_date);
  if (up) bits.push(`uploaded ${up}`);
  return bits.join(", ") + ".";
}
