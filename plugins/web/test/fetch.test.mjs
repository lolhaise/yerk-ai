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
// A Bookings with me page as a browser gets it with the anonymous flag (read live 2026-11-02): an app that shows "Sign in
// or continue as guest" and fetches its meeting types and open times only on a click on "Continue as guest", the URL
// unchanged; a few hundred visible chars after the click, in an app's large document. Under /stuck/ the click brings
// nothing; under /hang/ the choice screen asks for a script that does not finish, as two did on the live page.
const GUEST_TIMES = ["8:30 AM", "9:00 AM", "9:30 AM", "10:00 AM", "11:00 AM", "12:30 PM", "2:00 PM", "2:30 PM", "3:00 PM", "3:30 PM", "4:00 PM", "4:30 PM", "5:00 PM", "5:30 PM", "6:00 PM"];
const GUEST_VIEW = `<h1>Example Person</h1><p>Booking Page</p><h2>Choose a meeting type</h2><p>update call, 20 min. Book time to connect with me.</p><h2>Available times</h2><p>Today, Monday</p><ul>${GUEST_TIMES.map((t) => `<li><button>${t}</button></li>`).join("")}</ul>`;
const bookingsApp = ({ works = true, hang = false } = {}) => page("Bookings with me", `<div id="app"></div><script>
  const app = document.getElementById("app");
  setTimeout(() => {
    app.innerHTML = '<h1>Example Person</h1><p>Booking Page</p><h2>Sign in or continue as guest</h2><button>Sign in with a Microsoft 365 work or school account</button><button id="guest">Continue as guest</button>';
    ${hang ? `document.head.appendChild(Object.assign(document.createElement("script"), { src: "/bookwithme-hang.js" }));` : ""}
    document.getElementById("guest").onclick = () => { ${works ? `fetch("/bookwithme-data").then((r) => r.text()).then((t) => { app.innerHTML = t; });` : ""} };
  }, 300);
</script><script>/* ${"the app's bundle ".repeat(700)} */</script>`);
const roomPage = ({ modal = true } = {}) => `<!doctype html><html><head><title>Two-bedroom flat with a courtyard</title></head><body><div id="skip-links"${modal ? ' aria-hidden="true"' : ""}><a href="#main">Skip to content</a></div><div id="app"${modal ? ' aria-hidden="true"' : ""}><main id="main">${LISTING}</main></div>${modal ? DIALOG : ""}</body></html>`;

const BROWSER = localBrowserPresent(resolveSettings({}, { stateDir: path.join(TMP, "probe") }));
const needsBrowser = BROWSER ? {} : { skip: "no local browser engine on this machine" };

let server;
let base;
let guestReads = 0;
before(async () => {
  server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    const send = (body, type = "text/html; charset=utf-8", status = 200) => {
      res.writeHead(status, { "content-type": type });
      res.end(body);
    };
    if (u.pathname.startsWith("/rooms/")) return send(roomPage());
    if (u.pathname.startsWith("/bookwithme/")) return send(bookingsApp({ works: !u.pathname.startsWith("/bookwithme/stuck/"), hang: u.pathname.startsWith("/bookwithme/hang/") }));
    switch (u.pathname) {
      case "/bookwithme-data":
        guestReads++;
        return void setTimeout(() => send(GUEST_VIEW), 800);
      case "/bookwithme-hang.js":
        return void setTimeout(() => send("", "application/javascript"), 20000).unref();
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
// As read live on 2026-11-02: a plain read gets the app's shell (HTTP 200) for either link; in a browser the bare link
// ends on Microsoft's sign-in and the flagged one on "Sign in or continue as guest", whose meeting types and open times
// show only after a click on "Continue as guest" (F2's guest step, on the fixture at the top).
const BOOKING = "https://outlook.office.com/bookwithme/user/0a1b2c3d4e5f@example/meetingtype/AbC123?ep=mlink";
const GUEST = "https://outlook.office.com/bookwithme/user/0a1b2c3d4e5f@example/meetingtype/AbC123?anonymous&ep=mlink";
const SIGNIN = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=example";
const APP = "/bookwithme/user/0a1b2c3d4e5f@example/meetingtype/AbC123?anonymous&ep=mlink";
const SHELL = page("Bookings with me", `<div id="app"></div>${Array.from({ length: 6 }, (_, i) => `<script src="/static/app-${i}.js"></script>`).join("")}`);
const CHOICE = "# Example Person\n\nBooking Page\n\n## Sign in or continue as guest"; // F2's read of the choice screen, as the live one was
const SIGNIN_PAGE = page("Sign in to your account", `<form><input type="email" name="loginfmt"><input type="password" name="passwd"><p>Sign in to continue.</p></form>`);

// f2: "app" the machine's browser on the fixture; "thin" the choice screen with nothing clicked; "short" the same screen
// taken as a short read (what L3's short-page rule made of it); "signin" a click that went on to Microsoft's sign-in.
let bookingsRuns = 0;
function bookingsRig({ f2 = "app" } = {}) {
  const asked = [];
  const f2Args = [];
  const r = rig({ overrides: { stateDir: path.join(TMP, `state-bookings-${++bookingsRuns}`) } });
  r.deps.localFetchImpl = async (url) => {
    asked.push(String(url));
    if (String(url).includes("/bookwithme/")) return html(SHELL); // either link, as live
    if (String(url).startsWith("https://login.microsoftonline.com/")) return html(SIGNIN_PAGE);
    return new Response("", { status: 302, headers: { location: SIGNIN } });
  };
  r.deps.localBrowserRung = async (a) => {
    r.seen.f2++;
    f2Args.push(a);
    if (f2 === "app") return { ...(await localBrowserRung({ ...a, url: `${base}${APP}`, settings: f2Settings })), finalUrl: a.url }; // served from 127.0.0.1 in the page's place, so it lands where it was asked (the off-site guard reads it)
    if (f2 === "signin") return { rung: "F2", ok: false, wall: "login", verdict: "login", outcome: "login:url", status: 200, ms: 0, next: "BB", guest: "clicked" };
    const choice = { status: 200, finalUrl: a.url, ctype: "text/html", ex: { content: CHOICE } };
    if (f2 === "short") return { rung: "F2", ok: true, ...choice, outcome: "ok:short", short: true, ms: 0 };
    return { rung: "F2", ok: false, wall: "thin", verdict: "thin", outcome: "thin:js_shell", status: 200, ms: 0, next: "BB", ex: choice.ex, partial: choice };
  };
  return { ...r, asked, f2Args };
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

test("F2 on a Bookings page clicks Continue as guest and reads the meeting types and open times the click brings; without the ladder's word it clicks nothing", needsBrowser, async () => {
  const before = guestReads;
  const plain = await localBrowserRung({ url: `${base}${APP}`, settings: f2Settings, extractMode: "markdown", budgetMs: 30000 });
  assert.equal(plain.ok, false, "the choice screen is no read"); // all F2 did before the guest step, as on the real page
  assert.match(plain.partial.ex.content, /Sign in or continue as guest/);
  assert.equal(plain.guest, undefined);
  assert.equal(guestReads, before, "no click");
  const r = await localBrowserRung({ url: `${base}${APP}`, settings: f2Settings, extractMode: "markdown", budgetMs: 30000, guest: true });
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.equal(r.guest, "clicked");
  assert.equal(r.outcome, "ok:short"); // a few hundred chars in an app's large document read as thin, and are the answer
  assert.match(r.ex.content, /update call, 20 min/);
  assert.match(r.ex.content, /8:30 AM[\s\S]*6:00 PM/);
  assert.doesNotMatch(r.ex.content, /continue as guest/i);
  assert.equal(guestReads, before + 1);
  assert.ok(r.phases.guest >= 800 && r.phases.guest < 6000, `guest ${r.phases.guest}`);
});

test("F2's guest step on a choice screen whose click brings nothing ends stuck at localReadableWaitMs, and is no read", needsBrowser, async () => {
  const r = await localBrowserRung({ url: `${base}/bookwithme/stuck/0a1b@example?anonymous`, settings: { ...f2Settings, localReadableWaitMs: 2500 }, extractMode: "markdown", budgetMs: 30000, guest: true });
  assert.equal(r.ok, false);
  assert.equal(r.guest, "stuck");
  assert.ok(r.phases.guest >= 2000 && r.phases.guest < 4500, `guest ${r.phases.guest}`);
});

test("the guest step waits on the click's own requests, not on one the choice screen left open", needsBrowser, async () => {
  const r = await localBrowserRung({ url: `${base}/bookwithme/hang/0a1b@example?anonymous`, settings: { ...f2Settings, localReadableWaitMs: 4000 }, extractMode: "markdown", budgetMs: 30000, guest: true });
  assert.equal(r.ok, true, JSON.stringify(r.outcome));
  assert.equal(r.guest, "clicked");
  assert.match(r.ex.content, /8:30 AM/);
  assert.ok(r.phases.guest >= 800 && r.phases.guest < 3000, `guest ${r.phases.guest}`); // the ceiling is 4,000
});

test("the wall need 21 met: the plain read gets the app's shell for either link; F2 clicks Continue as guest and reads the open times", needsBrowser, async () => {
  const shell = await localFetchRung({ url: BOOKING, settings: resolveSettings({}, { stateDir: path.join(TMP, "state-bookings-f1") }), extractMode: "markdown", timeoutMs: 5000, fetchImpl: bookingsRig().deps.localFetchImpl });
  assert.equal(shell.verdict, "thin"); // a plain read is no sign-in and no times: only a browser gets either
  const r = bookingsRig();
  const p = await runFetchPlus({ url: BOOKING }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:F2", p.text);
  assert.match(p.text, /update call, 20 min/);
  assert.match(p.text, /8:30 AM/);
  assert.deepEqual(r.asked, [GUEST]); // the bare link is never asked, so the sign-in never comes up
  assert.deepEqual(r.f2Args.map((a) => [a.url, a.guest]), [[GUEST, true]]);
  assert.match(p.warning, /read this Bookings page as a guest, in the anonymous view its share links open, with no sign-in, past its Continue as guest button/);
  assert.equal(r.seen.urls.length, 0, "no paid rung ran");
});

test("a Bookings page left on its choice screen is no answer, thin or taken for a short read, and no paid rung runs for it", async () => {
  for (const f2 of ["thin", "short"]) {
    const r = bookingsRig({ f2 });
    const p = await runFetchPlus({ url: BOOKING }, {}, {}, r.deps);
    assert.equal(p.extractor, "fetch-plus:failed:guest_choice", f2);
    assert.match(p.text, /retry=maybe/);
    assert.match(p.text, /What stopped it: This Bookings page stayed on Microsoft's "Sign in or continue as guest" screen/);
    assert.match(p.text, /## Sign in or continue as guest/, "the screen itself, as the partial text");
    assert.doesNotMatch(`${p.text}\n${p.warning}`, /read this Bookings page as a guest/);
    assert.equal(r.seen.urls.length, 0, `${f2}: no paid rung ran`);
  }
});

test("a Bookings page whose guest view went on to Microsoft's sign-in is named so, with no saved-login advice and no paid rung", async () => {
  const r = bookingsRig({ f2: "signin" });
  const p = await runFetchPlus({ url: BOOKING }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:failed:login"); // F2's name for it; L3, when it runs, says login_required
  assert.match(p.text, /retry=no/);
  assert.match(p.text, /What stopped it: This Bookings page went to Microsoft's sign-in even in its guest view \(anonymous\)\./);
  assert.match(p.text, /Next move: Ask the page's owner for the share link of a public meeting type, or for the open times in words\./);
  assert.doesNotMatch(p.text, /fetch-plus context/);
  assert.equal(r.seen.urls.length, 0, "no paid rung ran");
  const other = await runFetchPlus({ url: "https://portal.example/reports" }, {}, {}, r.deps); // any other login wall reads as before
  assert.equal(other.extractor, "fetch-plus:failed:login");
  assert.doesNotMatch(other.text, /Bookings page/);
  assert.deepEqual(r.f2Args.map((a) => a.guest), [true], "no guest step for another page");
});

// ---- the off-site landing guard (OpenClaw's fetch-plus, 2026-11-08) -----------------------------------------------
// A browser rung (F2, F3, L3n, L3) that lands on another registrable domain than the one asked for, and than the one F1's
// HTTP redirect chain reached, is refused with the wall offsite_redirect and its text never becomes the page's text. Why: a
// classifieds search page read thin at F1, F2 rendered it, an ad sent the tab to another domain, and a scareware page came
// back as the page with status 200, then site memory remembered F2 as the step that reads that site.
const THIN_RESULTS = page("used lamp in the city | Classifieds", "<main><h1>used lamp</h1><p>0 results. Try another search.</p></main>");
const APP_SHELL = page("App", `<div id="root"></div><script src="/app.js"></script><script src="/vendor.js"></script><script src="/chunk.js"></script><script src="/d.js"></script><script src="/e.js"></script>`);
const SCAM_TEXT = `Windows has detected wiretapping on your device! Your personal data is at risk. Call support now. ${"Download TotalAV to remove the threats found on this PC. ".repeat(20)}`;
const LISTING_TEXT = `Brass floor lamp, $35, pickup only. ${"Great condition, shade included, cash on pickup. ".repeat(20)}`;
const redirect = (to) => new Response("", { status: 301, headers: { location: to } });

let offsiteN = 0;
// A rig per case: its own state dir (site memory and the call log are keyed on it), F1 pages by URL, an F2 that "reads"
// wherever the case says it landed, Browserbase Fetch answering the thin page, L3 off.
function offsiteRig({ pages, landing, landingText = LISTING_TEXT, memory, siteMemory = true } = {}) {
  const state = path.join(TMP, `offsite-${++offsiteN}`);
  fs.mkdirSync(state, { recursive: true });
  if (memory) fs.writeFileSync(path.join(state, "local-cooldown.json"), JSON.stringify(memory));
  const seen = { f2: 0, f1: [] };
  const deps = {
    credentials: { apiKey: "bb_live_FAKE_0123456789", projectId: "fake-project" },
    localFetchImpl: async (url) => {
      seen.f1.push(String(url));
      const hit = pages[String(url)];
      if (!hit) return html("nope", 404);
      return typeof hit === "string" && /^https?:\/\//.test(hit) ? redirect(hit) : html(hit);
    },
    localBrowserRung: async () => {
      seen.f2++;
      return { rung: "F2", ok: true, status: 200, finalUrl: landing, ctype: "text/html", ex: { title: "Landed", content: landingText, visibleText: landingText, articleChars: landingText.length }, outcome: "ok", verdict: "ok", ms: 7 };
    },
    trawlFetchImpl: async () => {
      throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    },
    fetchImpl: async (url) => {
      if (String(url).endsWith("/v1/fetch")) return json({ statusCode: 200, headers: { "content-type": "text/html" }, content: THIN_RESULTS, contentType: "text/html" });
      return json({}, 404);
    },
    settingsOverrides: { stateDir: state, l3Enabled: false, siteMemory, localTrawlUrl: "http://127.0.0.1:9" },
  };
  const lastCall = () => JSON.parse(fs.readFileSync(path.join(state, "calls.jsonl"), "utf8").trim().split("\n").at(-1));
  const memoryFile = () => (fs.existsSync(path.join(state, "local-cooldown.json")) ? JSON.parse(fs.readFileSync(path.join(state, "local-cooldown.json"), "utf8")) : {});
  return { deps, seen, lastCall, memoryFile };
}

const offsiteRun = (url, deps) => runFetchPlus({ url, extractMode: "markdown" }, { config: {} }, {}, deps);

test("the off-site case: F1 thin on a classifieds site, F2 lands on another domain: refused, named, the scam text never returned, nothing remembered", async () => {
  const url = "https://www.classifieds.test/b-city/used-lamp/k0l1700272";
  const { deps, seen, lastCall, memoryFile } = offsiteRig({ pages: { [url]: THIN_RESULTS }, landing: "https://chancespark.biz/landing?clk=9f3a&sub=totalav", landingText: SCAM_TEXT });
  const r = await offsiteRun(url, deps);
  assert.equal(seen.f2, 1);
  assert.equal(r.extractor, "fetch-plus:failed:offsite_redirect");
  assert.doesNotMatch(r.text, /wiretapping|TotalAV/i, "the off-site page's text must not come back as this page");
  assert.match(r.text, /offsite=chancespark\.biz/);
  assert.match(r.text, /Off-site landing: F2 .*chancespark\.biz\/landing/);
  assert.doesNotMatch(r.text, /clk=9f3a/, "the landing's query string (tracking) is not printed");
  assert.match(r.text, /0 results\. Try another search\./, "the partial text is F1's real, thin page");
  assert.equal(new URL(r.finalUrl).hostname, "www.classifieds.test", "finalUrl never points at the off-site page");
  assert.match(r.warning, /wall=offsite_redirect rungs=F1:thin:visible_chars_\d+,F2:offsite:chancespark\.biz,L1:/);
  const row = lastCall();
  assert.equal(row.outcome, "fail");
  assert.equal(row.wall, "offsite_redirect");
  assert.equal(row.offsite, "F2:chancespark.biz");
  const mem = memoryFile();
  assert.equal(Object.keys(mem).filter((k) => k.startsWith("www.classifieds.test")).length, 0, "a refused read is not a win: site memory stays empty");
});

test("an HTTP redirect to another domain is the site's own answer: F1 follows it and reads there, no refusal", async () => {
  const { deps } = offsiteRig({ pages: { "https://short.test/x": "https://news.test/story", "https://news.test/story": page("Rain moves into the city tonight", ARTICLE) }, landing: "https://never.test/" });
  const r = await offsiteRun("https://short.test/x", deps);
  assert.equal(r.extractor, "fetch-plus:F1");
  assert.equal(r.finalUrl, "https://news.test/story");
  assert.match(r.text, /Paragraph 30: the weather office says a band of rain/); // Readability lifts the h1 into the title
});

test("a browser landing on the domain F1's HTTP redirect chain reached is allowed (F1 is the redirect authority)", async () => {
  const { deps, seen } = offsiteRig({ pages: { "https://short.test/y": "https://app.test/page", "https://app.test/page": APP_SHELL }, landing: "https://app.test/page" });
  const r = await offsiteRun("https://short.test/y", deps);
  assert.equal(seen.f2, 1);
  assert.equal(r.extractor, "fetch-plus:F2");
  assert.equal(r.finalUrl, "https://app.test/page");
  assert.match(r.text, /Brass floor lamp/);
  assert.match(r.warning, /read this at F2 after F1:thin/);
});

test("another host of the same registrable domain is the same site: www to m is allowed", async () => {
  const url = "https://www.classifieds.test/b-region/floor-lamp/k0l9004";
  const { deps } = offsiteRig({ pages: { [url]: THIN_RESULTS }, landing: "https://m.classifieds.test/b-region/floor-lamp/k0l9004" });
  const r = await offsiteRun(url, deps);
  assert.equal(r.extractor, "fetch-plus:F2");
  assert.equal(r.finalUrl, "https://m.classifieds.test/b-region/floor-lamp/k0l9004");
});

test("site memory starts the climb at F2, so F1 never ran: an off-site landing has no HTTP truth behind it and is refused; the memory entry is left as it was", async () => {
  const url = "https://www.classifieds.test/b-city/lamp/k0l1700272";
  const iso = new Date().toISOString();
  const { deps, seen, lastCall, memoryFile } = offsiteRig({ pages: { [url]: THIN_RESULTS }, landing: "https://chancespark.biz/other", landingText: SCAM_TEXT, memory: { "www.classifieds.test": { start: "F2", readAt: iso, probeAt: iso } } });
  const r = await offsiteRun(url, deps);
  assert.equal(seen.f1.length, 0, "F1 was skipped by memory");
  assert.equal(seen.f2, 1);
  assert.equal(r.extractor, "fetch-plus:failed:offsite_redirect");
  assert.doesNotMatch(r.text, /wiretapping/i);
  assert.match(r.warning, /rungs=F2:offsite:chancespark\.biz,L1:/);
  const row = lastCall();
  assert.equal(row.mem, "F2/L1");
  assert.equal(row.offsite, "F2:chancespark.biz");
  assert.deepEqual(memoryFile()["www.classifieds.test"], { start: "F2", readAt: iso, probeAt: iso });
});

// A Browserbase browser session with no network: the API answers through deps.fetchImpl, and the page is driven by a
// stand-in for playwright-core (settings.playwrightCorePath), which asks globalThis.mockSessionPage what each session loads.
const MOCK_PLAYWRIGHT = path.join(TMP, "mock-playwright-core.mjs");
fs.writeFileSync(MOCK_PLAYWRIGHT, `export const chromium = {
  async connectOverCDP(connectUrl) {
    let land = { url: "about:blank", title: "", html: "", text: "" };
    const page = {
      route: async () => {},
      on: () => {},
      mainFrame: () => null,
      url: () => land.url,
      waitForLoadState: async () => {},
      goto: async (url) => {
        land = await globalThis.mockSessionPage(connectUrl, url);
        if (land.navError) throw new Error(land.navError);
        return { status: () => land.status ?? 200 };
      },
      evaluate: async (js) => (String(js).includes("textLen") ? { title: land.title, head: land.html, textLen: land.text.length, text: land.text } : { html: land.html, title: land.title, url: land.url, text: land.text }),
    };
    const context = { pages: () => [page], newPage: async () => page };
    return { contexts: () => [context], newContext: async () => context, close: async () => {} };
  },
};
`);

// sessions: (body, n) => what the n-th session loads ({ url, title, html, text } or { navError }); body is the create call's.
function sessionRig({ sessions, overrides = {} }) {
  const state = path.join(TMP, `session-${++offsiteN}`);
  fs.mkdirSync(state, { recursive: true });
  const made = new Map();
  globalThis.mockSessionPage = async (connectUrl) => made.get(connectUrl)();
  const deps = {
    credentials: { apiKey: "bb_live_FAKE_0123456789", projectId: "fake-project" },
    fetchImpl: async (url, init = {}) => {
      const u = String(url);
      if (u.endsWith("/v1/fetch")) return json({ statusCode: 200, headers: { "content-type": "text/html" }, content: THIN_RESULTS, contentType: "text/html" });
      if (u.endsWith("/v1/sessions") && init.method === "POST") {
        const body = JSON.parse(init.body);
        const id = `session-${made.size + 1}`;
        const connectUrl = `wss://connect.example/${id}`;
        const n = made.size;
        made.set(connectUrl, () => sessions(body, n));
        return json({ id, connectUrl }, 201);
      }
      if (/\/v1\/sessions\/[^/]+$/.test(u)) return json({ id: u.split("/").at(-1), status: "COMPLETED", proxyBytes: 0 });
      return json({}, 404);
    },
    settingsOverrides: { stateDir: state, localEnabled: false, siteMemory: false, contextStrategy: "none", usageSyncMinutes: 0, _settleDelaysMs: [0], playwrightCorePath: MOCK_PLAYWRIGHT, ...overrides },
  };
  return { deps, made };
}

const LANDED = { url: "https://chancespark.biz/landing?clk=9f3a", title: "You won", html: page("You won", `<main><p>${SCAM_TEXT}</p><p>${SCAM_TEXT}</p></main>`), text: `${SCAM_TEXT} ${SCAM_TEXT}` };

test("a browser session sent off-site is refused at L3n and L3, and both sessions are still booked: Browserbase bills a session wherever it landed", async () => {
  const { deps, made } = sessionRig({ sessions: () => LANDED });
  const r = await offsiteRun("https://www.classifieds.test/b-city/used-lamp/k0l1700272", deps);
  assert.equal(made.size, 2, "L3n, then L3 with proxies");
  assert.equal(r.extractor, "fetch-plus:failed:offsite_redirect");
  assert.doesNotMatch(r.text, /wiretapping|TotalAV/i);
  assert.match(r.warning, /rungs=L1:thin:visible_chars_\d+,L2:thin:visible_chars_\d+,L3n:offsite:chancespark\.biz,L3:offsite:chancespark\.biz$/);
  assert.deepEqual(r.paid.map((p) => [p.rung, p.proxies]), [["L1", false], ["L2", true], ["L3n", false], ["L3", true]]);
  assert.ok(r.paid.find((p) => p.rung === "L3").bytes >= 1048576, "the proxied session's bytes estimate is booked");
});
