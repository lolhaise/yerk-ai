// web_fetch's documents (need 87, 2026-11-11): a PDF or a Word file at a link is read as its text, never refused as a binary.
// A room asked what a province's form asks for met the binary wall on the form's own Word file and on two PDF copies of it,
// and answered off the forms list page and search previews. The ladder's fetch rungs keep a document's bytes (F1 a plain
// fetch from this machine, L1 and L2 Browserbase Fetch's base64 answer; a browser rung shows a viewer, never the bytes),
// tools.mjs saves the file in the room's work folder (where Read opens a PDF's pages) and this module reads its words:
//   pdf   pdftotext (poppler-utils), a page a `## page N` section as files.py writes a PDF sent to a room; a dynamic XFA
//         form, whose pages hold only a "Please wait..." placeholder that Adobe Reader replaces, from its own template
//   docx  word/document.xml from the zip, a paragraph a line, a table row a line with its cells between " | "
//   odt   content.xml from the zip, read the same way
//   doc   Word 97-2003: the WordDocument stream's text through its piece table, read from the compound file by hand
// Known by the bytes' own first bytes, never by a name or a content type alone; no dependency, no network, no model call.
import { execFile } from "node:child_process";
import zlib from "node:zlib";

export const DOCUMENT_CAP = 25 * 1024 * 1024; // a document's bytes kept at F1; Browserbase Fetch answers 5 MB at most
const INFLATE_CAP = 64 * 1024 * 1024; // the most one zip member or PDF stream may inflate to (a zip bomb stops here)

// The kinds read, as the reply names them, and the file's extension when saved.
export const KINDS = {
  pdf: { ext: "pdf", said: "a PDF" },
  docx: { ext: "docx", said: "a Word file (.docx)" },
  doc: { ext: "doc", said: "a Word 97-2003 file (.doc)" },
  odt: { ext: "odt", said: "an OpenDocument text file (.odt)" },
};

// A content type that may carry a document: a document's own, or a generic binary one a server sends any file under.
export function mayBeDocument(ctype) {
  const t = String(ctype || "").split(";")[0].trim().toLowerCase();
  return /^application\/(pdf|x-pdf|msword|vnd\.openxmlformats-officedocument\.wordprocessingml\.|vnd\.oasis\.opendocument\.text|octet-stream|download|force-download|x-download|zip|x-zip-compressed|x-ole-storage|vnd\.ms-word)/.test(t) || t === "binary/octet-stream";
}

// ---- the zip a .docx or an .odt is -----------------------------------------------------------------
// Its members by name, each read on demand: the central directory found from its end record, stored or deflated members.
function unzip(buf) {
  const min = Math.max(0, buf.length - 65557);
  let end = -1;
  for (let i = buf.length - 22; i >= min; i--) if (buf.readUInt32LE(i) === 0x06054b50) { end = i; break; }
  if (end < 0) return null;
  const count = buf.readUInt16LE(end + 10);
  let at = buf.readUInt32LE(end + 16);
  const members = new Map();
  for (let n = 0; n < count && at + 46 <= buf.length && buf.readUInt32LE(at) === 0x02014b50; n++) {
    const method = buf.readUInt16LE(at + 10);
    const size = buf.readUInt32LE(at + 20);
    const nameLen = buf.readUInt16LE(at + 28);
    const local = buf.readUInt32LE(at + 42);
    members.set(buf.toString("utf8", at + 46, at + 46 + nameLen), { method, size, local });
    at += 46 + nameLen + buf.readUInt16LE(at + 30) + buf.readUInt16LE(at + 32);
  }
  const read = (name, cap = INFLATE_CAP) => {
    const m = members.get(name);
    if (!m || m.local + 30 > buf.length || buf.readUInt32LE(m.local) !== 0x04034b50) return null;
    const from = m.local + 30 + buf.readUInt16LE(m.local + 26) + buf.readUInt16LE(m.local + 28);
    const raw = buf.subarray(from, from + m.size);
    if (m.method === 0) return raw;
    if (m.method === 8) return zlib.inflateRawSync(raw, { maxOutputLength: cap });
    throw new Error(`a zip member packed by method ${m.method}`);
  };
  return { has: (name) => members.has(name), read };
}

// ---- the compound file a .doc is ---------------------------------------------------------------------
// Its top-level streams by name: the header's FAT (through the DIFAT), the directory's tree under the root, and the mini
// stream for the small ones. documentKind runs this on every binary read at F1 and L1/L2, so a crafted file is held to
// what its own bytes hold (the fix of 2026-11-11): 512- or 4096-byte sectors only, never more FAT sectors than the file's
// sectors need, every chain (the DIFAT's, the FAT's, the mini FAT's) a sector at most once, and the directory's tree
// walked an entry at most once. Before it, a 64 KB file whose DIFAT looped asked Buffer.concat for 16 GiB, and a 4.6 KB
// one whose entries each named the next as both siblings walked 2^n paths (8 s at 26 deep, the walk allowed 64).
function compound(buf) {
  if (buf.length < 512 || buf.readUInt32LE(0) !== 0xe011cfd0 || buf.readUInt32LE(4) !== 0xe11ab1a1) return null;
  const shift = buf.readUInt16LE(30);
  if ((shift !== 9 && shift !== 12) || buf.readUInt16LE(32) !== 6) return null; // [MS-CFB] 2.2: v3 512, v4 4096; mini 64
  const size = 2 ** shift;
  const mini = 64;
  const per = size / 4; // the links one FAT sector holds
  const count = Math.max(0, Math.ceil(buf.length / size) - 1); // the sectors after the header
  const sector = (i) => (i < count ? buf.subarray((i + 1) * size, (i + 2) * size) : buf.subarray(0, 0));
  const fatSectors = [];
  const nFat = Math.min(buf.readUInt32LE(44), Math.ceil(count / per));
  for (let i = 0; i < 109 && fatSectors.length < nFat; i++) fatSectors.push(buf.readUInt32LE(76 + i * 4));
  for (let d = buf.readUInt32LE(68), hops = new Set(); fatSectors.length < nFat && d < count && !hops.has(d); ) {
    hops.add(d);
    const s = sector(d);
    for (let i = 0; i < per - 1 && (i + 1) * 4 <= s.length && fatSectors.length < nFat; i++) fatSectors.push(s.readUInt32LE(i * 4));
    d = s.length === size ? s.readUInt32LE(size - 4) : 0xfffffffe;
  }
  const table = (bytes) => Array.from({ length: Math.floor(bytes.length / 4) }, (_, i) => bytes.readUInt32LE(i * 4));
  const fat = table(Buffer.concat(fatSectors.map(sector)));
  // A chain's sectors from `start`, each once: a loop, a link off the table or `most` sectors ends it.
  const chain = (start, links, most = Infinity) => {
    const out = [];
    for (let s = start, seen = new Set(); s < links.length && !seen.has(s) && out.length < most; s = links[s]) {
      seen.add(s);
      out.push(s);
    }
    return out;
  };
  const big = (start, n) => Buffer.concat(chain(start, fat, Math.ceil(n / size)).map(sector)).subarray(0, n);
  const dir = big(buf.readUInt32LE(48), Infinity);
  const entry = (id) => {
    const o = id * 128;
    if (id >= 0xfffffffa || o + 128 > dir.length) return null;
    const len = Math.min(64, dir.readUInt16LE(o + 64));
    return { name: dir.toString("utf16le", o, o + Math.max(0, len - 2)), type: dir[o + 66], left: dir.readUInt32LE(o + 68), right: dir.readUInt32LE(o + 72), child: dir.readUInt32LE(o + 76), start: dir.readUInt32LE(o + 116), size: dir.readUInt32LE(o + 120) };
  };
  const root = entry(0);
  if (!root) return null;
  const streams = new Map();
  // The root's children, in the tree's order (an entry, its left, its right), each entry once however its links loop.
  for (const todo = [root.child], seen = new Set([0]); todo.length; ) {
    const id = todo.pop();
    const e = seen.has(id) ? null : entry(id);
    if (!e) continue;
    seen.add(id);
    if (e.type === 2) streams.set(e.name, e);
    todo.push(e.right, e.left);
  }
  const cutoff = buf.readUInt32LE(56);
  const ministream = big(root.start, root.size);
  const minifat = table(big(buf.readUInt32LE(60), buf.readUInt32LE(64) * size));
  const read = (name) => {
    const e = streams.get(name);
    if (!e) return null;
    if (e.size >= cutoff) return big(e.start, e.size);
    return Buffer.concat(chain(e.start, minifat, Math.ceil(e.size / mini)).map((i) => ministream.subarray(i * mini, (i + 1) * mini))).subarray(0, e.size);
  };
  return { has: (name) => streams.has(name), read };
}

// What the bytes are, by their own first bytes: "pdf", "docx", "odt" or "doc", else "".
export function documentKind(buf) {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf || []);
  if (b.subarray(0, 1024).includes("%PDF-") && !/^\s*[<{[]/.test(b.toString("latin1", 0, 64))) return "pdf"; // never a page or JSON naming one
  try {
    if (b.length > 30 && b.readUInt32LE(0) === 0x04034b50) {
      const z = unzip(b);
      if (z?.has("word/document.xml")) return "docx";
      if (z?.has("content.xml") && /^application\/vnd\.oasis\.opendocument\.text(-[a-z]+)?$/.test(String(z.read("mimetype", 1024) || "").trim())) return "odt";
    }
    if (compound(b)?.has("WordDocument")) return "doc";
  } catch {}
  return "";
}

// ---- words out of XML -----------------------------------------------------------------------------------
const ENTITY = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
export const decode = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => {
  if (e[0] !== "#") return ENTITY[e.toLowerCase()] ?? m;
  const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
  return Number.isInteger(n) && n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : m;
});

// Wingdings' boxes, as a form draws them by w:sym.
const WINGDINGS = { F06F: "☐", F071: "☐", F0A8: "☐", F078: "☒", F0FD: "☒", F0FE: "☑" };

// The words of a .docx's document.xml or an .odt's content.xml: a paragraph a line, a table's row a line with its cells
// between " | " (a cell's paragraphs between " / "), a tab as a tab, a break as a new line; a text box's paragraphs before
// the paragraph holding it. Left out: a field's code, deleted text in tracked changes, a drawing's fallback copy, and a
// note's body. A legacy Word check box is ☐ or ☒ by its state.
const VOCAB = {
  docx: { p: "w:p", t: "w:t", tbl: "w:tbl", tr: "w:tr", tc: "w:tc", tab: ["w:tab", "w:ptab"], br: ["w:br", "w:cr"], skip: ["w:instrText", "w:delText", "mc:Fallback", "w:del", "w:tabs"] },
  odt: { p: ["text:p", "text:h"], t: null, tbl: "table:table", tr: "table:table-row", tc: "table:table-cell", tab: ["text:tab"], br: ["text:line-break"], skip: ["office:annotation", "text:note", "text:tracked-changes", "draw:frame"] },
};
const isOne = (name, x) => (Array.isArray(x) ? x.includes(name) : name === x);
// A line's end cut of `chars` by hand: /[ ]+$/ backtracks through every run of them that does not end the string, so a
// .docx of 1 KB whose text is a megabyte of spaces and one letter took minutes in it (the fix of 2026-11-11).
const cutEnd = (s, chars) => {
  let e = s.length;
  while (e > 0 && chars.includes(s[e - 1])) e--;
  return s.slice(0, e);
};

export function xmlWords(xml, vocab) {
  const v = VOCAB[vocab];
  const lines = [];
  const paras = []; // the paragraphs open around this one (a text box sits inside a paragraph)
  let para = null;
  let cell = null;
  let row = null;
  let tables = 0;
  let inText = 0;
  let skipping = 0;
  let box = null;
  const end = (text) => {
    const t = cutEnd(text, "  ");
    if (tables && cell) cell.push(t.trim());
    else lines.push(t);
  };
  // A tag's name taken whole (the lookahead) and its attributes never past the next "<" (as XML's never are): a tag that
  // never closes costs the bytes to the next "<", once. Before 2026-11-11 each "<" of "<a <a <a ..." read to the end of
  // the part (a .docx of 1 KB, a megabyte of them, took about ten minutes), and one long name read it once per letter.
  const tag = /<(\/?)([\w:.-]+)(?![\w:.-])((?:[^<>"']|"[^"<]*"|'[^'<]*')*?)(\/?)>|([^<]+)/g;
  for (let m; (m = tag.exec(xml)); ) {
    const [, close, name, attrs, self, text] = m;
    if (text !== undefined) {
      if (!skipping && para !== null && (v.t ? inText : true)) para += decode(text);
      continue;
    }
    if (isOne(name, v.skip)) {
      if (!self) skipping += close ? -1 : 1;
      continue;
    }
    if (skipping) continue;
    if (v.t && name === v.t) {
      if (!self) inText += close ? -1 : 1;
      continue;
    }
    if (isOne(name, v.p)) {
      if (self && !close) end("");
      else if (!close) {
        paras.push(para);
        para = "";
      } else if (para !== null) {
        end(para);
        para = paras.pop() ?? null;
      }
      continue;
    }
    if (name === v.tbl) {
      tables += close ? -1 : self ? 0 : 1;
      continue;
    }
    if (tables === 1 && name === v.tr) {
      if (!close && !self) row = [];
      else if (close && row) {
        lines.push(cutEnd(row.join(" | "), " |"));
        row = null;
      }
      continue;
    }
    if (tables === 1 && name === v.tc) {
      if (!close && !self) cell = [];
      else if (row) {
        row.push((cell || []).filter(Boolean).join(" / "));
        cell = null;
      }
      continue;
    }
    if (para === null) continue;
    if (isOne(name, v.tab)) para += "\t";
    else if (isOne(name, v.br)) para += "\n";
    else if (name === "text:s") para += " ".repeat(Math.min(200, Number(/text:c="(\d+)"/.exec(attrs)?.[1] || 1)));
    else if (name === "w:noBreakHyphen") para += "-";
    else if (name === "w:sym") para += WINGDINGS[/w:char="([0-9A-Fa-f]{4})"/.exec(attrs)?.[1]?.toUpperCase()] ?? "";
    else if (name === "w:checkBox") {
      if (!close) box = { on: false };
      if ((close || self) && box) {
        para += box.on ? "☒" : "☐";
        box = null;
      }
    } else if (box && (name === "w:checked" || name === "w:default") && !close) box.on = !/w:val="(0|false|off)"/.test(attrs) || (name === "w:checked" && !attrs.includes("w:val"));
  }
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// ---- a Word 97-2003 file's text -------------------------------------------------------------------------
// The main document's characters (FibRgLw97 ccpText) through the piece table (the Clx at FibRgFcLcb97 fcClx in the table
// stream the FIB names), each piece 8-bit cp1252 or UTF-16; then a field's code dropped and its result kept, a cell mark a
// tab and a row's end a new line, a check box form field named as one (its state lives in another stream, not read).
const CP1252 = new TextDecoder("windows-1252");

function docWords(buf) {
  const cf = compound(buf);
  const wd = cf?.read("WordDocument");
  if (!wd || wd.length < 0x1aa || wd.readUInt16LE(0) !== 0xa5ec) throw new Error("it is not a Word 97-2003 file");
  const flags = wd.readUInt16LE(0x0a);
  if (flags & 0x0100) throw new Error("it is locked with a password");
  const tbl = cf.read(flags & 0x0200 ? "1Table" : "0Table");
  if (!tbl) throw new Error("its table stream is missing");
  const chars = Math.min(wd.readInt32LE(0x4c), wd.length); // a character takes a byte at least: pieces that repeat stop here
  const fcClx = wd.readUInt32LE(0x1a2);
  const clx = tbl.subarray(fcClx, fcClx + wd.readUInt32LE(0x1a6));
  let i = 0;
  while (i < clx.length && clx[i] === 0x01) i += 3 + clx.readUInt16LE(i + 1);
  if (clx[i] !== 0x02) throw new Error("its piece table is missing");
  const plc = clx.subarray(i + 5, i + 5 + clx.readUInt32LE(i + 1));
  const n = Math.floor((plc.length - 4) / 12);
  let text = "";
  for (let k = 0; k < n && text.length < chars; k++) {
    const from = plc.readUInt32LE(k * 4);
    const to = Math.min(plc.readUInt32LE(k * 4 + 4), chars, from + chars - text.length);
    if (from >= chars || to <= from) continue;
    const fc = plc.readUInt32LE((n + 1) * 4 + k * 8 + 2);
    const at = fc & 0x3fffffff;
    text += fc & 0x40000000 ? CP1252.decode(wd.subarray(at / 2, at / 2 + (to - from))) : wd.toString("utf16le", at, at + (to - from) * 2);
  }
  // Fields: \x13 code \x14 result \x15, nested; the code is dropped, but a check box's is named.
  let out = "";
  const stack = [];
  for (const ch of text) {
    if (ch === "\x13") stack.push({ code: "" });
    else if (ch === "\x14" && stack.length) stack.at(-1).result = true;
    else if (ch === "\x15" && stack.length) {
      const f = stack.pop();
      if (/^\s*FORMCHECKBOX\b/.test(f.code)) out += "[check box]";
    } else if (stack.length && !stack.at(-1).result) stack.at(-1).code += ch;
    else out += ch;
  }
  return out
    .replace(/\x07\x07/g, "\x07\n")
    .replace(/\x07/g, "\t")
    .replace(/[\r\x0b\x0c\x0e]/g, "\n")
    .replace(/\x1e/g, "-")
    .replace(/[\x00-\x08\x1f]/g, "")
    .split("\n")
    .map((x) => cutEnd(x, "\t "))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// ---- a PDF's text ---------------------------------------------------------------------------------------
const NO_TEXT = "(no text on this page)"; // files.py NO_TEXT, as a PDF sent to a room reads
const XFA_SHELL = /If this message is not eventually replaced by the proper contents of the document/i;

function run(cmd, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024, encoding: "utf8" }, (err, stdout, stderr) => {
      if (err) {
        err.stderr = String(stderr || "");
        reject(err);
      } else resolve(stdout);
    });
  });
}

// The next place `needle` stands at or after `from` in `s`, for a scan whose `from` only grows: the last answer is kept
// while it lies ahead, and a needle once missing stays missing. A thousand tags that never close cost one pass of the
// template this way; the lazy regexes it replaced (2026-11-11) read to its end once for each, so an XFA form of 1 KB whose
// template inflated to a megabyte of "<text>" or "<script>" locked the web plug-in for minutes.
function finder(s) {
  const last = new Map();
  return (needle, from) => {
    let at = last.get(needle);
    if (at === undefined || (at >= 0 && at < from)) last.set(needle, (at = s.indexOf(needle, from)));
    return at;
  };
}

// Each element whose name `re` (/<(name|...)\b/g) opens, as `visit` sees it: (name, start, inner, end), `inner` null for a
// self-closed one. As /<(name)\b(?:[^>]*\/>|>([\s\S]*?)<\/\1>)/ matched: a tag closed by "/>" at its first ">", else up
// to the first "</name>"; one that never closes is passed over.
function elements(s, re, visit) {
  const find = finder(s);
  re.lastIndex = 0;
  for (let m; (m = re.exec(s)); ) {
    const name = m[1];
    const gt = find(">", re.lastIndex);
    if (gt < 0) return;
    if (s[gt - 1] === "/" && gt > re.lastIndex) {
      visit(name, m.index, null, gt + 1);
      re.lastIndex = gt + 1;
      continue;
    }
    const close = find(`</${name}>`, re.lastIndex);
    if (close < 0) continue;
    visit(name, m.index, gt < close ? gt + 1 : close, close + name.length + 3);
    re.lastIndex = close + name.length + 3;
  }
}

// A dynamic XFA form's own words, from its template packet: each caption, label and fixed text in the form's order,
// and the choices a list offers; a script, a tool tip and a field's binding are left out. Linear in the PDF: each stream
// read once, and all its streams together inflated to XFA_INFLATE at most (a stream past what is left is passed over).
const XFA_INFLATE = 4 * INFLATE_CAP;
export function xfaWords(buf) {
  const s = buf.toString("latin1");
  let budget = XFA_INFLATE;
  for (let at = s.indexOf("stream"), next = 0; at >= 0; at = s.indexOf("stream", next)) {
    next = at + 6;
    if (s.startsWith("endstream", at - 3)) continue;
    const dict = s.slice(Math.max(0, at - 400), at);
    const open = dict.slice(Math.max(0, dict.lastIndexOf(" obj"))); // this object's dictionary, nested ones and all
    if (/\/(Image|FontFile|Length1|Subtype\s*\/XML)\b/.test(open) && !/xfa/i.test(open)) continue;
    let from = at + 6;
    if (s[from] === "\r") from++;
    if (s[from] === "\n") from++;
    const stop = s.indexOf("endstream", from);
    if (stop < 0) break;
    next = stop + 9;
    let body = buf.subarray(from, stop);
    if (/\/FlateDecode/.test(open)) {
      const cap = Math.min(INFLATE_CAP, budget);
      if (cap <= 0) break;
      try {
        body = zlib.inflateSync(body, { maxOutputLength: cap, finishFlush: zlib.constants.Z_SYNC_FLUSH });
        budget -= body.length;
      } catch {
        budget -= cap;
        continue;
      }
    }
    const xml = body.toString("utf8");
    if (!/xfa-template\//.test(xml)) continue;
    let clean = "";
    let kept = 0;
    elements(xml, /<(script|assist|bind|validate|calculate|event|traversal)\b/g, (name, start, inner, end) => {
      clean += `${xml.slice(kept, start)} `;
      kept = end;
    });
    clean += xml.slice(kept);
    const words = [];
    elements(clean, /<(text|exData)\b/g, (name, start, inner, end) => {
      if (inner === null) return;
      const t = decode(clean.slice(inner, end - name.length - 3).replace(/<(br|p|li|div)\b[^<>]*>/gi, "\n").replace(/<[^<>]+>/g, "")).replace(/[ \t ]+/g, " ");
      for (const line of t.split("\n").map((x) => x.trim()).filter(Boolean)) if (words.at(-1) !== line) words.push(line);
    });
    return words.join("\n");
  }
  return "";
}

// A PDF's pages by pdftotext, each a `## page N` section; the page count; and `xfa` when its pages are the XFA placeholder.
async function pdfWords(file, buf, { pdftotext = "pdftotext", timeoutMs = 60000 } = {}) {
  let out;
  try {
    out = await run(pdftotext, ["-enc", "UTF-8", "-eol", "unix", file, "-"], timeoutMs);
  } catch (err) {
    if (err?.code === "ENOENT") throw new Error("pdftotext (poppler-utils) is not on this machine");
    if (err?.killed) throw new Error(`pdftotext did not finish in ${Math.round(timeoutMs / 1000)} s`);
    const why = String(err?.stderr || "").split("\n").find((x) => /error/i.test(x)) || "";
    throw new Error(/password|encrypt/i.test(why) ? "it is locked with a password" : `pdftotext could not read it${why ? ` (${why.replace(/^Syntax Error[^:]*:\s*/i, "").slice(0, 120)})` : ""}`);
  }
  const pages = out.replace(/\f$/, "").split("\f");
  const xfa = pages.some((p) => XFA_SHELL.test(p)) ? xfaWords(buf) : "";
  if (xfa) return { text: xfa, pages: pages.length, xfa: true };
  const bare = pages.every((p) => !p.trim());
  const text = bare ? "" : pages.map((p, i) => [`## page ${i + 1}`, ...(p.split("\n").map((x) => x.trimEnd()).filter((x) => x.trim()).join("\n") || NO_TEXT).split("\n")].join("\n")).join("\n\n");
  return { text, pages: pages.length, bare };
}

// The words of a saved document: { text, pages?, bare?, xfa? }. Throws with a plain reason when it cannot be read.
export async function readDocument(kind, file, buf, opts = {}) {
  if (kind === "pdf") return pdfWords(file, buf, opts);
  if (kind === "doc") return { text: docWords(buf) };
  const z = unzip(buf);
  const xml = z?.read(kind === "docx" ? "word/document.xml" : "content.xml");
  if (!xml) throw new Error("its text part is missing");
  return { text: xmlWords(xml.toString("utf8"), kind) };
}
