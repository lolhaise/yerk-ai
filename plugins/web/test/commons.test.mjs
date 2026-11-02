// A House of Commons written question's answer (need 37, 2026-11-02), on fixtures: no network. The wall a room met: a plain
// read of a written question's page is the question alone, since each response's sections load after the page opens, from
// a request the page's own script makes with the anti-forgery token in its head and the cookie the page set; without the
// pair the site sends the request to its error page with StatusCode=400 (all read live on 2026-11-02, and mirrored by the
// fake site below). The ladder's seams stand in for this machine (deps.localFetchImpl) and Browserbase (deps.fetchImpl);
// the Browserbase key is fake and nothing is spent. Every name, number and date in here is an example (decision 22).
// Run from the plug-in folder: npm test.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { writtenQuestion } from "../lib/fetch/commons.js";
import { extractHtml } from "../lib/fetch/extract.js";
import { runFetchPlus } from "../lib/fetch/ladder.js";
import { _test as local, rememberRead } from "../lib/fetch/local.js";
import { flushPending, resolveSettings } from "../lib/fetch/util.js";
import { webFetch } from "../lib/tools.mjs";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "yerk-web-commons-"));
after(async () => {
  await Promise.race([flushPending(), new Promise((r) => setTimeout(r, 2000).unref())]);
  fs.rmSync(TMP, { recursive: true, force: true });
});

const PAGE = "https://www.ourcommons.ca/written-questions/99-1/q-101";
const TOKEN = "CfDJ8-example-request-token";
const COOKIE = ".AspNetCore.Antiforgery.Example=example-cookie-value";
const QUESTION = `With regard to the example grants program, since 2028: (a) how much has been delivered under each of its (i) first stream, (ii) second stream, (iii) third stream, to date; (b) for each stream in (a), what reviews, if any, have been conducted to ensure that (i) the funding was used as intended, (ii) the program gave value for money; and (c) for each review in (b), (i) who conducted it, (ii) when was it done, (iii) what were the results? ${"The question goes on with its details. ".repeat(4)}`;

// The question's page as the House's site serves it: the token in its head, the question's details, the response's tab
// (its sessional paper number written as the live page writes it, the quotes escaped and the attribute unquoted), the
// sections it lists, and the empty panel the page's script fills once it has opened.
const attr = (id, label, value) => `<div id="attribute-${id}"><div class="attribute-icon"></div><div><div class="label">${label}</div><div class="value">${value}</div></div></div>`;
const status = ({ label, value }) => `<div id="attribute-status"><div class="attribute-icon"></div><div class="status"><div class="label">${label}</div><div class="value">${value}</div></div></div>`;
function questionPage({ number = "Q-101", stat = { label: "Answered", value: "January 9, 2030" }, tabs = true, library = false } = {}) {
  const tab = tabs ? `<ul id="response-tabs"><li><div class="response-top-fragment d-none" data-response-id="90000001"><div id="responseTopFragment-90000001"> </div></div>
    <button class="nav-link active" id="tab-90000001" data-government-response-id="90000001" data-sessional-paper-number=&quot;8555-991-101&quot; type="button" role="tab">
    <div class="response-type">Response</div><div class="response-date">January 9, 2030</div><span class="badge d-none">0</span></button></li></ul>
    <div id="sections-combobox"><ul class="dropdown-menu">${[["cov", "Cover page"], ["ABC", "Department of Examples"], ["XYZ", "Department of Placeholders"]].map(([c, n]) => `<li><a class="dropdown-item" href="#" data-section-code="${c}">${n}</a></li>`).join("")}</ul></div>
    <div id="scrollable-content"><div id="file-content"></div></div>
    <nav id="government-response-sections-container"><a id="full-text-sessional-paper-preview" href="${PAGE}/90000001">Sessional paper No. <span id="sessional-paper-number">8555-991-101</span></a></nav>` : "";
  const lib = library ? `<section id="written-question-responses"><div class="responses"><a class="btn btn-primary response lop lop-btn" href="https://library.example/search?query=8555-981-7"><div>Response</div><div class="number-code"><span class="sessional-paper-number">8555-981-7</span></div><div>May 4, 2029</div></a></div></section>` : "";
  return `<!doctype html><html><head><title>${number} (99-1) - Written Questions - House of Commons of Canada</title><meta name="request-verification-token" content="${TOKEN}"></head><body>
    <nav>${"Parliamentary Business Committees Members Participate ".repeat(30)}</nav>
    <main><section id="written-question-profile"><h1 class="answered"><span class="question-number">${number}</span><span class="question-session">99th Parliament, 1st session</span></h1>
    <div class="question-attributes-section">${attr("asked-by", "Asked by", "Example Member (Example Riding)")}${attr("date-asked", "Date asked", "October 1, 2029")}${status(stat)}</div>
    <div class="question-text"><div class="content"><div class="text">${QUESTION}</div></div></div></section>
    <section id="written-question-responses-details">${tab}${lib}</section></main>
    <footer>Top of page Senate Library of Parliament</footer>${"<script>/* the page's script */</script>".repeat(6)}</body></html>`;
}

// The response's fragments as the site sends them: one JSON object a line, "main" every section, and "abc-1.1" the rows
// of the table the main fragment leaves empty, with a spinner, for the page's script to fill.
const MAIN = `<div class="section-content" data-section-code="cov"><div id="Q-101-cover-page" class="coverpage response"><div class="stamp"><img src="/written-questions/report-cover-page/emblem.png" alt="House of Commons"><div class="tabled-on"><div>Tabled on</div><div><strong>January 9, 2030</strong></div></div></div><div>Question number</div><div>Q-101</div></div></div>`
  + `<div class="section-content" data-section-code="ABC"><div class="response"><h3>Examples Canada</h3><p>Reply by: the Minister of Examples</p><h4>(a) how much has been delivered under each stream?</h4><p>The example grants program delivered the amounts in the table below.</p><div class="table-responsive"><table class="table" data-id="abc-1"><thead><tr><th><p>Stream</p></th><th><p>Delivered</p></th></tr></thead><tbody></tbody></table><div class="text-center" data-id="spinner-table-abc-1"><div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div></div></div><h4>(b) what reviews have been conducted?</h4><p>An internal audit of the first stream was completed in 2029.</p></div></div>`
  + `<div class="section-content" data-section-code="XYZ"><div class="response"><h3>Placeholders Canada</h3><p>Reply by: the Minister of Placeholders</p><p>The placeholder office delivered no funding under the program.</p></div></div>`;
const ROWS = "<tr><td><p>First stream</p></td><td><p>&nbsp; 1,000 units </p></td></tr><tr><td><p>Second stream</p></td><td><p>&nbsp; 250 units </p></td></tr>";
const NDJSON = [{ key: "main", content: MAIN }, { key: "abc-1.1", content: ROWS }].map((o) => JSON.stringify(o)).join("\n") + "\n";

// The site: the page sets its cookie; the answer comes only to a request carrying the page's token and that cookie, and
// any other request goes to the error page, the status in its address, as live.
function site({ page = questionPage(), cookie = true, answer = NDJSON } = {}) {
  const asked = [];
  const answerAsks = [];
  const localFetchImpl = async (url, init) => {
    asked.push(String(url));
    const u = new URL(url);
    if (/\/ajax\/file-content$/.test(u.pathname)) {
      answerAsks.push({ url: String(url), headers: init.headers, redirect: init.redirect });
      if (init.headers.requestverificationtoken !== TOKEN || !String(init.headers.cookie || "").split("; ").includes(COOKIE)) {
        return new Response("", { status: 302, headers: { location: `/ErrorPage/Default.aspx?Url=${encodeURIComponent(String(url))}&StatusCode=400` } });
      }
      return new Response(answer, { status: 200, headers: { "content-type": "application/json; charset=utf-8" } });
    }
    return new Response(page, { status: 200, headers: { "content-type": "text/html; charset=utf-8", ...(cookie ? { "set-cookie": `${COOKIE}; path=/written-questions; samesite=strict; httponly` } : {}) } });
  };
  return { asked, answerAsks, localFetchImpl };
}

let runs = 0;
function ladder(s, extra = {}) {
  const state = path.join(TMP, `state-${++runs}`);
  const paid = [];
  const f2 = [];
  const deps = {
    credentials: { apiKey: "bb_live_FAKE_0123456789", projectId: "fake-project" },
    localFetchImpl: s.localFetchImpl,
    localBrowserRung: async (a) => {
      f2.push(a.url);
      return { rung: "F2", ok: true, status: 200, finalUrl: a.url, ctype: "text/html", ex: { title: "Q-101", content: QUESTION }, ms: 1, outcome: "ok", verdict: "ok" };
    },
    fetchImpl: async (url) => {
      paid.push(String(url));
      return new Response("{}", { status: 404, headers: { "content-type": "application/json" } });
    },
    settingsOverrides: { stateDir: state, l3Enabled: false, localTrawlUrl: "http://127.0.0.1:9", ...extra },
  };
  const rows = () => fs.readFileSync(path.join(state, "calls.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  return { deps, paid, f2, state, rows };
}

test("the wall need 37 met: a plain read of a written question's page is the question alone, and the site refuses its answer to a request without the page's token and cookie", async () => {
  const ex = await extractHtml(questionPage(), { url: PAGE, extractMode: "markdown" });
  assert.match(ex.content, /example grants program/);
  assert.doesNotMatch(ex.content, /Examples Canada|First stream|1,000 units/); // the answer is in no part of the page as served
  const s = site();
  const bare = await s.localFetchImpl(`${PAGE}/90000001/ajax/file-content?section=`, { headers: {}, redirect: "manual" });
  assert.equal(bare.status, 302);
  assert.match(bare.headers.get("location"), /StatusCode=400$/);
});

test("F1 asks for a written question's answer as its page does after it opens, with the page's token and cookie, and every section and table row comes under the question, free", async () => {
  local.resetGuards();
  const s = site();
  const r = ladder(s);
  const p = await runFetchPlus({ url: PAGE }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:F1", p.text);
  assert.deepEqual(s.asked, [PAGE, `${PAGE}/90000001/ajax/file-content?section=`]);
  const ask = s.answerAsks[0];
  assert.equal(ask.headers.requestverificationtoken, TOKEN);
  assert.equal(ask.headers.cookie, COOKIE); // the name and value only, never the cookie's path or flags
  assert.equal(ask.headers.referer, PAGE);
  assert.equal(ask.headers["sec-fetch-mode"], "cors");
  assert.equal(ask.redirect, "manual");
  assert.match(p.text, /^With regard to the example grants program/);
  assert.match(p.text, /\*\*Q-101\*\*: Asked by: Example Member \(Example Riding\); Date asked: October 1, 2029; Answered: January 9, 2030/);
  assert.match(p.text, /## Response, January 9, 2030, sessional paper 8555-991-101/);
  assert.match(p.text, /3 sections: Cover page \(cov\), Department of Examples \(ABC\), Department of Placeholders \(XYZ\)\. One alone: this address with \?section= and its code\./);
  assert.match(p.text, /### Department of Examples \(ABC\)\n\n#### Examples Canada\n\nReply by: the Minister of Examples\n\n##### \(a\) how much/);
  assert.match(p.text, /\| Stream \| Delivered \|\n\| --- \| --- \|\n\| First stream \| 1,000 units \|\n\| Second stream \| 250 units \|/);
  assert.match(p.text, /An internal audit of the first stream was completed in 2029\./);
  assert.match(p.text, /### Department of Placeholders \(XYZ\)[\s\S]*delivered no funding under the program/);
  assert.ok(p.text.indexOf("Tabled on") < p.text.indexOf("Examples Canada") && p.text.indexOf("Examples Canada") < p.text.indexOf("Placeholders Canada"), "the page's order");
  assert.doesNotMatch(p.text, /Loading\.\.\.|emblem\.png/);
  assert.equal(p.warning, "fetch-plus read this written question's answer as its page asks for it after it opens: 1 response, 3 sections below the question");
  assert.deepEqual(r.paid, [], "no paid rung ran");
  assert.deepEqual(r.f2, [], "no browser was needed");
  const row = r.rows().at(-1);
  assert.equal(row.answers, "read");
  assert.equal(row.rungs, "F1:ok");
});

test("a section in the address, the page's own way to show one, keeps that section in any case, and the rest are named with their codes", async () => {
  local.resetGuards();
  const s = site();
  const p = await runFetchPlus({ url: `${PAGE}?section=xyz` }, {}, {}, ladder(s).deps);
  assert.equal(p.extractor, "fetch-plus:F1");
  assert.equal(s.answerAsks[0].url, `${PAGE}/90000001/ajax/file-content?section=`, "the whole response is asked for, as the page asks");
  assert.match(p.text, /Its section XYZ below, as the address asks; all 3 sections: Cover page \(cov\), Department of Examples \(ABC\), Department of Placeholders \(XYZ\); every one with no section in the address\./);
  assert.match(p.text, /delivered no funding under the program/);
  assert.doesNotMatch(p.text, /Examples Canada|Tabled on/);
  assert.match(p.warning, /1 response, 1 section below the question$/);
  const none = await runFetchPlus({ url: `${PAGE}?section=QQQ` }, {}, {}, ladder(site()).deps);
  assert.match(none.text, /3 sections, none of them QQQ: Cover page \(cov\)/); // a section the response does not have: every one, said so
  assert.match(none.text, /Examples Canada[\s\S]*Placeholders Canada/);
});

test("a request the site refuses is named with its status, and the read says it is the question alone; no paid rung runs for it", async () => {
  local.resetGuards();
  const s = site({ cookie: false }); // the page set no cookie, so the answer's request goes without one, as a room's fetch would
  const r = ladder(s);
  const p = await runFetchPlus({ url: PAGE }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:F1");
  assert.equal(p.warning, "this written question's answer did not load: the page's request for it answered HTTP 400, so the text below is the question alone");
  assert.match(p.text, /## Response, January 9, 2030, sessional paper 8555-991-101\n\nNot read: the page's request for it answered HTTP 400\./);
  assert.doesNotMatch(p.text, /Examples Canada/);
  assert.deepEqual(r.paid, []);
  assert.equal(r.rows().at(-1).answers, "http_400");
  const torn = await runFetchPlus({ url: PAGE }, {}, {}, ladder(site({ answer: "<!doctype html><p>Sorry</p>" })).deps);
  assert.equal(torn.warning, "this written question's answer did not load: the page's request for it came back with no answer in it, so the text below is the question alone");
});

test("a question still awaiting its response says so with its due date, an older one whose response is a Library of Parliament paper names it and its link, and a page that is no question's says so; none makes a request for an answer", async () => {
  local.resetGuards();
  const waiting = site({ page: questionPage({ stat: { label: "Awaiting response", value: "February 20, 2030" }, tabs: false }) });
  const p = await runFetchPlus({ url: PAGE }, {}, {}, ladder(waiting).deps);
  assert.equal(p.extractor, "fetch-plus:F1");
  assert.equal(p.warning, "this written question has no response tabled yet: Awaiting response, February 20, 2030");
  assert.match(p.text, /Awaiting response: February 20, 2030[\s\S]*No response is tabled for this question yet/);
  assert.equal(waiting.answerAsks.length, 0);
  const old = site({ page: questionPage({ stat: { label: "Answered", value: "May 4, 2029" }, tabs: false, library: true }) });
  const q = await runFetchPlus({ url: "https://www.ourcommons.ca/written-questions/98-1/q-7" }, {}, {}, ladder(old).deps);
  assert.equal(q.warning, "this written question's response is a sessional paper at the Library of Parliament, linked below the question");
  assert.match(q.text, /The response is not on this page: it is a sessional paper the Library of Parliament holds\.\n\n- Response 8555-981-7 May 4, 2029: https:\/\/library\.example\/search\?query=8555-981-7/);
  assert.equal(old.answerAsks.length, 0);
  const notice = site({ page: `<!doctype html><html><head><title>Site maintenance</title></head><body><main><h1>Site maintenance</h1>${"<p>The site is down for scheduled maintenance tonight and will be back soon.</p>".repeat(12)}</main></body></html>` });
  const m = await runFetchPlus({ url: PAGE }, {}, {}, ladder(notice).deps);
  assert.equal(m.warning, "this address did not serve a written question's page (no question number or details), so no answer was asked for");
  assert.doesNotMatch(m.text, /No response is tabled/);
  assert.equal(notice.answerAsks.length, 0);
});

test("only F1 asks for the answer: site memory never skips it on a written question's page, and a read at another rung says it may hold the question alone", async () => {
  local.resetGuards();
  const s = site();
  const r = ladder(s);
  const settings = resolveSettings({}, r.deps.settingsOverrides);
  rememberRead(settings, "www.ourcommons.ca", { win: "F2", plan: null, url: PAGE }); // the last read of this path was the browser's
  const p = await runFetchPlus({ url: PAGE }, {}, {}, r.deps);
  assert.equal(p.extractor, "fetch-plus:F1");
  assert.equal(s.answerAsks.length, 1);
  local.resetGuards();
  const down = ladder({ localFetchImpl: async () => { throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNRESET" } }); } });
  const viaF2 = await runFetchPlus({ url: PAGE }, {}, {}, down.deps);
  assert.equal(viaF2.extractor, "fetch-plus:F2");
  assert.match(viaF2.warning, /this is a House of Commons written question: its answer loads after the page opens and only the plain read from this machine \(F1\) asks for it, so this read at F2 may hold the question alone/);
  local.resetGuards();
  const other = await runFetchPlus({ url: "https://news.example/story" }, {}, {}, ladder(site()).deps); // any other page: no word of it
  assert.doesNotMatch(String(other.warning ?? ""), /written question/);
});

test("a written question's address: the House's two sites and their folders, the full text view's response and the section; any other is none", () => {
  assert.deepEqual(writtenQuestion(`${PAGE}?section=ABC`), { page: PAGE, response: "", section: "ABC" });
  assert.deepEqual(writtenQuestion("https://ourcommons.ca/Written-Questions/99-1/Q-101/90000001/"), { page: "https://ourcommons.ca/written-questions/99-1/q-101", response: "90000001", section: "" });
  assert.deepEqual(writtenQuestion("https://www.noscommunes.ca/questions-ecrites/99-1/q-101"), { page: "https://www.noscommunes.ca/questions-ecrites/99-1/q-101", response: "", section: "" });
  for (const no of [
    "https://www.ourcommons.ca/questions-ecrites/99-1/q-101", // the French folder on the English site
    "https://www.ourcommons.ca/written-questions/questions", // the search page
    "https://www.ourcommons.ca/written-questions/99-1/q-101/90000001/ajax/file-content", // the request itself
    "https://ourcommons.example/written-questions/99-1/q-101",
    "ftp://www.ourcommons.ca/written-questions/99-1/q-101",
    "not a url",
  ]) assert.equal(writtenQuestion(no), null, no);
});

test("web_fetch hands a room the answer under the question with the line that says how it was read, and the full text view's address reads the same", async () => {
  const state = path.join(TMP, "tools-state");
  const at = { private: "", state, web: path.join(state, "web"), room: "test-room" }; // no key: the free rungs alone
  const read = async (url, s) => {
    local.resetGuards();
    const { localFetchImpl, localBrowserRung, fetchImpl } = ladder(s).deps;
    return (await webFetch({ url }, { deps: { localFetchImpl, localBrowserRung, fetchImpl, settingsOverrides: { l3Enabled: false } }, at })).content[0].text;
  };
  const t = await read(PAGE, site());
  const lines = t.split("\n");
  assert.match(lines[0], /^web_fetch read https:\/\/www\.ourcommons\.ca\/written-questions\/99-1\/q-101 at F1 \(a plain fetch from this machine, free\): HTTP 200, \d+ characters\.$/);
  assert.ok(lines.includes("fetch-plus read this written question's answer as its page asks for it after it opens: 1 response, 3 sections below the question"), t.slice(0, 600));
  assert.match(t, /First stream \| 1,000 units/);
  const full = site();
  assert.match(await read(`${PAGE}/90000001`, full), /Placeholders Canada/);
  assert.deepEqual(full.answerAsks.map((a) => a.url), [`${PAGE}/90000001/ajax/file-content?section=`]);
});
