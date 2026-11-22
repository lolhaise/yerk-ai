// An ÖBB journey: the trains, times and fares for a dated trip (Yerk need 136, 2026-11-22). The ticket shop's journey
// page, shop.oebbtickets.at/<lang>/ticket?..., is a script app whose host answers every rung of the ladder (F1 to L3)
// with a captcha, and Scotty's old query page is gone. Read live 2026-11-22: the trains come from Scotty's own timetable
// service, fahrplan.oebb.at/bin/mgate.exe (ÖBB's apps ask it, as hafas-client's oebb profile does), which answers a
// plain request from this machine; the fares only from the shop's API (api/domain/v4/init for a session with no
// account, api/hafas/v1/stations, api/hafas/v4/timetable, api/offer/v1/prices), which the page itself asks and whose
// bot wall refuses this machine's Node (HTTP 403, its "Access temporarily restricted" page). So web_fetch on a link of
// the shop's reads the trains from Scotty and asks the shop for the fares as the page does; a refused fare step is
// said with its status and each train keeps its own shop link, never a wall dodged, a browser, a paid rung or a booking.
const HOSTS = new Set(["shop.oebbtickets.at", "tickets.oebb.at"]);
export const API = "https://shop.oebbtickets.at/api";
export const MGATE = "https://fahrplan.oebb.at/bin/mgate.exe";
const CLIENT = { client: { id: "OEBB", v: "6030600", type: "IPH", name: "oebbPROD-ADHOC" }, ext: "OEBB.1", ver: "1.41", auth: { type: "AID", aid: "OWDL4fE4ixNiPBBm" } };
export const SHAPE = "https://shop.oebbtickets.at/en/ticket?stationOrigName=FROM&stationDestName=TO&outwardDateTime=YYYY-MM-DDTHH:MM";
const COUNT = 5; // trains a read, as the shop's own page lists them
const HEADERS = {
  "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
  accept: "application/json, text/plain, */*",
  channel: "inet",
};

const first = (q, ...keys) => keys.map((k) => (q.get(k) || "").trim()).find(Boolean) || "";

// A link of the shop's: null for any other host; else the trip it names, or what it lacks.
export function oebbJourney(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (!/^https?:$/.test(u.protocol) || !HOSTS.has(u.hostname.toLowerCase())) return null;
  const q = u.searchParams;
  const lang = /^\/(de|en|it)(\/|$)/i.exec(u.pathname)?.[1].toLowerCase() || "en";
  const from = { name: first(q, "stationOrigName"), id: first(q, "stationOrigExtId", "connectionOrigEva") };
  const to = { name: first(q, "stationDestName"), id: first(q, "stationDestExtId", "connectionDestEva") };
  const when = first(q, "outwardDateTime", "connectionDatetimeDeparture");
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?$/.exec(when);
  const day = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  const real = day && day.getUTCMonth() === +m[2] - 1 && day.getUTCDate() === +m[3] && (!m[4] || (+m[4] < 24 && +m[5] < 60));
  const missing = [!from.name && "stationOrigName", !to.name && "stationDestName", !real && "outwardDateTime"].filter(Boolean);
  if (missing.length) return { missing };
  return { from, to, lang, date: `${m[1]}-${m[2]}-${m[3]}`, time: m[4] ? `${m[4]}:${m[5]}` : "00:00" };
}

class Refused extends Error {}

// One request to the shop's API: its JSON, or Refused naming the step, the status and the shop's own words.
async function ask(step, url, init, { fetchImpl, signal, left }) {
  const timeoutMs = left();
  const sig = AbortSignal.any([AbortSignal.timeout(timeoutMs), ...(signal ? [signal] : [])]);
  let res;
  try {
    res = await fetchImpl(url, { ...init, signal: sig, redirect: "manual" });
  } catch (err) {
    throw new Refused(`${step} did not answer (${err?.name === "TimeoutError" ? `no answer in ${Math.round(timeoutMs / 1000)} s` : String(err?.message || err).split("\n")[0].slice(0, 120)})`);
  }
  const text = await res.text().catch(() => "");
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {}
  if (res.ok && body && !body.error) return { body, cookies: res.headers.getSetCookie?.() ?? [] };
  const wall = !body && /<html/i.test(text) ? (/captcha|challenge|just a moment|access temporarily restricted|zugriff/i.test(text) ? ", a bot wall's page in place of its answer" : ", a web page in place of its answer") : "";
  const said = body?.error ? `: ${String(body.error.data || body.error.message || "").split(" {")[0].slice(0, 200)}${body.error.code ? ` (its error ${body.error.code})` : ""}` : "";
  throw new Refused(`${step} answered HTTP ${res.status}${wall}${said}`);
}

const span = (ms) => {
  const min = Math.round((Number(ms) || 0) / 60000);
  return min >= 60 ? `${Math.floor(min / 60)} h${min % 60 ? ` ${min % 60} min` : ""}` : `${min} min`;
};
const plain = (s) => String(s || "").replace(/<br\s*\/?>/gi, " ").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
const named = (st) => ({ ...st, name: st.name || st.meta });
const euro = (n) => `EUR ${Number(n).toFixed(2)}`;
const local = (v, lang) => (v && typeof v === "object" ? v[lang] || v.en || v.de || "" : v || "");

// The stations the link names, each the shop's own match: the one whose number the link gives, else its first.
async function station(want, ctx) {
  const { body } = await ask(`the shop's station search for "${want.name}"`, `${API}/hafas/v1/stations?count=8&name=${encodeURIComponent(want.name)}`, { headers: ctx.headers }, ctx);
  const list = (Array.isArray(body) ? body : []).filter((s) => s && s.number && (s.name || s.meta));
  const hit = list.find((s) => String(s.number) === want.id) || list[0];
  if (!hit) throw new Refused(`no station on ÖBB's timetable matches "${want.name}"`);
  return named(hit);
}

const passenger = { type: "ADULT", id: 1, me: false, remembered: false, challengedFlags: { hasHandicappedPass: false, hasAssistanceDog: false, hasWheelchair: false, hasAttendant: false }, relations: [], cards: [], birthdateChangeable: true, birthdateDeletable: true, nameChangeable: true, passengerDeletable: true, isSelected: true };

export function timetableBody(trip, from, to, count = COUNT) {
  return {
    reverse: false,
    datetimeDeparture: `${trip.date}T${trip.time}:00.000`,
    filter: { regionaltrains: false, direct: false, changeTime: false, wheelchair: false, bikes: false, trains: false, motorail: false, droppedConnections: false },
    passengers: [passenger],
    count,
    debugFilter: { noAggregationFilter: false, noEqclassFilter: false, noNrtpathFilter: false, noPaymentFilter: false, useTripartFilter: false, noVbxFilter: false, noCategoriesFilter: false },
    sortType: "DEPARTURE",
    from,
    to,
    timeout: {},
  };
}

// Scotty's times: "HHMMSS", or "DDHHMMSS" a day or more on; out as "HH:MM", with the day it lands when it is a later one.
const at = (date, t) => {
  const v = String(t || "").padStart(6, "0");
  const days = v.length > 6 ? Number(v.slice(0, -6)) : 0;
  const hm = `${v.slice(-6, -4)}:${v.slice(-4, -2)}`;
  if (!days) return { hm, day: "" };
  const d = new Date(Date.UTC(+date.slice(0, 4), +date.slice(4, 6) - 1, +date.slice(6, 8) + days));
  return { hm, day: d.toISOString().slice(0, 10) };
};
const hms = (t) => {
  const v = String(t || "").padStart(6, "0");
  return (Number(v.slice(0, -4)) * 60 + Number(v.slice(-4, -2))) * 60000;
};

// One request to Scotty's service: its one answer, or Refused naming the step.
async function scotty(step, req, ctx) {
  const { body } = await ask(`Scotty's ${step}`, MGATE, { method: "POST", headers: { accept: "application/json", "content-type": "application/json" }, body: JSON.stringify({ lang: ctx.lang === "en" ? "eng" : ctx.lang === "it" ? "ita" : "deu", svcReqL: [req], ...CLIENT }) }, ctx);
  const res = body.svcResL?.[0];
  if (body.err && body.err !== "OK") throw new Refused(`Scotty's ${step} answered ${body.err}${body.errTxt ? `: ${body.errTxt}` : ""}`);
  if (!res || res.err !== "OK") throw new Refused(`Scotty's ${step} answered ${res?.err || "nothing"}${res?.errTxt ? `: ${res.errTxt}` : ""}`);
  return res.res || {};
}

// A station the link names, Scotty's own match: the one whose number the link gives, else its first.
async function stop(want, ctx) {
  const r = await scotty(`station search for "${want.name}"`, { meth: "LocMatch", req: { input: { loc: { type: "S", name: `${want.name}?` }, maxLoc: 8, field: "S" } } }, ctx);
  const list = (r.match?.locL || []).filter((l) => l && l.lid && l.name);
  const hit = list.find((l) => String(l.extId) === want.id) || list[0];
  if (!hit) throw new Refused(`no station on ÖBB's timetable matches "${want.name}"`);
  return hit;
}

// The shop's fares for the trains, by their departure and arrival, as the shop's page asks: a Map, or Refused.
async function shopFares(trip, ctx) {
  const shop = { ...ctx, headers: { ...HEADERS, lang: trip.lang } };
  const { body: s, cookies } = await ask("the shop's session start", `${API}/domain/v4/init`, { headers: shop.headers }, shop);
  if (!s.accessToken || !s.sessionId) throw new Refused("the shop's session start gave no session");
  const cookie = cookies.map((c) => c.split(";")[0]).filter(Boolean).join("; ");
  shop.headers = { ...shop.headers, accesstoken: s.accessToken, sessionid: s.sessionId, "x-ts-supportid": `WEB_${s.supportId || ""}`, ...(cookie ? { cookie } : {}) };
  const from = await station(trip.from, shop);
  const to = await station(trip.to, shop);
  const { body: tt } = await ask("the shop's timetable", `${API}/hafas/v4/timetable`, { method: "POST", headers: { ...shop.headers, "content-type": "application/json" }, body: JSON.stringify(timetableBody(trip, from, to, COUNT + 3)) }, shop);
  const trains = (tt.connections || []).filter((c) => c && c.id);
  if (!trains.length) return new Map();
  const ids = trains.map((c) => `connectionIds[]=${encodeURIComponent(c.id)}`).join("&");
  const { body: p } = await ask("the shop's fares", `${API}/offer/v1/prices?${ids}&sortType=DEPARTURE&bestPriceId=undefined`, { headers: shop.headers }, shop);
  const by = new Map((p.offers || []).map((o) => [o.connectionId, o]));
  return new Map(trains.filter((c) => by.has(c.id)).map((c) => [`${String(c.from?.departure).slice(0, 16)}|${String(c.to?.arrival).slice(0, 16)}`, by.get(c.id)]));
}

function fareOf(offer, lang) {
  if (!offer || offer.offerError || typeof offer.price !== "number") return "";
  const scope = (offer.reducedScope || []).map((r) => (r.from?.name && r.to?.name ? `${r.from.name} to ${r.to.name}` : "")).filter(Boolean);
  return [`from ${euro(offer.price)}`, plain(local(offer.specialNote, lang)), offer.firstClass ? "first class" : "", scope.length ? `this fare covers only ${scope.join(" and ")}` : ""].filter(Boolean).join(", ");
}

// The trip read: { ok, text, trains, fares } with the page's words, or { ok: false, why }.
export async function readJourney(trip, { fetchImpl = fetch, signal, capMs = 90000, now = Date.now } = {}) {
  const end = now() + capMs;
  const ctx = { fetchImpl, signal, lang: trip.lang, left: () => Math.max(1000, Math.min(25000, end - now())) };
  try {
    const from = await stop(trip.from, ctx);
    const to = await stop(trip.to, ctx);
    const r = await scotty("timetable", { meth: "TripSearch", req: { depLocL: [{ type: "S", lid: from.lid }], arrLocL: [{ type: "S", lid: to.lid }], outDate: trip.date.replaceAll("-", ""), outTime: `${trip.time.replace(":", "")}00`, outFrwd: true, numF: COUNT, getPasslist: false, getPolyline: false, trfReq: { jnyCl: 2, tvlrProf: [{ type: "E" }], cType: "PK" } } }, ctx);
    const locs = r.common?.locL || [], prods = r.common?.prodL || [];
    const trains = (r.outConL || []).filter((c) => c && c.dep && c.arr);
    let fares = new Map(), fareWhy = "";
    if (trains.length) {
      try {
        fares = await shopFares(trip, ctx);
      } catch (err) {
        if (!(err instanceof Refused)) throw err;
        fareWhy = err.message;
      }
    }
    const lines = [`# ÖBB trains, ${from.name} to ${to.name}, ${trip.date} from ${trip.time}`, "", `Stations: "${trip.from.name}" read as ${from.name} (${from.extId}); "${trip.to.name}" read as ${to.name} (${to.extId}).`];
    lines.push(fareWhy ? `Fares: none read, since ${fareWhy}; each train's own shop link shows its fare in a browser.` : "Fares: one adult with no discount card, second class unless a train says otherwise, each the lowest the shop offers for that train when read.", "");
    if (!trains.length) lines.push(`No train: Scotty's timetable has none from ${trip.time} on ${trip.date} for these two stations.`);
    let priced = 0, last = "";
    trains.forEach((c, i) => {
      const d = at(c.date, c.dep.dTimeS), a = at(c.date, c.arr.aTimeS);
      const iso = (x) => `${x.day || `${c.date.slice(0, 4)}-${c.date.slice(4, 6)}-${c.date.slice(6, 8)}`}T${x.hm}`;
      const fare = fareOf(fares.get(`${iso(d)}|${iso(a)}`), trip.lang);
      if (fare) priced += 1;
      last = iso(d);
      const changes = Number(c.chg) ? `${c.chg} change${c.chg > 1 ? "s" : ""}` : "direct";
      const shop = c.trfRes?.clickout ? ` Its shop link: ${c.trfRes.clickout}` : "";
      lines.push(`${i + 1}. ${d.hm}${d.day ? ` on ${d.day}` : ""} ${locs[c.dep.locX]?.name} to ${a.hm}${a.day ? ` on ${a.day}` : ""} ${locs[c.arr.locX]?.name}: ${span(hms(c.dur))}, ${changes}; ${fare || "no fare read"}.${shop}`);
      for (const sec of c.secL || []) {
        const sd = at(c.date, sec.dep?.dTimeS), sa = at(c.date, sec.arr?.aTimeS);
        const prod = sec.jny ? prods[sec.jny.prodX] : null;
        const what = sec.type === "JNY" ? `${prod?.name || "a train"}${sec.jny.dirTxt ? ` towards ${sec.jny.dirTxt}` : ""}` : sec.type === "WALK" || sec.type === "TRSF" ? "walk" : String(sec.type || "leg").toLowerCase();
        const plat = sec.dep?.dPlatfS || sec.dep?.dPltfS?.txt;
        lines.push(`   - ${what}: ${sd.hm} ${locs[sec.dep?.locX]?.name}${plat ? `, platform ${plat}` : ""} to ${sa.hm} ${locs[sec.arr?.locX]?.name}`);
      }
    });
    if (last) {
      const next = new Date(`${last}:00Z`);
      next.setUTCMinutes(next.getUTCMinutes() + 1);
      lines.push("", `Later trains: the same link with outwardDateTime=${next.toISOString().slice(0, 16)}.`);
    }
    return { ok: true, text: lines.join("\n"), trains: trains.length, fares: priced };
  } catch (err) {
    if (err instanceof Refused) return { ok: false, why: err.message };
    throw err;
  }
}
