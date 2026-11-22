// An ÖBB journey (need 136, 2026-11-22), on a fake ÖBB: no network. The wall a room met: web_fetch on the ticket shop's
// journey page climbed every rung (F1 to L3) into a captcha, and Scotty's old query page is gone. web_fetch on a shop
// link now reads the trains from Scotty's timetable service and asks the shop's API for the fares; the fake below answers
// as both did live on 2026-11-22, the shop's bot wall included. Every station, number and date is an example (decision 22).
// Run from the plug-in folder: npm test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { API, MGATE, oebbJourney } from "../lib/fetch/oebb.js";
import { webFetch } from "../lib/tools.mjs";

const LINK = "https://shop.oebbtickets.at/en/ticket?stationOrigName=Example%20Town%20Hbf&stationDestName=Placeholder%20Valley&outwardDateTime=2031-02-03T08:00";
const AT = { web: "/nonexistent-yerk-oebb", private: "/nonexistent-yerk-oebb" };
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const WALL = () => new Response("<!doctype html><html><head><title>Zugriff vorübergehend eingeschränkt | Access temporarily restricted</title></head></html>", { status: 403, headers: { "content-type": "text/html" } });

const LOCS = { "Example Town Hbf?": { lid: "A=1@L=9100001@", name: "Example Town Hbf", extId: "9100001" }, "Placeholder Valley?": { lid: "A=1@L=9100002@", name: "Placeholder Valley", extId: "9100002" } };
const TRIPS = {
  common: { locL: [{ name: "Example Town Hbf" }, { name: "Middle Junction" }, { name: "Placeholder Valley" }], prodL: [{ name: "RJX 101" }, { name: "REX 202" }] },
  outConL: [
    { date: "20310203", dep: { locX: 0, dTimeS: "084000" }, arr: { locX: 2, aTimeS: "112500" }, dur: "024500", chg: 0, trfRes: { clickout: "https://shop.oebbtickets.at/en/ticket?cref=example-one" },
      secL: [{ type: "JNY", dep: { locX: 0, dTimeS: "084000", dPlatfS: "4" }, arr: { locX: 2, aTimeS: "112500" }, jny: { prodX: 0, dirTxt: "Example End" } }] },
    { date: "20310203", dep: { locX: 0, dTimeS: "231000" }, arr: { locX: 2, aTimeS: "01011500" }, dur: "020500", chg: 1,
      secL: [{ type: "JNY", dep: { locX: 0, dTimeS: "231000" }, arr: { locX: 1, aTimeS: "235000" }, jny: { prodX: 0 } }, { type: "WALK", dep: { locX: 1, dTimeS: "235000" }, arr: { locX: 1, aTimeS: "235500" } }, { type: "JNY", dep: { locX: 1, dTimeS: "01000500" }, arr: { locX: 2, aTimeS: "01011500" }, jny: { prodX: 1 } }] },
  ],
};
const SHOP = [
  { id: "c1", from: { departure: "2031-02-03T08:40:00.000" }, to: { arrival: "2031-02-03T11:25:00.000" } },
  { id: "c2", from: { departure: "2031-02-03T23:10:00.000" }, to: { arrival: "2031-02-04T01:15:00.000" } },
];
const OFFERS = [
  { connectionId: "c1", price: 19.9, specialNote: { en: "Sparschiene ticket" }, offerError: false, firstClass: false },
  { connectionId: "c2", price: 9.9, specialNote: { en: "Sections on<br>Sparschiene" }, offerError: false, firstClass: false, reducedScope: [{ from: { name: "Middle Junction" }, to: { name: "Placeholder Valley" } }] },
];

// The fake ÖBB: Scotty's service always answers; the shop's API answers as `shop` says (open, or its bot wall).
function fakeOebb({ shop = "open" } = {}) {
  const asked = [];
  const impl = async (url, init = {}) => {
    asked.push(String(url));
    if (url === MGATE) {
      const req = JSON.parse(init.body).svcReqL[0];
      if (req.meth === "LocMatch") {
        const hit = LOCS[req.req.input.loc.name];
        return json({ err: "OK", svcResL: [{ meth: "LocMatch", err: "OK", res: { match: { locL: hit ? [hit] : [] } } }] });
      }
      assert.equal(req.req.outDate, "20310203");
      assert.equal(req.req.outTime, "080000");
      return json({ err: "OK", svcResL: [{ meth: "TripSearch", err: "OK", res: TRIPS }] });
    }
    if (!String(url).startsWith(API)) throw new Error(`the read asked a rung's host: ${url}`);
    if (shop === "wall") return WALL();
    if (String(url).endsWith("/domain/v4/init")) return json({ accessToken: "example-token", sessionId: "example-session", supportId: "0000" });
    if (String(url).includes("/hafas/v1/stations")) return json([{ number: 9100009, latitude: 1, longitude: 2, name: "", meta: decodeURIComponent(String(url).split("name=")[1]) }]);
    if (String(url).endsWith("/hafas/v4/timetable")) {
      const body = JSON.parse(init.body);
      assert.ok(body.from.name && body.to.name, "a meta station goes with its name");
      return json({ connections: SHOP });
    }
    if (String(url).includes("/offer/v1/prices")) return json({ offers: OFFERS });
    throw new Error(`unexpected ${url}`);
  };
  return { impl, asked };
}

test("a shop link names its trip; another host is no journey; a short link says what it lacks", () => {
  assert.deepEqual(oebbJourney(LINK), { from: { name: "Example Town Hbf", id: "" }, to: { name: "Placeholder Valley", id: "" }, lang: "en", date: "2031-02-03", time: "08:00" });
  const click = oebbJourney("https://shop.oebbtickets.at/de/ticket?connectionDatetimeDeparture=2031-02-03T08:40&connectionOrigEva=9100001&connectionDestEva=9100002&stationOrigName=A&stationDestName=B");
  assert.equal(click.lang, "de");
  assert.equal(click.from.id, "9100001");
  assert.equal(click.time, "08:40");
  assert.equal(oebbJourney("https://www.example.org/ticket?stationOrigName=A"), null);
  assert.deepEqual(oebbJourney("https://shop.oebbtickets.at/en/"), { missing: ["stationOrigName", "stationDestName", "outwardDateTime"] });
  assert.deepEqual(oebbJourney("https://tickets.oebb.at/en/ticket?stationOrigName=A&stationDestName=B&outwardDateTime=2031-02-30T08:00"), { missing: ["outwardDateTime"] });
});

test("the wall the room met: the shop's bot wall on the fares still gives the trains, their legs and each train's link", async () => {
  const { impl, asked } = fakeOebb({ shop: "wall" });
  const r = await webFetch({ url: LINK }, { at: AT, deps: { oebbFetchImpl: impl } });
  const text = r.content[0].text;
  assert.ok(!r.isError, text);
  assert.match(text, /^web_fetch read the ÖBB journey Example Town Hbf to Placeholder Valley on 2031-02-03 from 08:00 from Scotty/);
  assert.match(text, /Fares: none read, since the shop's session start answered HTTP 403, a bot wall's page/);
  assert.match(text, /1\. 08:40 Example Town Hbf to 11:25 Placeholder Valley: 2 h 45 min, direct; no fare read\. Its shop link: https:\/\/shop\.oebbtickets\.at\/en\/ticket\?cref=example-one/);
  assert.match(text, /- RJX 101 towards Example End: 08:40 Example Town Hbf, platform 4 to 11:25 Placeholder Valley/);
  assert.match(text, /2\. 23:10 Example Town Hbf to 01:15 on 2031-02-04 Placeholder Valley: 2 h 5 min, 1 change; no fare read\./);
  assert.match(text, /- walk: 23:50 Middle Junction to 23:55 Middle Junction/);
  assert.match(text, /Later trains: the same link with outwardDateTime=2031-02-03T23:11\./);
  assert.match(text, /<<<EXTERNAL_UNTRUSTED_CONTENT/);
  assert.ok(asked.every((u) => u === MGATE || u.startsWith(API)), "no rung of the ladder is climbed");
  assert.equal(asked.filter((u) => u.startsWith(API)).length, 1, "the wall stops the fare step at its first request");
});

test("an open shop gives each train its lowest fare, a partial one said as partial", async () => {
  const { impl } = fakeOebb();
  const text = (await webFetch({ url: LINK }, { at: AT, deps: { oebbFetchImpl: impl } })).content[0].text;
  assert.match(text, /2 trains, 2 with a fare/);
  assert.match(text, /Fares: one adult with no discount card, second class unless a train says otherwise/);
  assert.match(text, /direct; from EUR 19\.90, Sparschiene ticket\./);
  assert.match(text, /1 change; from EUR 9\.90, Sections on Sparschiene, this fare covers only Middle Junction to Placeholder Valley\./);
});

test("a short shop link gets the link's shape, and a station Scotty has not, its name, with nothing climbed", async () => {
  const { impl, asked } = fakeOebb();
  const short = await webFetch({ url: "https://shop.oebbtickets.at/en/ticket" }, { at: AT, deps: { oebbFetchImpl: impl } });
  assert.ok(short.isError);
  assert.match(short.content[0].text, /this link has no stationOrigName, stationDestName and outwardDateTime\. Ask for https:\/\/shop\.oebbtickets\.at\/en\/ticket\?stationOrigName=FROM/);
  assert.equal(asked.length, 0);
  const none = await webFetch({ url: LINK.replace("Placeholder%20Valley", "Nowhere%20Example") }, { at: AT, deps: { oebbFetchImpl: impl } });
  assert.ok(none.isError);
  assert.match(none.content[0].text, /no station on ÖBB's timetable matches "Nowhere Example"\. No train and no fare was read, and no other rung was tried/);
});
