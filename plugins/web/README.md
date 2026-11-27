# web

yerk-web, the add-on that gives a room the web: five tools on one MCP server, written in JavaScript for Node.js. `web_fetch` reads a page or a PDF or Word file, `web_search` searches, `web_research` and `web_research_result` run a longer research job and fetch its answer, and `web_image` saves one picture.

## The code: lib/

- `lib/common.mjs`: shared helpers: where the install lives, keys, settings, locks and rate limits.
- `lib/fetch/`: the fetch ladder, which tries free reads from this machine first and paid ones after, until a page reads as real:
  - `browserbase.js`: a small client for Browserbase, the paid remote-browser service.
  - `commons.js`: reads a House of Commons written question's answer, which loads only after the page opens.
  - `contexts.js`: keeps one saved Browserbase browser profile per group of sites, so a login done once keeps working.
  - `detect.js`: decides whether a fetched page is a real page or a wall, such as a bot check.
  - `document.js`: reads a PDF or Word file at a link as text.
  - `extract.js`: turns fetched HTML into a title, markdown and plain text.
  - `image.js`: fetches the bytes of one picture, for `web_image`.
  - `l3.js`: the last step, a real remote browser session with proxies and captcha solving.
  - `ladder.js`: the ladder itself, step by step.
  - `local.js`: the free steps run from this machine: a plain fetch and local headless browsers.
  - `oebb.js`: reads an ÖBB (Austrian railways) journey: the trains, times and fares for a dated trip.
  - `util.js`: shared helpers: settings, state files, monthly usage counts and the call log.
- `lib/research.mjs`: runs Exa Agent research jobs for `web_research` and `web_research_result`.
- `lib/search/`: web search:
  - `engines.js`: the search engines it can call, each returning results in one common shape.
  - `provider.js`: one search call: the engines tried in order, plus a YouTube leg when its rule says so.
  - `util.js`: shared helpers: settings, cache, state files and text.
  - `video.js`: the YouTube leg: when to add videos, and the transcript excerpts it adds.
- `lib/tools.mjs`: the five tools, as the model is handed them.
- `lib/untrusted.mjs`: wraps every page and search result in markers, so the model reads it as a record, never an order.

## The server and its package

- `package-lock.json`: npm's lock file: the exact versions of the dependencies.
- `package.json`: the npm package file: its name, its three dependencies and its test command (`npm test`).
- `server.mjs`: starts the plug-in as one MCP server, the way a room's Claude Code attaches it.

## The tests: test/

- `test/commons.test.mjs`: reading a House of Commons written answer, on recorded pages.
- `test/fetch.test.mjs`: the fetch ladder's steps and its page verdicts.
- `test/fixtures/`: the recorded files the tests read: `cf-g2.html`, a bot-check page, and `notice.doc`, an old-format Word file.
- `test/live.test.mjs`: the one test against the live web, skipped unless `YERK_WEB_LIVE=1` is set.
- `test/oebb.test.mjs`: reading an ÖBB journey, against a fake ÖBB.
- `test/tools.test.mjs`: the five tools, on recorded pages, with no live state.

Every test but the live one runs with no network.
