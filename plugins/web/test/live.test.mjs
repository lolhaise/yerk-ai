// The one test against the live web, skipped unless YERK_WEB_LIVE=1: web_fetch reads a public page from this machine
// (a free rung), and web_search runs once when the private folder YERK_PRIVATE names holds secrets/exa.key or
// secrets/parallel.key (one paid search, about a US cent at most). State and spend rows go to a temp folder, never to
// the live state. Run: YERK_WEB_LIVE=1 YERK_PRIVATE=~/yerk-private npm test
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { readKey, where } from "../lib/common.mjs";
import { webFetch, webSearch } from "../lib/tools.mjs";

const LIVE = process.env.YERK_WEB_LIVE === "1";
const TMP = LIVE ? fs.mkdtempSync(path.join(os.tmpdir(), "yerk-web-live-")) : "";
const at = { ...where([], process.env), state: TMP, web: path.join(TMP, "web"), room: "live-test" };
const keyed = LIVE && Boolean(readKey(at, "exa").value || readKey(at, "parallel").value);
after(() => TMP && fs.rmSync(TMP, { recursive: true, force: true }));

test("live: web_fetch reads a public page from this machine at a free rung", { skip: !LIVE && "set YERK_WEB_LIVE=1" }, async () => {
  const t = (await webFetch({ url: "https://en.wikipedia.org/wiki/Web_crawler" }, { at })).content[0].text;
  assert.match(t.split("\n")[0], /^web_fetch read https:\/\/en\.wikipedia\.org\/wiki\/Web_crawler at F[123] /, t.slice(0, 600));
  assert.match(t, /crawler/i);
});

test("live: web_search answers from a keyed engine", { skip: !keyed && "set YERK_WEB_LIVE=1 and YERK_PRIVATE to a private folder with secrets/exa.key or secrets/parallel.key" }, async () => {
  const t = (await webSearch({ query: "Model Context Protocol specification", count: 3 }, { at })).content[0].text;
  assert.match(t.split("\n")[0], /^web_search "Model Context Protocol specification": \d+ results from (Exa, paid|Parallel's paid Search API)\.$/, t.slice(0, 600));
});
