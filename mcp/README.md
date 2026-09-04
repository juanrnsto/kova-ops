# `mcp/` — kova-route, an MCP server

Kova delivers in one van, on one night, in Los Angeles traffic. Two questions decide the run,
and a straight-line distance model answers neither:

1. What is the best stop **order** at a given departure time, with real traffic?
2. What is the **latest departure** that still lands every drop by the courtesy cap?

The CLI in [`../routing/route.mjs`](../routing/route.mjs) has answered both since August 2026.
This is the same method exposed as an **MCP server**, so an agent can ask instead of a terminal.

```
claude mcp add kova-route -- node /absolute/path/to/kova-ops/mcp/server.mjs
```

Then: *"I have five drops tonight, here are the addresses — how late can I leave?"*

## The two tools

| tool | question | cost |
|---|---|---|
| `solve_route` | Best order leaving at `depart_at`; ETA, window and mileage per stop; does the last drop hold the cap | 2 Routes API events |
| `latest_departure` | Binary-searches the latest departure that holds the cap, then returns that schedule | up to 10 events |

Both take `home`, `stops[]`, `cap`, `date`, `service_minutes`; both return **structured
content** against a declared output schema, plus a one-screen human summary.

## What is actually involved in "building an MCP server"

Less than the phrase suggests, and the interesting parts are not the protocol:

- **`server.mjs`** declares the two tools — a title, a description an agent can route on, a
  Zod input schema that becomes JSON Schema on the wire, an output schema, and annotations
  (`readOnlyHint`, `openWorldHint`). The SDK handles the handshake, `tools/list` and
  `tools/call`; the transport is stdio, so a client runs this file as a subprocess.
- **`lib/route-core.mjs`** is the engine, and the differences from the CLI are the whole
  reason it is a separate file:

  | CLI (`routing/route.mjs`) | server core |
  |---|---|
  | `process.exit(1)` on bad input | throws `RouteError` — a server has to survive it |
  | prints a table | returns data |
  | one module-level call counter | a budget object per request, plus a process ceiling |

- The routing method itself is unchanged, including the finding that forced it: Routes API
  refuses to combine `optimizeWaypointOrder` with `TRAFFIC_AWARE_OPTIMAL` (verified live
  Aug 13 2026), so every solve is **two** calls — pass 1 optimises the order under
  `TRAFFIC_AWARE`, pass 2 prices that order under `TRAFFIC_AWARE_OPTIMAL`.

The CLI is deliberately **not** refactored to import this core. It routes real deliveries and
its operational original lives outside this repo; a public mirror is the wrong place to fork
running code.

## Three decisions worth naming

- **The key is read on the first tool call, never at boot.** A client starts every configured
  server at once, and a server that refuses to start is harder to diagnose than a tool that
  says why. Missing key → a tool error with the two places to put one.
- **A domain failure is an answer, not a crash.** Bad time, ungeocodable address, a departure
  in the past, no feasible departure at all — each returns `isError` with the reason the
  caller can act on. `test/smoke.mjs` makes six bad calls and then asserts the server is
  still serving.
- **Two budgets, because a server outlives a request.** The CLI counts calls in a module
  variable and exits; a long-lived server needs a per-request ceiling (12) *and* a per-process
  one (250), or one looping client spends a month of quota. Pro tier gives 5,000 free events
  a month against Kova's real 10–25.

## Windows are quoted forward from the ETA, and clamped

Not centred on it. The model runs optimistic — Aug 13 2026 arrivals were +4 to +24 minutes —
so a centred window turns every estimate error into a late arrival. The clamp is separate: a
centred window once quoted "8:24–9:24pm", promising a doorbell the 9pm cap forbids. A window
narrower than 30 minutes after clamping comes back flagged, with instructions not to quote it.

## Tests

```
npm install
npm test                      # 16 assertions, no API calls, no key needed
KOVA_ROUTE_LIVE=1 npm test    # adds one real solve (2 Routes API events)
```

The test drives the server over stdio with the SDK's own `Client`, because reading the source
proves nothing about a protocol — only a handshake does. Last live run: 23/23.

## Requirements

Node 20+. A Google Maps API key with the **Routes API** enabled, in
`~/.config/kova-automation/google-maps-api-key` (chmod 600) or `$GOOGLE_MAPS_API_KEY`.
No key is stored in this repo, and no error message can carry one out to a client.
