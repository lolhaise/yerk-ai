// A House of Commons written question's answer (Yerk need 37, 2026-11-02). The question's page,
// www.ourcommons.ca/written-questions/<parliament>-<session>/q-<n> (the French site's noscommunes.ca/questions-ecrites/
// the same), carries the question; each response tabled for it is a tab whose sections, a cover page and then one per
// federal institution, the page's own script asks for once the page has opened: <page>/<response id>/ajax/file-content,
// sent with the anti-forgery token in the page's head and the cookie the page set, without which the site answers 400
// (read live 2026-11-02). So a plain read had the question alone, and a browser at most the one section it showed.
// F1 (local.js) hands the page here, and this asks as the page does: one request a response, for every section and the
// table rows the page fills in after, as markdown under the question. A `section` in the address, the page's own way to
// show one, keeps that one. A question still awaiting its response says so with the date it is due, and an older one
// whose response is a sessional paper at the Library of Parliament names the paper and its link. Nothing is clicked or sent.
import { domOf, markdownOf, markdownToText } from "./extract.js";

// The House's two sites, each with its folder for written questions.
const SITES = { "ourcommons.ca": "written-questions", "noscommunes.ca": "questions-ecrites" };

// The question's page, the response its address names (the page's full text view) and the section it asks for, else null.
export function writtenQuestion(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  const folder = SITES[u.hostname.toLowerCase().replace(/^www\./, "")];
  const m = folder && /^https?:$/.test(u.protocol) ? new RegExp(`^/${folder}/(\\d+-\\d+)/q-(\\d+)(?:/(\\d+))?/?$`, "i").exec(u.pathname) : null;
  if (!m) return null;
  return { page: `${u.origin}/${folder}/${m[1]}/q-${m[2]}`, response: m[3] || "", section: (u.searchParams.get("section") || "").trim() };
}

const words = (el) => String(el?.textContent ?? "").replace(/\s+/g, " ").trim();
const unquote = (s) => String(s ?? "").replace(/^["'\s]+|["'\s]+$/g, "");

// What the page says of its answer: the token its script sends, each response's tab (id, kind, date, sessional paper),
// the names its section lists give, the question's details line, and the Library of Parliament's papers for an older one.
export function answerPlan(document, wq) {
  const token = document.querySelector('meta[name="request-verification-token"]')?.getAttribute("content") || "";
  const responses = [];
  for (const el of document.querySelectorAll("[data-government-response-id]")) {
    const id = el.getAttribute("data-government-response-id") || "";
    if (/^\d+$/.test(id) && !responses.some((r) => r.id === id)) {
      responses.push({ id, kind: words(el.querySelector(".response-type")) || "Response", date: words(el.querySelector(".response-date")), paper: unquote(el.getAttribute("data-sessional-paper-number")) });
    }
  }
  if (wq?.response && !responses.some((r) => r.id === wq.response)) responses.push({ id: wq.response, kind: "Response", date: "", paper: "" });
  const lone = words(document.querySelector("#sessional-paper-number"));
  if (responses.length === 1 && !responses[0].paper && lone) responses[0].paper = lone;
  const names = new Map();
  for (const el of document.querySelectorAll("[data-section-code]")) {
    const code = el.getAttribute("data-section-code") || "";
    if (code && words(el) && !names.has(code.toLowerCase())) names.set(code.toLowerCase(), words(el));
  }
  const facts = [...document.querySelectorAll('.question-attributes-section [id^="attribute-"]')]
    .map((el) => [words(el.querySelector(".label")), words(el.querySelector(".value"))])
    .filter(([label, value]) => label && value)
    .map(([label, value]) => `${label}: ${value}`);
  const number = words(document.querySelector(".question-number"));
  const details = facts.length ? `${number ? `**${number}**: ` : ""}${facts.join("; ")}` : "";
  const status = words(document.querySelector("#attribute-status .label"));
  const due = words(document.querySelector("#attribute-status .value"));
  // Each block of the link a word group of its own, so a page served without whitespace between them still reads.
  const library = [...document.querySelectorAll("#written-question-responses a[href].lop")].map((a) => ({ what: [...a.childNodes].map(words).filter(Boolean).join(" "), href: a.getAttribute("href") }));
  return { token, responses, names, details, status, due, library, page: Boolean(number || facts.length) };
}

// A request as the page's own script makes it once the page has opened: same origin, the token, the page's cookie.
function answerHeaders(headers, token, cookies, referer) {
  const { "upgrade-insecure-requests": _u, "sec-fetch-user": _s, ...h } = headers || {};
  const cookie = (cookies || []).map((c) => String(c).split(";")[0].trim()).filter((c) => c.includes("=")).join("; ");
  return { ...h, accept: "*/*", "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin", referer, requestverificationtoken: token, ...(cookie ? { cookie } : {}) };
}

// One response's fragments: lines of {key, content}, "main" the sections and "<table>.<n>" a table's rows; or its wall.
async function askResponse({ url, headers, fetchImpl, signal, timeoutMs, read }) {
  const sig = signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs);
  let res;
  try {
    res = await fetchImpl(url, { headers, signal: sig, redirect: "manual" });
  } catch (err) {
    if (signal?.aborted) throw err;
    return { wall: err?.name === "TimeoutError" ? "timeout" : "connection_failed" };
  }
  if (res.status !== 200) {
    try {
      await res.body?.cancel();
    } catch {}
    // The site sends a refused request to its error page, the status in that page's address.
    const code = /[?&]StatusCode=(\d{3})\b/i.exec(res.headers.get("location") || "")?.[1];
    return { wall: `http_${code || res.status}` };
  }
  let buf;
  try {
    buf = await read(res);
  } catch (err) {
    if (signal?.aborted) throw err;
    return { wall: err?.code === "TOO_LARGE" ? "too_large" : "connection_failed" };
  }
  const parts = [];
  for (const line of buf.toString("utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      const o = JSON.parse(line);
      if (typeof o?.key === "string" && typeof o?.content === "string") parts.push(o);
    } catch {}
  }
  if (!parts.length) return { wall: "unreadable", bytes: buf.length };
  let missing = [];
  try {
    missing = [].concat(JSON.parse(res.headers.get("x-sections-missing-html") || "[]")).map(String);
  } catch {}
  return { parts, missing, bytes: buf.length };
}

// The response's sections as the page shows them, its tables filled: [{ code, name, md }].
async function sectionsOf(parts, plan, base) {
  const main = parts.find((p) => p.key === "main") || parts[0];
  const doc = await domOf(main.content);
  if (!doc) return null;
  for (const p of parts) {
    const t = /^([\w-]+)\.\d+$/.exec(p.key);
    const body = t && doc.querySelector(`table[data-id="${t[1]}"] tbody`);
    if (body) body.insertAdjacentHTML("beforeend", p.content);
  }
  for (const el of doc.querySelectorAll('[data-id^="spinner-table-"], .coverpage img')) el.remove();
  const found = [...doc.querySelectorAll(".section-content[data-section-code]")];
  if (!found.length) return [{ code: "", name: "", md: markdownOf(doc.body, base) }];
  return found.map((el) => {
    const code = el.getAttribute("data-section-code");
    const name = plan.names.get(code.toLowerCase()) || words(el.querySelector("h1,h2,h3,h4")) || (/^cov$/i.test(code) ? "Cover page" : code);
    return { code, name, md: markdownOf(el, base).replace(/^(#{1,5}) /gm, "#$1 ") }; // its own headings a level under its name
  });
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const SAID = { timeout: "timed out", connection_failed: "could not reach the site", too_large: "came back over the size cap", unreadable: "came back with no answer in it" };
const said = (wall) => (/^http_\d{3}$/.test(wall) ? `answered HTTP ${wall.slice(5)}` : SAID[wall] || wall.replace(/_/g, " "));
const listed = (sections) => sections.map((s) => `${s.name} (${s.code})`).join(", ");

// The page's answer under its question: { text, chars, page, note, outcome }. `note` is the line the read's warning
// carries, `outcome` the call log's word. `headers` are F1's own, `read` F1's capped body read.
export async function readAnswers({ wq, html, cookies, headers, fetchImpl, signal, timeoutMs, read, extractMode = "markdown" }) {
  const doc = await domOf(html);
  if (!doc) return { text: "", chars: 0, page: false, note: "this written question's answer was not read: the page parser is missing", outcome: "no_parser" };
  const plan = answerPlan(doc, wq);
  // Not the question's page as the House serves it (a maintenance or error page at its address): nothing is asked for.
  if (!plan.page && !plan.responses.length) return { text: "", chars: 0, page: false, note: "this address did not serve a written question's page (no question number or details), so no answer was asked for", outcome: "not_question" };
  const out = [];
  let calls = 0;
  let bytes = 0;
  if (plan.details) out.push(plan.details);
  let note;
  let outcome;
  if (!plan.responses.length) {
    if (plan.library.length) {
      out.push(`The response is not on this page: it is a sessional paper the Library of Parliament holds.`, plan.library.map((l) => `- ${l.what}: ${l.href}`).join("\n"));
      note = "this written question's response is a sessional paper at the Library of Parliament, linked below the question";
      outcome = "library";
    } else {
      out.push(`No response is tabled for this question yet${plan.status ? ` (the page says ${plan.status}${plan.due ? `, ${plan.due}` : ""})` : ""}.`);
      note = `this written question has no response tabled yet${plan.status ? `: ${plan.status}${plan.due ? `, ${plan.due}` : ""}` : ""}`;
      outcome = /awaiting/i.test(plan.status) ? "awaiting" : "none";
    }
  } else if (!plan.token) {
    note = "this written question's answer did not load: the page carried no request token for it, so the text below is the question alone";
    outcome = "no_token";
  } else {
    const ask = wq.section.toLowerCase() === "all" ? "" : wq.section.toLowerCase();
    const reads = [];
    for (const r of plan.responses) {
      const got = await askResponse({ url: `${wq.page}/${r.id}/ajax/file-content?section=`, headers: answerHeaders(headers, plan.token, cookies, wq.page), fetchImpl, signal, timeoutMs, read });
      calls++;
      bytes += got.bytes || 0;
      reads.push({ r, got, sections: got.parts ? await sectionsOf(got.parts, plan, wq.page) : null });
    }
    const ok = reads.filter((x) => x.sections);
    const anyAsked = Boolean(ask) && ok.some((x) => x.sections.some((s) => s.code.toLowerCase() === ask));
    let shown = 0;
    for (const { r, got, sections } of reads) {
      const head = `## ${r.kind}${r.date ? `, ${r.date}` : ""}${r.paper ? `, sessional paper ${r.paper}` : ""}`;
      if (!sections) {
        out.push(head, `Not read: the page's request for it ${said(got.wall)}.`);
        continue;
      }
      const mine = anyAsked ? sections.filter((s) => s.code.toLowerCase() === ask) : sections;
      const index = anyAsked
        ? mine.length
          ? `Its section ${mine[0].code} below, as the address asks; all ${plural(sections.length, "section")}: ${listed(sections)}; every one with no section in the address.`
          : `No section ${wq.section} in this one; its ${plural(sections.length, "section")}: ${listed(sections)}.`
        : `${plural(sections.length, "section")}${ask ? `, none of them ${wq.section}` : ""}: ${listed(sections)}. One alone: this address with ?section= and its code.`;
      const gone = got.missing.length ? [`The site says it has no text for: ${got.missing.join(", ")}.`] : [];
      out.push(head, index, ...gone, ...mine.map((s) => [s.name ? `### ${s.name} (${s.code})` : "", s.md].filter(Boolean).join("\n\n")));
      shown += mine.length;
    }
    const failed = reads.filter((x) => !x.sections);
    if (!ok.length) {
      const w = failed[0].got.wall;
      note = `this written question's answer did not load: the page's request for it ${said(w)}, so the text below is the question alone`;
      outcome = w;
    } else {
      note = `fetch-plus read this written question's answer as its page asks for it after it opens: ${failed.length ? `${ok.length} of ${plural(reads.length, "response")}` : plural(ok.length, "response")}, ${plural(shown, "section")} below the question`;
      outcome = failed.length ? "partial" : "read";
    }
  }
  const md = out.filter((s) => String(s).trim()).join("\n\n");
  const text = extractMode === "text" ? markdownToText(md) : md;
  return { text, chars: markdownToText(md).length, page: plan.page, note, outcome, calls, bytes };
}
