// web_image's climb (tools.mjs, 2026-10-23): the bytes of ONE picture, over the ladder's fetch rungs only:
//   F1  a plain fetch from this machine with the ladder's browser identity, free  (refused, a challenge or thin -> L1)
//   L1  Browserbase Fetch, no proxies, paid                                       (refused, a challenge or thin -> L2)
//   L2  Browserbase Fetch with proxies, paid
// Never a browser rung: a browser hands back the page it rendered, never a picture's bytes. The ladder's site memory is
// read and never written: a host resting from this machine's free rungs (local.js cooldowns) or past the shared
// per-minute window starts at L1, as web_fetch's climb does, but a picture refused to a plain fetch may be hotlink
// protection rather than a verdict on the home connection, so it never sends the host's page reads up the ladder.
// Kept: jpeg, png, gif and webp, the four Claude Code shows a model, each known by its own first bytes, and only when
// the answer's content type is image/*. Another picture type, a page or any other file is refused where it stands,
// since no paid rung changes what a URL serves.
import { bbFetch } from "./browserbase.js";
import { detectChallenge } from "./detect.js";
import { describeWall, wallFromBbError } from "./ladder.js";
import { browserHeaders, fetchPublicOnly, localCooldownFor, localNetWall, readCapped, takeLocalSlot } from "./local.js";
import { appendCallLog, hostOf, isPrivateHost, recordUsage, syncProjectUsage } from "./util.js";

export const IMAGE_CAP = 15 * 1024 * 1024; // 15 MB; a call's max_bytes may only lower it
export const IMAGE_TYPES = { "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp" };

// ---- what the bytes are -----------------------------------------------------------------------
const be16 = (b, i) => (b[i] << 8) | b[i + 1];
const le16 = (b, i) => b[i] | (b[i + 1] << 8);
const le24 = (b, i) => b[i] | (b[i + 1] << 8) | (b[i + 2] << 16);
const sized = (type, width, height) => (width > 0 && height > 0 ? { type, ext: IMAGE_TYPES[type], width, height } : null);

// A picture's own type and size from its bytes, { type, ext, width, height }, or null when they are none of the four
// as a whole picture (a block page, a cut file, an avif).
export function imageInfo(buf) {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf || []);
  const n = b.length;
  const is = (s, i = 0) => n >= i + s.length && b.toString("latin1", i, i + s.length) === s;
  if (n >= 24 && is("\x89PNG\r\n\x1a\n") && is("IHDR", 12)) return sized("image/png", b.readUInt32BE(16), b.readUInt32BE(20));
  if (n >= 10 && (is("GIF87a") || is("GIF89a"))) return sized("image/gif", le16(b, 6), le16(b, 8));
  if (n >= 30 && is("RIFF") && is("WEBP", 8)) {
    if (is("VP8 ", 12) && b[23] === 0x9d && b[24] === 0x01 && b[25] === 0x2a) return sized("image/webp", le16(b, 26) & 0x3fff, le16(b, 28) & 0x3fff);
    if (is("VP8L", 12) && b[20] === 0x2f) return sized("image/webp", 1 + (b[21] | ((b[22] & 0x3f) << 8)), 1 + (((b[22] & 0xc0) >> 6) | (b[23] << 2) | ((b[24] & 0x0f) << 10)));
    if (is("VP8X", 12)) return sized("image/webp", 1 + le24(b, 24), 1 + le24(b, 27));
    return null;
  }
  if (n >= 4 && b[0] === 0xff && b[1] === 0xd8) {
    // Marker to marker up to the frame header (SOF0 to SOF15 but DHT, JPG and DAC), each segment skipped by its length.
    for (let i = 2; i + 8 < n; ) {
      if (b[i] !== 0xff) return null;
      const m = b[i + 1];
      if (m === 0xff) i += 1;
      else if (m === 0x01 || (m >= 0xd0 && m <= 0xd8)) i += 2;
      else if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) return sized("image/jpeg", be16(b, i + 7), be16(b, i + 5));
      else i += 2 + be16(b, i + 2);
    }
  }
  return null;
}

// A picture type no model is shown, from the bytes, so a CDN that answers avif under a jpeg header is named, not climbed.
function otherPicture(b) {
  const brand = b.length >= 12 && b.toString("latin1", 4, 8) === "ftyp" ? b.toString("latin1", 8, 12) : "";
  if (/^avi[fs]$/.test(brand)) return "image/avif";
  if (/^(heic|heix|hevc|hevx|mif1|msf1)$/.test(brand)) return "image/heic";
  if (/<svg[\s>]/i.test(b.toString("utf8", 0, Math.min(b.length, 2048)))) return "image/svg+xml";
  return "";
}

const visible = (page) => page.replace(/<(script|style|noscript)\b[\s\S]*?<\/\1>/gi, " ").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

// One rung's answer, judged the same on every rung.
function judge({ rung, status, ctype, buf, t0, paid = {} }) {
  const base = { rung, status, ms: Date.now() - t0, ...paid };
  const no = (wall, outcome, next = "stop", extra = {}) => ({ ...base, ok: false, wall, outcome, next, ...extra });
  const type = String(ctype || "").split(";")[0].trim().toLowerCase();
  if (status === 404 || status === 410) return no(`http_${status}`, `not_found:http_${status}`);
  if (!(status >= 200 && status < 300)) return no(`http_${status}`, `blocked:http_${status}`, "climb");
  if (/^image\//.test(type)) {
    const shown = imageInfo(buf);
    if (shown) return { ...base, ok: true, outcome: "ok", buf, served: type, ...shown };
    const other = otherPicture(buf) || (IMAGE_TYPES[type] ? "" : type);
    if (other) return no("not_viewable", `type:${other}`, "stop", { type: other });
    return no("thin", `thin:${type}`, "climb"); // says jpeg, png, gif or webp and is not a whole one
  }
  if (/html/.test(type) || (!type && /^\s*</.test(buf.toString("latin1", 0, 64)))) {
    const page = buf.toString("utf8");
    const c = detectChallenge({ html: page, title: /<title[^>]*>([\s\S]*?)<\/title>/i.exec(page)?.[1] || "", visibleText: visible(page) });
    if (c) return no(c.kind === "challenge" ? "captcha_unsolved" : "blocked", `${c.kind}:${c.why}`, "climb");
  }
  return no("not_image", `type:${type || "none"}`, "stop", { type: type || "no content type" });
}

// ---- the rungs ---------------------------------------------------------------------------------
// A browser's own request for a picture on a page (the ladder's browser identity, local.js), not a tab opening the URL,
// so a host that answers a tab with its HTML viewer page answers this with the picture. Accept leaves out avif and svg,
// which no model is shown, so a CDN that picks the format by Accept sends webp or the original.
function imageHeaders(settings) {
  const { "upgrade-insecure-requests": _u, "sec-fetch-user": _s, ...h } = browserHeaders(settings);
  return { ...h, accept: "image/webp,image/apng,image/*,*/*;q=0.8", "sec-fetch-dest": "image", "sec-fetch-mode": "no-cors", "sec-fetch-site": "cross-site" };
}

async function f1Rung({ url, settings, cap, signal, fetchImpl }) {
  const t0 = Date.now();
  let res;
  let buf;
  try {
    const sig = signal ? AbortSignal.any([signal, AbortSignal.timeout(settings.localFetchTimeoutMs)]) : AbortSignal.timeout(settings.localFetchTimeoutMs);
    ({ res } = await fetchPublicOnly(url, { headers: imageHeaders(settings), signal: sig }, fetchImpl, settings.blockPrivateHosts));
    if (Number(res.headers.get("content-length")) > cap) {
      try {
        await res.body?.cancel();
      } catch {}
      throw Object.assign(new Error("too large"), { code: "TOO_LARGE" });
    }
    buf = await readCapped(res, cap);
  } catch (err) {
    if (signal?.aborted) throw err;
    const wall = localNetWall(err);
    // A private target, a name that does not resolve and a picture past the cap are the same on every rung.
    return { rung: "F1", ok: false, wall, outcome: `net:${wall}`, ms: Date.now() - t0, next: ["dns_error", "invalid_url", "too_large"].includes(wall) ? "stop" : "climb" };
  }
  recordUsage(settings, { localFetchCalls: 1, localBytes: buf.length });
  return judge({ rung: "F1", status: res.status, ctype: res.headers.get("content-type"), buf, t0 });
}

// L1 or L2: Browserbase Fetch in its raw format, a picture coming back base64 (the ladder's fetchRung reads the same
// answer as a binary wall). Counted and booked as fetchRung does: the meter on every try, a spend row per answer.
async function bbRung({ rung, url, proxies, settings, apiKey, cap, signal, timeoutMs, fetchImpl }) {
  const t0 = Date.now();
  let res;
  for (let tries = 1; ; tries++) {
    try {
      recordUsage(settings, { fetchCalls: 1, ...(proxies ? { fetchProxiedCalls: 1 } : {}) });
      res = await bbFetch({ base: settings.apiBaseUrl, apiKey, url, proxies, format: "raw", allowInsecureSsl: settings.allowInsecureSsl, timeoutMs, signal, fetchImpl });
      break;
    } catch (err) {
      if (signal?.aborted) throw err;
      if (err?.kind === "concurrency" && tries < 2) {
        await new Promise((r) => setTimeout(r, 1200));
        continue;
      }
      const wall = wallFromBbError(err); // Browserbase itself said no: a second paid call would hear the same
      return { rung, ok: false, wall, outcome: wall, ms: Date.now() - t0, next: "stop" };
    }
  }
  const paid = { billed: true, bytes: res.responseBytes + 1024 };
  if (proxies) recordUsage(settings, { proxyBytesEst: paid.bytes });
  const raw = res.encoding === "base64" ? Buffer.from(String(res.content ?? ""), "base64") : Buffer.from(typeof res.content === "string" ? res.content : JSON.stringify(res.content ?? ""), "utf8");
  if (raw.length > cap) return { rung, ok: false, wall: "too_large", outcome: "too_large:cap", status: res.statusCode, ms: Date.now() - t0, next: "stop", ...paid };
  const ctype = res.contentType || res.headers?.["content-type"] || res.headers?.["Content-Type"];
  return judge({ rung, status: Number.isFinite(res.statusCode) ? res.statusCode : 200, ctype, buf: raw, t0, paid });
}

// ---- the climb ---------------------------------------------------------------------------------
const trail = (attempts) => attempts.map((a) => `${a.rung}:${a.outcome}`).join(",");

// Success: { ok, rung, buf, type, ext, width, height, served, status, ms, attempts, paid, skipped }; failure: { ok: false,
// wall, type, attempts, paid, ms, skipped }. `paid` is the Browserbase calls it paid for, as fetchRows books them.
export async function runImage({ url, settings, creds = {}, cap = IMAGE_CAP, signal, deps = {} }) {
  const t0 = Date.now();
  const fetchImpl = deps.fetchImpl || fetch;
  const host = hostOf(url) || "invalid";
  const attempts = [];
  let skipped = "";
  const end = (win, wall) => {
    const last = win || attempts.at(-1);
    appendCallLog(settings, { host, tool: "web_image", rung: win ? win.rung : "none", outcome: win ? "ok" : "fail", ...(wall ? { wall } : {}), status: typeof last?.status === "number" ? last.status : null, ms: Date.now() - t0, rungs: trail(attempts) });
    if (attempts.some((a) => a.billed)) syncProjectUsage(settings, creds, fetchImpl);
    const paid = attempts.filter((a) => a.billed).map((a) => ({ rung: a.rung, proxies: a.rung === "L2", seconds: 0, bytes: a.bytes }));
    const common = { attempts, rungs: trail(attempts), paid, ms: Date.now() - t0, skipped };
    return win ? { ...win, ...common } : { ok: false, wall, type: last?.type, outcome: last?.outcome, ...common };
  };

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return end(null, "invalid_url");
  }
  if (!/^https?:$/.test(parsed.protocol) || (settings.blockPrivateHosts && isPrivateHost(parsed.hostname))) return end(null, "invalid_url");

  const hostLc = parsed.hostname.toLowerCase();
  const rest = settings.localEnabled ? localCooldownFor(settings, hostLc) : null;
  const capped = settings.localEnabled && !rest ? takeLocalSlot(settings, hostLc) : null;
  if (rest) skipped = `F1 skipped: ${hostLc} refused a plain read from this machine and rests from it until ${new Date(rest.until).toISOString().slice(0, 16).replace("T", " ")} UTC.`;
  else if (capped) skipped = "F1 skipped: this machine's free reads are at their per-minute cap, shared by every room.";
  if (settings.localEnabled && !rest && !capped) {
    const r = await f1Rung({ url, settings, cap, signal, fetchImpl: deps.localFetchImpl || fetch });
    attempts.push(r);
    if (r.ok) return end(r);
    if (r.next === "stop") return end(null, r.wall);
  }
  if (!creds.apiKey) return end(null, "no_key");
  for (const [rung, proxies, timeoutMs] of [["L1", false, settings.l1TimeoutMs], ["L2", true, settings.l2TimeoutMs]]) {
    if (proxies && !settings.l2Proxies) break;
    const r = await bbRung({ rung, url, proxies, settings, apiKey: creds.apiKey, cap, signal, timeoutMs, fetchImpl });
    attempts.push(r);
    if (r.ok) return end(r);
    if (r.next === "stop") break;
  }
  return end(null, attempts.at(-1).wall);
}

// ---- what a wall says ----------------------------------------------------------------------------
// [what stopped it, the next move] for a failed climb; the ladder's own words for a wall it shares (a Browserbase
// refusal, TLS, DNS, an address that is not public, an HTTP code).
export function imageWall(p, cap = IMAGE_CAP) {
  const shot = "Ask the owner for a screenshot.";
  const http = /^http_(\d{3})$/.exec(p.wall || "");
  switch (p.wall) {
    case "no_key": return ["The free rung did not get the picture, and there is no Browserbase key for the paid ones.", "Put a Browserbase key in the private folder's secrets/browserbase.key, or ask the owner for a screenshot."];
    case "timeout": return ["The picture did not arrive inside the time cap.", "Retry; the site may be slow right now."];
    case "too_large": return [p.outcome === "too_large" ? "The picture is over Browserbase Fetch's 5 MB limit." : `The picture is over the cap of ${cap.toLocaleString("en-US")} bytes.`, shot];
    case "not_image": return [`The URL serves ${p.type}, not a picture.`, p.type === "text/html" ? "A page: web_fetch reads it, and a picture on it has a URL of its own." : "web_image keeps a jpeg, png, gif or webp picture only."];
    case "not_viewable": return [`The URL serves a ${p.type} picture, and a model is shown only jpeg, png, gif and webp.`, `Look for the same picture as a jpeg, png or webp, or ${shot.toLowerCase()}`];
    case "thin": return ["Every rung tried answered with something that is not a whole picture (a cut file or a placeholder).", `Retry later, or ${shot.toLowerCase()}`];
    case "captcha_unsolved": return ["A bot challenge answered in place of the picture on every rung tried; a picture never goes to a browser rung.", shot];
    case "blocked": return ["The site refused every rung tried.", shot];
    default:
      if (http && (http[1] === "404" || http[1] === "410")) return [`The site says the picture is not there (HTTP ${http[1]}).`, "Check the URL."];
      return ((w) => [w[0], w[2]])(describeWall(p.wall));
  }
}
