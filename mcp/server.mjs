#!/usr/bin/env node
/**
 * kova-route — an MCP server exposing Kova's traffic-aware delivery routing as two tools.
 *
 * Kova's delivery night has two questions, and a straight-line distance model answers neither:
 *   1. What is the best stop ORDER at a given departure time, with real traffic?
 *   2. What is the LATEST departure that still lands every drop by the courtesy cap?
 *
 * The CLI in `../routing/route.mjs` has answered both since Aug 2026. This server answers them
 * for an agent instead of for a terminal: it declares the arguments as schemas, returns
 * structured data rather than a printed table, and stays alive through bad input.
 *
 * Transport is stdio, so a client launches it as a subprocess. Wire it up with:
 *
 *   claude mcp add kova-route -- node /absolute/path/to/kova-ops/mcp/server.mjs
 *
 * Needs a Google Maps API key with the Routes API enabled, in either
 * ~/.config/kova-automation/google-maps-api-key or $GOOGLE_MAPS_API_KEY. The key is read
 * lazily on the first tool call, never at boot — a client starts every configured server at
 * once, and a server that refuses to start is harder to diagnose than a tool that says why.
 *
 * Cost: every solve is two Routes API "Pro" events, and a departure search is up to ten.
 * Pro carries 5,000 free events a month against Kova's ~10-25 real calls, and both a
 * per-request and a per-process budget are enforced in lib/route-core.mjs so a looping
 * client cannot spend the quota.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import {
  loadKey, makeBudget, makeProcessBudget, planRoute, latestDeparture,
  summarize, RouteError, MAX_STOPS, DEFAULT_SERVICE_MIN,
} from "./lib/route-core.mjs";

const VERSION = "1.0.0";
const CALLS_PER_REQUEST = 12;
const processBudget = makeProcessBudget(250);

// ------------------------------------------------------------------ schemas

const stopInput = z.object({
  name: z.string().optional().describe("Who the drop is for. Used in the quoted schedule."),
  address: z.string().describe("A geocodable street address."),
});

const commonInput = {
  home: z.string().describe("The address the run starts and ends at. The route is a closed loop."),
  stops: z.array(stopInput).min(1).max(MAX_STOPS)
    .describe(`The drops, in any order — the tool solves the order. Max ${MAX_STOPS} (Routes API limit).`),
  cap: z.string().optional()
    .describe('Latest acceptable doorbell, 24-hour HH:MM. Default "21:00".'),
  date: z.string().optional()
    .describe("Delivery date, YYYY-MM-DD. Defaults to today in Los Angeles."),
  service_minutes: z.number().optional()
    .describe(`Minutes spent at each door. Default ${DEFAULT_SERVICE_MIN}.`),
};

const stopOutput = z.object({
  seq: z.number(), name: z.string(), address: z.string(),
  eta: z.string(), window_start: z.string(), window_end: z.string(),
  window_minutes: z.number(), leg_miles: z.number(), narrow_window: z.boolean(),
});

const scheduleOutput = {
  date: z.string(),
  depart_at: z.string(),
  cap: z.string(),
  service_minutes: z.number(),
  total_miles: z.number(),
  driving_hours: z.number(),
  home_at: z.string(),
  last_drop: z.string(),
  within_cap: z.boolean(),
  minutes_past_cap: z.number(),
  stops: z.array(stopOutput),
  warnings: z.array(z.string()),
  api_calls: z.number(),
};

// The infeasible answer is a real answer, not an error, and it carries no schedule —
// so every schedule field is optional on this tool's output.
const latestOutput = {
  feasible: z.boolean(),
  latest_depart: z.string().nullable(),
  probes: z.array(z.object({ depart: z.string(), last_drop: z.string(), fits: z.boolean() })),
  date: z.string(),
  cap: z.string(),
  api_calls: z.number(),
  warnings: z.array(z.string()),
  ...Object.fromEntries(
    Object.entries(scheduleOutput)
      .filter(([k]) => !["date", "cap", "api_calls", "warnings"].includes(k))
      .map(([k, v]) => [k, v.optional()])),
};

// ------------------------------------------------------------------ plumbing

let keyPromise = null;
const key = () => (keyPromise ??= loadKey());

/**
 * One place where a tool's result is built, because the failure path is the part that has to
 * be right: a thrown RouteError is the tool's ANSWER (isError, with the reason the caller can
 * act on), not a crash. Anything unexpected is reported without its stack, so a message
 * cannot carry file paths or a key out to the client.
 */
async function respond(run) {
  try {
    const result = await run(await key(), makeBudget(CALLS_PER_REQUEST, processBudget));
    return {
      content: [{ type: "text", text: summarize(result) }],
      structuredContent: result,
    };
  } catch (e) {
    const known = e instanceof RouteError;
    if (!known) process.stderr.write(`kova-route: unexpected error: ${e?.stack ?? e}\n`);
    return {
      isError: true,
      content: [{ type: "text", text: known ? e.message : `Unexpected server error: ${e?.message ?? e}` }],
    };
  }
}

// ------------------------------------------------------------------ the server

const server = new McpServer(
  { name: "kova-route", version: VERSION },
  { instructions:
      "Traffic-aware delivery routing for a closed loop from one home address. Use solve_route " +
      "when the departure time is decided, and latest_departure when the question is how late " +
      "the run can start. Quote customers the window the tools return, never the bare ETA." },
);

server.registerTool("solve_route", {
  title: "Solve a delivery route",
  description:
    "Order the stops for the best traffic-aware route leaving at a given time, and return each " +
    "stop's ETA, the customer window to quote, mileage, the time home, and whether the last drop " +
    "holds the cap. Costs two Google Routes API calls.",
  inputSchema: {
    ...commonInput,
    depart_at: z.string().optional()
      .describe('Departure time, 24-hour HH:MM. Default "20:00". Must be in the future.'),
  },
  outputSchema: scheduleOutput,
  annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: false, openWorldHint: true },
}, (args) => respond((k, budget) => planRoute(args, { key: k, budget })));

server.registerTool("latest_departure", {
  title: "Find the latest feasible departure",
  description:
    "Binary-search the latest departure time whose final drop still lands by the cap, then return " +
    "that schedule. Reports feasible: false when no departure holds the cap. Costs up to ten " +
    "Google Routes API calls.",
  inputSchema: commonInput,
  outputSchema: latestOutput,
  annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: false, openWorldHint: true },
}, (args) => respond((k, budget) => latestDeparture(args, { key: k, budget })));

// ------------------------------------------------------------------ boot

const transport = new StdioServerTransport();
await server.connect(transport);
// stdout is the protocol channel. Anything this process wants to say goes to stderr.
process.stderr.write(`kova-route ${VERSION} ready on stdio\n`);
