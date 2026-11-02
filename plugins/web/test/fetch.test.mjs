// The fetch ladder's changes carried from the machine's lib (2026-10-21 and 2026-10-22): F2's readable-stop and its
// probe's real text length, the challenge kinds that clear on their own, the hopeless skip with its whole-call ceiling,
// path-class site memory and the modal lift; a Bookings page read as a guest (need 21). Run from the plug-in folder: npm test.
// The F2 cases launch the machine's own local browser (skipped when its engine is not installed) on a temp profile
// against a local http server on 127.0.0.1 (blockPrivateHosts off for that reason). The ladder cases use its seams
// (deps.localFetchImpl, deps.localBrowserRung, deps.trawlFetchImpl, deps.fetchImpl, settings._now) and never touch the
// network. The Browserbase key is fake; nothing is spent. Every name in here is an example (decision 22).
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import { detectChallenge } from "../lib/fetch/detect.js";
import { extractHtml } from "../lib/fetch/extract.js";
import { bookingsGuestUrl, isBookingsPage, runFetchPlus } from "../lib/fetch/ladder.js";
import { closeLocalBrowser, localBrowserPresent, localBrowserRung, localFetchRung, memoryKey, pathClass, rememberRead, siteMemoryPlan } from "../lib/fetch/local.js";
import { flushPending, resolveSettings } from "../lib/fetch/util.js";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "yerk-web-fetch-"));
const STATE = path.join(TMP, "state");
const HOUR = 3600000;
const T0 = Date.parse("2026-10-22T22:00:00Z");
const page = (title, body, head = "") => `<!doctype html><html><head><title>${title}</title>${head}</head><body>${body}</body></html>`;
const ARTICLE = `<article><h1>Rain moves into the city tonight</h1>${Array.from({ length: 30 }, (_, i) => `<p>Paragraph ${i + 1}: the weather office says a band of rain will reach the city after dark, with 15 to 25 millimetres expected by morning and gusts near 50 km/h along the lake.</p>`).join("")}</article>`;
const SKELETON = `<nav>${"menu item ".repeat(60)}</nav><main id="m"><p>${"Loading the player page, please wait. ".repeat(100)}</p></main>`;
const LATE_DATA = `<article><h1>Example player</h1>${"Real player stats line with numbers 12 7 3. ".repeat(120)}</article>`;
const BEACON = `<script>setInterval(() => fetch("/ping").catch(() => {}), 300);</script>`;
// A listing page with a notice up: every body child but the dialog's portal is aria-hidden (the hide-others pattern).
const LISTING = `<h1>Two-bedroom flat with a courtyard</h1><p>Entire rental unit. 6 guests, 2 bedrooms, 4 beds, 2 baths.</p>${Array.from({ length: 14 }, (_, i) => `<p>Section ${i + 1}: the main bedroom has a 140 x 200 cm bed, the second bedroom a queen bed, air conditioning in both rooms, an elevator to the third floor and a quiet courtyard. <span aria-hidden="true">icon</span></p>`).join("")}`;
const DIALOG = `<div><div role="dialog" aria-modal="true" aria-label="Translation on"><h2>Translation on</h2><p>This symbol shows when content from hosts and guests has been automatically translated. Change this setting at any time.</p></div></div>`;
const roomPage = ({ modal = true } = {}) => `<!doctype html><html><head><title>Two-bedroom flat with a courtyard</title></head><body><div id="skip-links"${modal ? ' aria-hidden="true"' : ""}><a href="#main">Skip to content</a></div><div id="app"${modal ? ' aria-hidden="true"' : ""}><main id="main">${LISTING}</main></div>${modal ? DIALOG : ""}</body></html>`;

const BROWSER = localBrowserPresent(resolveSettings({}, { stateDir: path.join(TMP, "probe") }));
const needsBrowser = BROWSER ? {} : { skip: "no local browser engine on this machine" };

let server;
let base;
before(async () => {
  server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    const send = (body, type = "text/html; charset=utf-8", status = 200) => {
      res.writeHead(status, { "content-type": type });
      res.end(body);
    };
    if (u.pathname.startsWith("/rooms/")) return send(roomPage());
    switch (u.pathname) {
      case "/static":
        return send(page("Static article", ARTICLE));
      case "/late":
        return send(page("Late data", `${SKELETON}${BEACON}<script>fetch("/data").then((r) => r.text()).then((t) => { document.getElementById("m").innerHTML = t; });</script>`));
      case "/data":
        return void setTimeout(() => send(LATE_DATA, "text/plain"), 1200);
      case "/busy":
        return send(page("Busy", `${ARTICLE}${BEACON}<div id="t"></div><script>setInterval(() => { document.getElementById("t").textContent += " tick"; }, 200);</script>`));
      case "/cfmarker":
        return send(page("Real page behind Cloudflare", ARTICLE, `<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>`));
      case "/cdn-cgi/challenge-platform/scripts/jsd/main.js":
        return send("/* bot management stub */", "application/javascript");
      case "/interstitial":
        return send(page("Just a moment...", `<p>Checking your browser before accessing the site.</p><script>setTimeout(() => location.replace("/static"), 1500);</script>`));
      case "/recaptcha":
        return send(page("Robot check", `<div class="g-recaptcha"></div><p>Please solve the captcha to continue.</p>`));
      case "/ping":
        return send("ok", "text/plain");
      default:
        return send("nope", "text/plain", 404);
    }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  await closeLocalBrowser().catch(() => {});
  await Promise.race([flushPending(), new Promise((r) => setTimeout(r, 2000).unref())]);
  server?.close();
  fs.rmSync(TMP, { recursive: true, force: true });
});

// ---- detect.js: the probe's real text length ------------------------------------------------------
test("a cut visible text with its whole length is no challenge on a real page carrying Cloudflare's script", () => {
  const html = `<html><head><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></head><body>x</body></html>`;
  const cut = "word ".repeat(600);   // 3,000 chars, the cut the probe sent before it passed the whole length
  assert.equal(detectChallenge({ html, title: "News", visibleText: cut, visibleLength: 26000 }), null);
  assert.equal(detectChallenge({ html, title: "News", visibleText: "Checking your browser" })?.kind, "challenge");
});

// ---- F2: the readable-stop, on the machine's browser ----------------------------------------------
const f2Settings = resolveSettings({}, { stateDir: STATE, blockPrivateHosts: false, localNetworkIdleMs: 3000, localChallengeWaitMs: 6000, localIdleCloseMs: 1000, siteMemory: false, localCooldownHours: 0, noLog: true });
const f2 = (p) => localBrowserRung({ url: `${base}${p}`, settings: f2Settings, extractMode: "markdown", budgetMs: 30000 });

test("readable-stop: a page that is readable at domcontentloaded ends the wait within two polls", needsBrowser, async () => {
  const r = await f2("/static");
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.ok(["readable", "networkidle"].includes(r.phases.stop), r.phases.stop);
  assert.ok(r.phases.settle < 1800, `settle ${r.phases.settle}`);
  assert.ok(r.phases.challenge < 50, `challenge ${r.phases.challenge}`);
  assert.match(r.ex.content, /the weather office/);
});

test("readable-stop waits while the page's data is in flight: a 4,000-char skeleton above the threshold is not the page", needsBrowser, async () => {
  const r = await f2("/late");
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.equal(r.phases.stop, "readable");
  assert.ok(r.phases.settle >= 1200 && r.phases.settle < 3000, `settle ${r.phases.settle}`);
  assert.match(r.ex.content, /Real player stats/);
});

test("readable-stop ceiling: a page whose text never settles and whose network never idles stops at localNetworkIdleMs", needsBrowser, async () => {
  const r = await f2("/busy");
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.equal(r.phases.stop, "ceiling");
  assert.ok(r.phases.settle >= 2900 && r.phases.settle < 4000, `settle ${r.phases.settle}`);
});

test("a real page with Cloudflare's bot-management script in its head is not a challenge inside the probe", needsBrowser, async () => {
  const r = await f2("/cfmarker");
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.equal(r.phases.kind, undefined);
  assert.ok(r.phases.challenge < 50, `challenge ${r.phases.challenge}`);
  assert.ok(r.phases.settle < 1800, `settle ${r.phases.settle}`);
});

test("a self-clearing interstitial is waited out, then the real page gets the readable-stop", needsBrowser, async () => {
  const r = await f2("/interstitial");
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.equal(r.phases.kind, "challenge_title");
  assert.ok(r.phases.challenge >= 1000, `challenge ${r.phases.challenge}`);
  assert.match(r.ex.content, /the weather office/);
});

test("a recaptcha page is a challenge the loop does not wait on", needsBrowser, async () => {
  const r = await f2("/recaptcha");
  assert.equal(r.ok, false);
  assert.equal(r.outcome, "challenge:recaptcha");
  assert.equal(r.phases.kind, "recaptcha");
  assert.ok(r.phases.challenge < 500, `challenge ${r.phases.challenge}`);
});

test("F2 reads a listing page through its dialog (the in-browser lift), within the readable-stop", needsBrowser, async () => {
  const r = await localBrowserRung({ url: `${base}/rooms/1000000000000001`, settings: f2Settings, extractMode: "markdown", budgetMs: 30000 });
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.match(r.ex.content, /140 x 200 cm bed/);
  assert.ok(r.ex.articleChars >= 1500, `articleChars ${r.ex.articleChars}`);
  assert.ok(r.ms < 8000, `ms ${r.ms}`);
});

// ---- extract.js: the modal lift --------------------------------------------------------------------
test("modal lift: a page under an aria-modal dialog with its siblings aria-hidden reads as the page, not the dialog", async () => {
  const ex = await extractHtml(roomPage(), { url: "https://www.stays.example/rooms/1", extractMode: "markdown" });
  assert.match(ex.extractor, /\+lift$/);
  assert.ok(ex.articleChars >= 1500, `articleChars ${ex.articleChars}`);
  assert.match(ex.content, /140 x 200 cm bed/);
  assert.doesNotMatch(ex.content, /automatically translated/);
  assert.doesNotMatch(ex.content, /\bicon\b/, "aria-hidden nodes inside the content stay dropped");
  assert.match(ex.visibleText, /Translation on/, "the block detectors still see the whole body");
});

test("modal lift does not fire on a page that already reads, nor without a dialog", async () => {
  const plain = await extractHtml(roomPage({ modal: false }), { url: "https://www.stays.example/s/homes", extractMode: "markdown" });
  assert.doesNotMatch(plain.extractor, /lift/);
  assert.match(plain.content, /140 x 200 cm bed/);
  const nd = await extractHtml(roomPage().replace(DIALOG, ""), { url: "https://www.stays.example/rooms/1", extractMode: "markdown" });
  assert.doesNotMatch(nd.extractor, /lift/);
  assert.ok(nd.articleChars < 500, `articleChars ${nd.articleChars}`);
});

// ---- local.js: path-class site memory --------------------------------------------------------------
const MEM = path.join(TMP, "state-mem");
const memSettings = (now) => resolveSettings({}, { stateDir: MEM, siteMemory: true, noLog: true, _now: () => now.t });
const cooldown = () => JSON.parse(fs.readFileSync(path.join(MEM, "local-cooldown.json"), "utf8"));
const HOST = "www.stays.example";
const ROOMS = "https://www.stays.example/rooms/1000000000000002";
const STAYS = "https://www.stays.example/old-town-example-city/stays/apartments";

test("path class: the first route-word segment, the second when the first is a place slug or an id, else /", () => {
  assert.equal(pathClass("https://www.stays.example/rooms/1000000000000002?check_in=2027-09-19"), "/rooms");
  assert.equal(pathClass("https://www.stays.example/rooms/1000000000000003/photos"), "/rooms");
  assert.equal(pathClass(STAYS), "/stays");
  assert.equal(pathClass("https://www.stays.example/s/homes?pinned=1"), "/s");
  assert.equal(pathClass("https://www.stays.example/help/article/475"), "/help");
  assert.equal(pathClass("https://www.stays.example/"), "/");
  assert.equal(pathClass("https://example.com/2026/10/22/a-long-story-slug"), "/");
  assert.equal(pathClass("not a url"), "/");
  assert.equal(memoryKey(HOST, ROOMS), "www.stays.example/rooms");
});

test("memory key: a class not seen yet starts from the per-host entry; its read is kept per class and the host entry is still written", () => {
  const now = { t: T0 };
  const settings = memSettings(now);
  fs.mkdirSync(settings.stateDir, { recursive: true });
  fs.writeFileSync(path.join(MEM, "local-cooldown.json"), JSON.stringify({ [HOST]: { start: "F2", bb: "L3n", readAt: new Date(T0 - HOUR).toISOString(), probeAt: new Date(T0 - 2 * HOUR).toISOString() } }));
  const p1 = siteMemoryPlan(settings, HOST, ROOMS);
  assert.deepEqual([p1.start, p1.bb, p1.probe], ["F2", "L3n", false]);
  rememberRead(settings, HOST, { win: "F2", plan: p1, url: ROOMS });
  let file = cooldown();
  assert.equal(file[`${HOST}/rooms`].start, "F2");
  assert.equal(file[`${HOST}/rooms`].bb, "L3n", "a new class entry starts from the host entry");
  assert.equal(file[HOST].start, "F2", "the host entry is still written");
  // /stays reads at F1 (a probe of the class): its entry keeps no start, so it is not sent back to F2.
  rememberRead(settings, HOST, { win: "F1", plan: { ...p1, probe: true }, url: STAYS });
  file = cooldown();
  assert.ok(file[`${HOST}/stays`], "a class at the defaults is kept while the host entry remembers more");
  assert.equal(file[`${HOST}/stays`].start, undefined);
  assert.equal(siteMemoryPlan(settings, HOST, STAYS).start, "F1");
  assert.equal(siteMemoryPlan(settings, HOST, ROOMS).start, "F2", "/rooms keeps F2 while /stays starts at F1");
  assert.equal(siteMemoryPlan(settings, HOST, "https://www.stays.example/s/homes?x=1").bb, "L3n", "a class never seen falls back to the host entry");
  assert.equal(siteMemoryPlan(settings, HOST).bb, "L3n", "a call without a url still reads the host entry");
});

test("memory key: siteMemoryRetryHours re-probes each class on its own clock", () => {
  const now = { t: T0 };
  const settings = memSettings(now);
  assert.equal(settings.siteMemoryRetryHours, 24);
  now.t = T0 + 21 * HOUR;   // /rooms kept the host entry's clock (probed at T0 - 2 h); /stays was probed at T0
  assert.equal(siteMemoryPlan(settings, HOST, ROOMS).probe, false);
  now.t = T0 + 23 * HOUR;
  const p = siteMemoryPlan(settings, HOST, ROOMS);
  assert.deepEqual([p.start, p.bb, p.probe], ["F1", "L1", true]);
  assert.equal(siteMemoryPlan(settings, HOST, STAYS).probe, false, "each class on its own clock");
  rememberRead(settings, HOST, { win: "F2", plan: p, url: ROOMS });
  assert.equal(Date.parse(cooldown()[`${HOST}/rooms`].probeAt), now.t);
  assert.equal(siteMemoryPlan(settings, HOST, ROOMS).probe, false);
});

test("memory file: every entry keeps the host entry's shape, so a lookup by host and the prune do not trip on a class key", () => {
  const file = cooldown();
  for (const [k, r] of Object.entries(file)) {
    assert.equal(typeof r, "object", k);
    for (const f of ["start", "bb", "readAt", "probeAt"]) if (r[f] !== undefined) assert.equal(typeof r[f], "string", `${k}.${f}`);
    assert.ok(!Number.isNaN(Date.parse(r.readAt)), `${k} readAt`);
  }
  assert.ok(Object.keys(file).every((k) => !k.includes("/") || k.startsWith(`${HOST}/`)), "a class key is host + /class");
});

// ---- the ladder: the hopeless skip and its ceiling, on the seams ------------------------------------
const CF = page("Just a moment...", "<p>Checking your browser before accessing the site.</p>");
const RECAPTCHA = page("Robot check", `<div class="g-recaptcha"></div><p>Please solve the captcha to continue.</p>`);
const THIN = page("App", "<p>hi</p>");
const LADDER = path.join(TMP, "state-ladder");
const html = (body, status = 200) => new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8" } });
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
const lastRow = () => JSON.parse(fs.readFileSync(path.join(LADDER, "calls.jsonl"), "utf8").trim().split("\n").at(-1));

function rig({ f1 = RECAPTCHA, f2Outcome = "challenge:recaptcha", f2Ms = 0, clock, overrides = {} } = {}) {
  const seen = { fetch: 0, f2: 0, urls: [] };
  const deps = {
    credentials: { apiKey: "bb_live_FAKE_0123456789", projectId: "fake-project" },
    localFetchImpl: async () => html(f1),
    localBrowserRung: async () => {
      seen.f2++;
      if (clock) clock.t += f2Ms;
      return { rung: "F2", ok: false, wall: "captcha_unsolved", verdict: f2Outcome.split(":")[0], outcome: f2Outcome, status: 200, ms: f2Ms, next: "BB" };
    },
    trawlFetchImpl: async () => {
      throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    },
    fetchImpl: async (url) => {
      seen.urls.push(String(url));
      if (String(url).endsWith("/v1/fetch")) {
        seen.fetch++;
        return json({ statusCode: 403, headers: { "content-type": "text/html" }, content: CF, contentType: "text/html" });
      }
      return json({}, 404);
    },
    settingsOverrides: { stateDir: LADDER, l3Enabled: false, siteMemory: false, localTrawlUrl: "http://127.0.0.1:9", ...(clock ? { _now: () => clock.t } : {}), ...overrides },
  };
  return { seen, deps };
}

test("hopeless skip: F1 and F2 both recaptcha skips L1 and L2, names the wall and says so", async () => {
  const r = rig();
  const p = await runFetchPlus({ url: "https://hopeless.example/booking" }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:failed:captcha_unsolved");
  assert.equal(r.seen.fetch, 0, `Browserbase Fetch was called: ${r.seen.urls.join(", ")}`);
  assert.match(p.text, /rungs=F1:challenge:recaptcha,F2:challenge:recaptcha/);
  assert.match(p.text, /Ceiling: two free rungs met the same recaptcha wall, so the fetch rungs L1 and L2 were skipped and the call was capped at 45 s/);
  const row = lastRow();
  assert.equal(row.hopeless, "recaptcha");
  assert.equal(row.cap, 45);
});

test("the home guards are untouched: the second call inside the host's cooldown skips the free rungs and climbs from L1, uncapped", async () => {
  const r = rig();
  const p = await runFetchPlus({ url: "https://hopeless.example/another" }, {}, {}, r.deps);
  assert.equal(r.seen.f2, 0);
  assert.equal(r.seen.fetch, 2);
  assert.equal(p.extractor, "fetch-plus:failed:captcha_unsolved");
  assert.match(p.text, /rungs=L1:challenge:challenge_title,L2:challenge:challenge_title/);
  assert.doesNotMatch(p.text, /Ceiling:/);
  assert.equal(lastRow().hopeless, undefined);
});

test("not hopeless: a thin F1 and a recaptcha F2 still climb through L1 and L2", async () => {
  const r = rig({ f1: THIN });
  const p = await runFetchPlus({ url: "https://mixed.example/page" }, {}, {}, r.deps);
  assert.equal(r.seen.fetch, 2);
  assert.equal(p.extractor, "fetch-plus:failed:captcha_unsolved");
  assert.doesNotMatch(p.text, /Ceiling:/);
  assert.equal(lastRow().hopeless, undefined);
});

test("Cloudflare's title and its script marker are one kind, so F1 challenge_title plus F2 cloudflare is hopeless", async () => {
  const r = rig({ f1: CF, f2Outcome: "challenge:cloudflare" });
  const p = await runFetchPlus({ url: "https://cf.example/page" }, {}, {}, r.deps);
  assert.equal(r.seen.fetch, 0);
  assert.match(p.text, /Ceiling: two free rungs met the same cloudflare wall/);
});

test("the ceiling: with hopelessMaxSeconds spent in F2, nothing else runs and the site's wall is named, not timeout", async () => {
  const clock = { t: 1_700_000_000_000 };
  const r = rig({ f2Ms: 44_500, clock, overrides: { l3Enabled: true } });
  const p = await runFetchPlus({ url: "https://capped.example/page" }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:failed:captcha_unsolved");
  assert.equal(r.seen.urls.length, 0, `Browserbase was called: ${r.seen.urls.join(", ")}`);
  const row = lastRow();
  assert.equal(row.hopeless, "recaptcha");
  assert.ok(row.ms >= 44_500 && row.ms < 46_000, `ms ${row.ms}`);
  assert.match(p.text, /capped at 45 s/);
});

// ---- a Bookings page, read as a guest (need 21, 2026-10-24) ------------------------------------------
// The wall a room met: a Bookings with me link without `anonymous` sends a reader to Microsoft's sign-in, so every rung
// met login_required on a page anyone may book. Microsoft's own share links carry the flag, and the ladder adds it.
const BOOKING = "https://outlook.office.com/bookwithme/user/0a1b2c3d4e5f@example/meetingtype/AbC123?ep=mlink";
const GUEST = "https://outlook.office.com/bookwithme/user/0a1b2c3d4e5f@example/meetingtype/AbC123?anonymous&ep=mlink";
const SIGNIN = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=example";
const SLOTS = page("Book time with Example Person", `<main><h1>Example Person</h1><h2>30 minute call</h2>${Array.from({ length: 12 }, (_, i) => `<p>Thursday, open at ${9 + (i % 8)}:${i % 2 ? "30" : "00"}: a 30 minute call on the example meeting app, booked as a guest with a name and an email address, no account needed.</p>`).join("")}</main>`);
const SIGNIN_PAGE = page("Sign in to your account", `<form><input type="email" name="loginfmt"><input type="password" name="passwd"><p>Sign in to continue.</p></form>`);

function bookingsRig({ guestOpen = true } = {}) {
  const asked = [];
  const r = rig({ overrides: { stateDir: path.join(TMP, "state-bookings") } });
  r.deps.localFetchImpl = async (url) => {
    asked.push(String(url));
    if (String(url).startsWith("https://login.microsoftonline.com/")) return html(SIGNIN_PAGE);
    if (guestOpen && /[?&]anonymous(&|=|$)/.test(String(url))) return html(SLOTS);
    return new Response("", { status: 302, headers: { location: SIGNIN } });
  };
  return { ...r, asked };
}

test("a Bookings with me link gets the anonymous flag its share links carry, first; any other link is left alone", () => {
  assert.equal(bookingsGuestUrl(BOOKING), GUEST);
  assert.equal(bookingsGuestUrl("https://outlook.office.com/bookwithme/user/0a1b@example"), "https://outlook.office.com/bookwithme/user/0a1b@example?anonymous");
  assert.equal(bookingsGuestUrl("https://bookings.cloud.microsoft/bookwithme/user/0a1b@example?ep=pcard"), "https://bookings.cloud.microsoft/bookwithme/user/0a1b@example?anonymous&ep=pcard");
  for (const same of [
    "https://outlook.office365.com/bookwithme/user/0a1b@example?anonymous&ep=pcard", // a share link as Microsoft writes it
    "https://outlook.office.com/BookWithMe/user/0a1b@example?Anonymous=1", // the flag already there, in any case
    "https://outlook.office.com/mail/inbox", // a Microsoft page that is not a Bookings page
    "https://bookings.example/bookwithme/user/0a1b@example", // the same path on another site
    "not a url",
  ]) assert.equal(bookingsGuestUrl(same), same);
  assert.ok(isBookingsPage("https://outlook.office365.us/bookwithme/user/0a1b@example"));
  assert.ok(!isBookingsPage("https://notoffice.com/bookwithme/user/0a1b@example"));
});

test("the wall need 21 met: the bare link lands on Microsoft's sign-in; the ladder reads the guest view at F1 instead", async () => {
  const r = bookingsRig();
  const bare = await localFetchRung({ url: BOOKING, settings: resolveSettings({}, { stateDir: path.join(TMP, "state-bookings") }), extractMode: "markdown", timeoutMs: 5000, fetchImpl: r.deps.localFetchImpl });
  assert.equal(bare.verdict, "login"); // what every rung met, as the room filed it
  r.asked.length = 0;
  const p = await runFetchPlus({ url: BOOKING }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:F1");
  assert.match(p.text, /Thursday, open at 9:00/);
  assert.equal(p.finalUrl, GUEST);
  assert.deepEqual(r.asked, [GUEST]); // the bare link is never asked, so the sign-in never comes up
  assert.match(p.warning, /read this Bookings page as a guest, in the anonymous view its share links open, with no sign-in/);
  assert.equal(r.seen.f2 + r.seen.urls.length, 0, "no other rung ran");
});

test("a Bookings page whose guest view still asks for a sign-in is named so, with no saved-login advice", async () => {
  const r = bookingsRig({ guestOpen: false });
  const p = await runFetchPlus({ url: BOOKING }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:failed:login"); // F1's name for it; L3, when it runs, says login_required
  assert.match(p.text, /retry=no/);
  assert.match(p.text, /What stopped it: This Bookings page asked for a sign-in even in its guest view \(anonymous\): its owner lets only signed-in people book\./);
  assert.match(p.text, /Next move: Ask the page's owner for the share link of a public meeting type, or for the open times in words\./);
  assert.doesNotMatch(p.text, /fetch-plus context/);
  assert.deepEqual(r.asked, [GUEST, SIGNIN]);
  const other = await runFetchPlus({ url: "https://portal.example/reports" }, {}, {}, r.deps); // any other login wall reads as before
  assert.equal(other.extractor, "fetch-plus:failed:login");
  assert.doesNotMatch(other.text, /Bookings page/);
});
