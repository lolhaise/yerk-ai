// yerk-web's shared reads and writes, used by tools.mjs, research.mjs and the fetch and search code: where the install
// lives, the keys from the private folder's secrets (read on every call, kept in this process's memory only and never
// written to a file, a log, a tool result or the environment: the search code spawns children for YouTube), the
// optional settings file, a lock for the small state files every room's process shares, the per-minute window those
// processes book together, and the spend row each paid call leaves in Yerk's ledger.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// ---- where the install lives ------------------------------------------------------------------
// --private names the private folder (its secrets/ and web.json), else YERK_PRIVATE. --state, else YERK_STATE, else
// $XDG_STATE_HOME/yerk, else ~/.local/state/yerk, as yerk_ai/config.py finds it. --room, else YERK_ROOM, else the room
// whose work folder this process runs in (Claude Code starts a room's servers in <state>/rooms/<room>/work). `work`, where
// web_image saves a picture for the room's Read: the folder this process runs in when it is that room's work folder, else
// <state>/rooms/<room>/work; with no room, the folder it runs in. `limit`: the room's own limit on a call to this server,
// in ms, from YERK_TOOL_TIMEOUT, which Yerk sets from the server's `timeout` in rooms.toml (Claude Code cuts a call there
// and drops its answer, and ignores one under a second); null when none is set. `own`, where this process writes its
// state: <state>/web, else, for a Yerk worker (YERK_WORKER its id, hex, at most 12 characters, and YERK_WORK_DIR its own
// folder, as Yerk's worker start sets them), <its folder>/web, since its unit mounts the state folder read-only (need 109
// of 2026-11-18: every web_fetch in a worker failed on the rate window's lock). <state>/web stays `web`, read for what
// every room's process booked and learned there (takeWindow's `also`, the site memory) and never written by a worker.
export function where(argv = process.argv.slice(2), env = process.env, cwd = process.cwd()) {
  const arg = (name) => {
    const i = argv.indexOf(`--${name}`);
    return i >= 0 && argv[i + 1] ? argv[i + 1] : undefined;
  };
  const home = (p) => (p === "~" ? os.homedir() : p?.startsWith("~/") ? path.join(os.homedir(), p.slice(2)) : p);
  const priv = home(arg("private") ?? env.YERK_PRIVATE);
  const state = path.resolve(home(arg("state") ?? env.YERK_STATE ?? path.join(env.XDG_STATE_HOME || path.join(os.homedir(), ".local/state"), "yerk")));
  const base = env.CLAUDE_PROJECT_DIR || cwd;
  const own = /\/rooms\/([^/]+)\/work\/?$/.exec(base)?.[1];
  const room = arg("room") ?? env.YERK_ROOM ?? own;
  const work = path.resolve(room && own !== room ? path.join(state, "rooms", room, "work") : base);
  const limit = Number(env.YERK_TOOL_TIMEOUT);
  const folder = String(env.YERK_WORK_DIR || "");
  const worker = /^[0-9a-f]{1,12}$/.test(String(env.YERK_WORKER || "")) && path.isAbsolute(folder) ? String(env.YERK_WORKER) : null;
  const web = path.join(state, "web");
  return { private: priv ? path.resolve(priv) : "", state, web, own: worker ? path.join(path.resolve(folder), "web") : web, worker, room: room || null, work, limit: Number.isInteger(limit) && limit >= 1000 ? limit : null };
}

// The room's work folder, as where() names it; an `at` made by hand (the tests) gives the folder its room implies.
// web_image saves a picture there and web_research keeps an answer there.
export const workOf = (at) => at.work || (at.room ? path.join(at.state, "rooms", at.room, "work") : process.cwd());

// ---- keys and settings, read on every call --------------------------------------------------------
// One file per key in <private>/secrets/, its first line, as the harness keeps its own (secrets/openai.key).
export const KEY_FILES = { exa: "exa.key", parallel: "parallel.key", browserbase: "browserbase.key", browserbaseProject: "browserbase-project.id" };

// `why` says what is missing, for the one no-key line; it never carries a value.
export function readKey(at, name) {
  const file = `secrets/${KEY_FILES[name]}`;
  if (!at.private) return { value: "", why: `${file} was not read: no private folder was given (--private)` };
  try {
    const value = fs.readFileSync(path.join(at.private, file), "utf8").split("\n")[0].trim();
    return value ? { value } : { value: "", why: `${file} is empty` };
  } catch (err) {
    return { value: "", why: err?.code === "ENOENT" ? `${file} is not in the private folder` : `${file} could not be read (${err?.code || "error"})` };
  }
}

// <private>/web.json, optional: {"fetch": {...}, "search": {...}, "research": {...}}, each with the keys its code reads
// (lib/fetch/util.js and lib/search/util.js DEFAULTS, research.mjs dailyUsd). Keys never go here: a key field in it is
// dropped (tools.mjs), so the secrets folder stays the one place a key lives.
export function readSettings(at) {
  if (!at.private) return { conf: {} };
  try {
    const conf = JSON.parse(fs.readFileSync(path.join(at.private, "web.json"), "utf8"));
    return { conf: conf && typeof conf === "object" && !Array.isArray(conf) ? conf : {} };
  } catch (err) {
    if (err?.code === "ENOENT") return { conf: {} };
    return { conf: {}, note: `web.json in the private folder could not be read (${err?.code || "not JSON"}), so every setting ran on its default.` };
  }
}

export function section(conf, name) {
  const c = conf?.[name];
  return c && typeof c === "object" && !Array.isArray(c) ? c : {};
}

export function scrub(text, secrets) {
  let t = String(text);
  for (const s of secrets) if (s && s.length >= 6) t = t.split(s).join("<key>");
  return t;
}

// ---- one writer at a time on a shared state file ------------------------------------------------
const pause = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

export function withLock(file, fn) {
  const lock = `${file}.lock`;
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  for (let i = 0; i < 50; i++) {
    let fd;
    try {
      fd = fs.openSync(lock, "wx", 0o600);
    } catch (err) {
      if (err?.code !== "EEXIST") throw err;
      try {
        if (Date.now() - fs.statSync(lock).mtimeMs > 5000) fs.unlinkSync(lock); // a crashed holder's lock
      } catch {}
      pause(10);
      continue;
    }
    fs.closeSync(fd);
    try {
      return fn();
    } finally {
      try {
        fs.unlinkSync(lock);
      } catch {}
    }
  }
  return fn(); // held past half a second: book without it rather than stall the call
}

// ---- the per-minute caps, one window for every room's process -----------------------------------
// Each room that attaches yerk-web starts its own process, so a cap kept in one process's memory would grow with the
// rooms. Every cap is booked here instead, in one file the processes share: null when a slot was booked, else "all"
// (the window holds perMinute already) or "key" (it holds perKey for this key, a host). `also`: windows counted with
// this one and never written, the shared one a worker reads but may not write (where's `own`), so its reads keep to
// the caps with every room's; the rooms do not count a worker's, since a worker's shell can write its folder.
const inWindow = (file, now) => {
  try {
    const t = JSON.parse(fs.readFileSync(file, "utf8")).t;
    return Array.isArray(t) ? t.filter((e) => Array.isArray(e) && e[0] > now - 60000) : [];
  } catch {
    return [];
  }
};

export function takeWindow(file, { key = "", perMinute = 0, perKey = 0, now = Date.now(), also = [] }) {
  if (!(perMinute > 0) && !(perKey > 0)) return null;
  return withLock(file, () => {
    const w = inWindow(file, now);
    const all = [...w, ...also.flatMap((f) => inWindow(f, now))];
    if (perMinute > 0 && all.length >= perMinute) return "all";
    if (perKey > 0 && all.filter((e) => e[1] === key).length >= perKey) return "key";
    w.push([now, key]);
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify({ t: w }) + "\n", { mode: 0o600 });
    fs.renameSync(tmp, file);
    return null;
  });
}

// ---- spend: one row per paid call in Yerk's ledger ------------------------------------------------
// <state>/ledger.sqlite, table spend (t, room, tool, engine, usd, note), made by the harness's own schema
// (yerk_ai/ledger.py), so the morning diff carries the day's spend (DECISIONS 32). usd is what the provider billed when
// its answer says, else the call's list price. A ledger that cannot take the rows (none at this state folder, a harness
// from before the table, a lock held past 5 s) gets them in <state>/web/spend-unledgered.jsonl, and stderr says so once.
let sqlite;
let said = false;

export async function spend(at, tool, rows) {
  if (!rows.length) return;
  const t = Date.now() / 1000;
  const full = rows.map((r) => ({ t, room: at.room, tool, engine: r.engine, usd: Math.round(r.usd * 1e6) / 1e6, note: r.note ?? null }));
  const ledger = path.join(at.state, "ledger.sqlite");
  try {
    if (!fs.existsSync(ledger)) throw Object.assign(new Error("no ledger"), { code: "ENOENT" });
    sqlite ??= await import("node:sqlite");
    const db = new sqlite.DatabaseSync(ledger);
    try {
      db.exec("PRAGMA busy_timeout = 5000");
      const add = db.prepare("INSERT INTO spend (t, room, tool, engine, usd, note) VALUES (?, ?, ?, ?, ?, ?)");
      for (const r of full) add.run(r.t, r.room, r.tool, r.engine, r.usd, r.note);
    } finally {
      db.close();
    }
  } catch (err) {
    try {
      fs.mkdirSync(at.web, { recursive: true, mode: 0o700 });
      fs.appendFileSync(path.join(at.web, "spend-unledgered.jsonl"), full.map((r) => JSON.stringify(r) + "\n").join(""), { mode: 0o600 });
    } catch {}
    if (!said) {
      said = true;
      process.stderr.write(`yerk-web: spend rows went to web/spend-unledgered.jsonl, the ledger did not take them (${err?.code || String(err?.message || err).slice(0, 80)})\n`);
    }
  }
}
