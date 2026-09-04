#!/usr/bin/env node
/**
 * smoke.mjs — drive the server the way a real client does: over stdio, with the SDK's own
 * Client. Reading the source proves nothing about a protocol; a handshake does.
 *
 * Every assertion here is free — no Routes API call is made, because each failing case is
 * rejected before the network. The one test that would cost money is opt-in:
 *
 *   KOVA_ROUTE_LIVE=1 npm test      # adds one real solve (2 Routes API events)
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const SERVER = join(here, "..", "server.mjs");

let pass = 0, fail = 0;
const ok = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}${detail ? ` — ${detail}` : ""}`); }
};

const HOME = "Union Station, Los Angeles, CA 90012";
const STOPS = [
  { name: "Sample A", address: "300 S Santa Fe Ave, Los Angeles, CA 90013" },
  { name: "Sample B", address: "1200 Getty Center Dr, Los Angeles, CA 90049" },
];

const client = new Client({ name: "kova-route-smoke", version: "1.0.0" });
const transport = new StdioClientTransport({ command: process.execPath, args: [SERVER] });

console.log("\nkova-route smoke test");
console.log("─".repeat(60));

await client.connect(transport);
ok("handshake over stdio", true);

// ---------------------------------------------------------------- tools/list

const { tools } = await client.listTools();
const names = tools.map((t) => t.name).sort();
ok("tools/list returns both tools", names.join(",") === "latest_departure,solve_route", names.join(","));

const solve = tools.find((t) => t.name === "solve_route");
ok("solve_route declares an input schema", !!solve?.inputSchema?.properties?.stops);
ok("solve_route declares an output schema", !!solve?.outputSchema?.properties?.last_drop);
ok("solve_route is annotated read-only", solve?.annotations?.readOnlyHint === true);
ok("every tool has a description an agent can route on",
   tools.every((t) => (t.description ?? "").length > 40));

// ------------------------------------------------- input validation (protocol level)

// The SDK validates arguments against the declared schema before the handler runs, and
// surfaces the failure as an isError result rather than throwing at the client. Either way
// the point stands: a malformed call never reaches the network.
const schemaRejects = async (args) => {
  const r = await client.callTool({ name: "solve_route", arguments: args });
  return r.isError === true && /Input validation error/i.test(r.content?.[0]?.text ?? "");
};

ok("an empty stops array is rejected by the schema, not the network",
   await schemaRejects({ home: HOME, stops: [] }));
ok("26 stops is rejected (Routes API allows 25 intermediates)",
   await schemaRejects({ home: HOME, stops: Array.from({ length: 26 }, () => ({ address: "x" })) }));
ok("a stop with no address at all is rejected by the schema",
   await schemaRejects({ home: HOME, stops: [{ name: "nameless" }] }));

// ------------------------------------------------- domain errors are ANSWERS, not crashes

const badTime = await client.callTool({
  name: "solve_route",
  arguments: { home: HOME, stops: STOPS, depart_at: "25:00" },
});
ok("a bad departure time returns isError, not a dead server", badTime.isError === true);
ok("the error says what was wrong", /Bad time/i.test(badTime.content?.[0]?.text ?? ""),
   badTime.content?.[0]?.text);

const badAddress = await client.callTool({
  name: "solve_route",
  arguments: { home: HOME, stops: [{ name: "no address", address: "   " }] },
});
ok("a blank address is caught before spending a call", badAddress.isError === true);

const badDate = await client.callTool({
  name: "latest_departure",
  arguments: { home: HOME, stops: STOPS, date: "sunday" },
});
ok("a non-ISO date returns isError", badDate.isError === true);

// the server survived every one of those
const still = await client.listTools();
ok("the server is still serving after six bad calls", still.tools.length === 2);

// ---------------------------------------------------------------- live (opt-in)

if (process.env.KOVA_ROUTE_LIVE === "1") {
  console.log("\n  live: one real solve (2 Routes API events)");
  const d = new Date(Date.now() + 26 * 3600 * 1000)
    .toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
  const res = await client.callTool({
    name: "solve_route",
    arguments: { home: HOME, stops: STOPS, depart_at: "19:00", cap: "21:00", date: d },
  });
  if (res.isError) {
    ok("live solve", false, res.content?.[0]?.text);
  } else {
    const s = res.structuredContent;
    ok("live solve returns structured content", !!s);
    ok("both stops come back in sequence", s.stops?.length === 2 && s.stops[0].seq === 1);
    ok("mileage is a real number", s.total_miles > 0);
    ok("every stop has a quotable window",
       s.stops.every((x) => /^\d\d:\d\d$/.test(x.window_start) && x.window_minutes > 0));
    ok("the window never runs past the cap",
       s.stops.every((x) => x.window_end <= s.cap));
    ok("the cap verdict agrees with the last drop",
       s.within_cap === (s.last_drop <= s.cap), `${s.last_drop} vs ${s.cap}`);
    ok("it reports what it spent", s.api_calls === 2, String(s.api_calls));
    console.log("\n" + res.content[0].text.split("\n").map((l) => "    " + l).join("\n"));
  }
} else {
  console.log("\n  (skipped the live solve — set KOVA_ROUTE_LIVE=1 to spend 2 API events)");
}

await client.close();

console.log("─".repeat(60));
console.log(`${pass} passed, ${fail} failed\n`);
process.exit(fail ? 1 : 0);
