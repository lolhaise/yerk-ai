#!/usr/bin/env node
// yerk-web, Yerk AI's web reach as one MCP server over stdio: web_fetch, web_search, web_research,
// web_research_result and web_image (lib/tools.mjs). One process per Claude Code session that attaches it, started as
//   node plugins/web/server.mjs --private <private folder> [--state <state folder>] [--room <room>]
// Nothing is written to stdout but the protocol, and nothing secret is ever written to stderr.
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { TOOLS, callTool, shutdown } from "./lib/tools.mjs";

const server = new Server({ name: "yerk-web", version: "1.0.0" }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
server.setRequestHandler(CallToolRequestSchema, async (req, extra) => callTool(req.params.name, req.params.arguments, { signal: extra.signal }));

// The local browser is a child of this process: close it when the session that owns us goes away
// (stdin closes, or Claude Code stops the server with a signal), so it never outlives the room.
let closing = false;
async function stop() {
  if (closing) return;
  closing = true;
  await shutdown().catch(() => {});
  process.exit(0);
}
process.stdin.on("end", stop);
process.stdin.on("close", stop);
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, stop);

await server.connect(new StdioServerTransport());
