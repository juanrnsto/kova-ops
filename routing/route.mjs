#!/usr/bin/env node
/**
 * route.mjs — traffic-aware delivery routing for Kova, via the Google Routes API.
 *
 * Answers the two questions a straight-line model cannot:
 *   1. What is the best stop ORDER at a given departure time, with real traffic?
 *   2. What is the LATEST departure that still lands every drop by the courtesy cap?
 *
 * Usage:
 *   node route.mjs --stops stops.json                     # solve at the default 20:00 departure
 *   node route.mjs --stops stops.json --latest            # binary-search the latest departure that holds the cap
 *   node route.mjs --stops stops.json --depart 16:30
 *   node route.mjs --stops stops.json --cap 21:00 --date 2026-08-16
 *
 * stops.json:
 *   { "home": "Union Station, Los Angeles, CA 90012",
 *     "stops": [ { "name": "Sample Customer", "address": "300 S Santa Fe Ave, Los Angeles, CA 90013" }, ... ] }
 *
 * Billing: every call is ONE Routes API "Pro" event (TRAFFIC_AWARE_OPTIMAL). Pro carries 5,000 free
 * events/month, so Kova's ~10-25 calls/month sit far inside the free cap. --latest costs ~6 calls.
 * The script refuses to exceed MAX_CALLS in a single run so a bug cannot run up a bill.
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

const ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes";
const KEY_FILE = join(homedir(), ".config", "kova-automation", "google-maps-api-key");
const MAX_CALLS = 12;
const TZ_OFFSET = "-07:00"; // America/Los_Angeles, PDT. Change to -08:00 during PST.

let callCount = 0;

// ---------------------------------------------------------------- key

async function loadKey() {
  if (process.env.GOOGLE_MAPS_API_KEY?.trim()) return process.env.GOOGLE_MAPS_API_KEY.trim();
  try {
    const k = (await readFile(KEY_FILE, "utf8")).trim();
    if (k) return k;
  } catch { /* fall through to the message below */ }
  fail(
    `No API key found.\n\n` +
    `Put it in either place:\n` +
    `  1. ${KEY_FILE}\n` +
    `     mkdir -p ~/.config/kova-automation\n` +
    `     printf '%s' 'YOUR_KEY' > ${KEY_FILE}\n` +
    `     chmod 600 ${KEY_FILE}\n` +
    `  2. or export GOOGLE_MAPS_API_KEY=YOUR_KEY\n\n` +
    `Setup steps are in maps/README.md.`
  );
}

// ---------------------------------------------------------------- args

function args() {
  const a = process.argv.slice(2), out = { cap: "21:00", depart: "20:00", latest: false };
  for (let i = 0; i < a.length; i++) {
    const k = a[i];
    if (k === "--latest") out.latest = true;
    else if (k === "--stops")  out.stops  = a[++i];
    else if (k === "--stdin")  out.stdin  = true;
    else if (k === "--cap")    out.cap    = a[++i];
    else if (k === "--depart") out.depart = a[++i];
    else if (k === "--date")   out.date   = a[++i];
    else fail(`Unknown argument: ${k}`);
  }
  if (!out.stops && !out.stdin) fail("Missing --stops <file.json> (or --stdin). See maps/README.md.");
  return out;
}

function fail(msg) { console.error(`\n✖ ${msg}\n`); process.exit(1); }

// ---------------------------------------------------------------- time

const hhmm = (s) => {
  const m = String(s).trim().match(/^(\d{1,2}):?(\d{2})$/);
  if (!m) fail(`Bad time "${s}". Use 24-hour HH:MM, e.g. 20:00 or 16:30.`);
  const h = +m[1], mm = +m[2];
  if (h > 23 || mm > 59) fail(`Bad time "${s}".`);
  return h * 60 + mm;
};
const clock = (min) => {
  const t = ((Math.round(min) % 1440) + 1440) % 1440;
  const h = Math.floor(t / 60), m = t % 60, ap = h >= 12 ? "pm" : "am";
  return `${h % 12 === 0 ? 12 : h % 12}:${String(m).padStart(2, "0")}${ap}`;
};
const today = () => new Date().toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
const rfc3339 = (date, minutes) =>
  `${date}T${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}:00${TZ_OFFSET}`;

// ---------------------------------------------------------------- API

/**
 * The API forbids combining optimizeWaypointOrder with TRAFFIC_AWARE_OPTIMAL — verified live
 * Aug 13 2026: "optimize_waypoint_order is not supported for RoutingPreference
 * TRAFFIC_AWARE_OPTIMAL". So the run is TWO passes:
 *   1. optimize=true  + TRAFFIC_AWARE          → the traffic-aware best ORDER
 *   2. optimize=false + TRAFFIC_AWARE_OPTIMAL  → accurate per-leg times for that order
 * Two calls instead of one, which is nothing against 5,000 free Pro events a month.
 */
async function computeRoute(key, home, stops, departureISO, { optimize = false, preference = "TRAFFIC_AWARE_OPTIMAL" } = {}) {
  if (++callCount > MAX_CALLS)
    fail(`Refusing to make more than ${MAX_CALLS} API calls in one run (safety guard).`);

  const body = {
    origin: { address: home },
    destination: { address: home },          // closed loop: out and back from the same door
    intermediates: stops.map((s) => ({ address: s.address })),
    travelMode: "DRIVE",
    routingPreference: preference,
    departureTime: departureISO,
    computeAlternativeRoutes: false,
    languageCode: "en-US",
    units: "IMPERIAL",
  };
  if (optimize) body.optimizeWaypointOrder = true;

  const res = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Goog-Api-Key": key,
      "X-Goog-FieldMask": [
        "routes.duration",
        "routes.distanceMeters",
        "routes.optimizedIntermediateWaypointIndex",
        "routes.legs.duration",
        "routes.legs.distanceMeters",
      ].join(","),
    },
    body: JSON.stringify(body),
  });

  const text = await res.text();
  if (!res.ok) {
    let hint = "";
    if (res.status === 403) hint = "\n  → Usually: Routes API not enabled on the project, or billing is off.";
    if (res.status === 400 && text.includes("departureTime")) hint = "\n  → departureTime must be in the FUTURE. Use --date for a later day.";
    if (res.status === 400 && text.includes("API key")) hint = "\n  → The key is malformed, or restricted to the wrong API.";
    fail(`Routes API returned ${res.status}.${hint}\n\n${text.slice(0, 700)}`);
  }

  const data = JSON.parse(text);
  const r = data.routes?.[0];
  if (!r) fail(`No route returned. Check the addresses are geocodable.\n\n${text.slice(0, 500)}`);

  const order = r.optimizedIntermediateWaypointIndex ?? stops.map((_, i) => i);
  const legs = (r.legs ?? []).map((l) => ({
    sec: parseInt(String(l.duration ?? "0s"), 10),
    mi: (l.distanceMeters ?? 0) / 1609.344,
  }));
  return {
    order,
    legs,
    totalSec: parseInt(String(r.duration ?? "0s"), 10),
    totalMi: (r.distanceMeters ?? 0) / 1609.344,
  };
}

// ---------------------------------------------------------------- schedule

/**
 * Pass 1 then pass 2. Returns { ordered, route } where `route.legs` line up with `ordered`.
 * The order is solved once and reused across departure probes — traffic shifts leg durations,
 * not usually the best sequence, and re-optimising every probe would double the call count.
 */
async function solveOrdered(key, home, stops, departureISO, cachedOrder) {
  let order = cachedOrder;
  // A one-stop run has nothing to optimise, and asking Google to optimise a single
  // intermediate returned an order that did not map back onto `stops` — pass 2 then got
  // an undefined stop and threw on `s.address`. Found Aug 14 2026 by rehearsing the
  // routine against a live order book that happened to hold exactly one order, which is
  // a perfectly ordinary Sunday. Skip pass 1 below two stops.
  if (!order && stops.length < 2) order = stops.map((_, i) => i);
  if (!order) {
    const pass1 = await computeRoute(key, home, stops, departureISO, {
      optimize: true, preference: "TRAFFIC_AWARE",
    });
    order = pass1.order;
  }
  // Never let a bad index reach pass 2 as `undefined`; fail loudly instead of at s.address.
  const ordered = order.map((i) => stops[i]);
  if (ordered.some((s) => !s?.address)) {
    throw new Error(`optimizer returned an order that does not map onto the stops: ` +
                    `order=${JSON.stringify(order)} stops=${stops.length}`);
  }
  const pass2 = await computeRoute(key, home, ordered, departureISO, {
    optimize: false, preference: "TRAFFIC_AWARE_OPTIMAL",
  });
  return { ordered, order, route: pass2 };
}

/** Walk the returned legs, adding service time, to get each arrival + the final drop. */
function schedule(route, ordered, departMin, serviceMin) {
  const rows = [];
  let t = departMin;
  ordered.forEach((s, i) => {
    t += (route.legs[i]?.sec ?? 0) / 60;   // leg i: previous point → this stop
    rows.push({ seq: i + 1, ...s, arriveMin: t, legMi: route.legs[i]?.mi ?? 0 });
    t += serviceMin;
  });
  const lastDrop = rows.length ? rows[rows.length - 1].arriveMin : departMin;
  const homeMin = t + (route.legs[ordered.length]?.sec ?? 0) / 60; // final leg back home
  return { rows, lastDrop, homeMin };
}

// ---------------------------------------------------------------- main

/** Read the whole of stdin, so a caller can pipe JSON instead of writing a file. */
async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const s = Buffer.concat(chunks).toString("utf8").trim();
  if (!s) fail("--stdin was passed but nothing arrived on stdin.");
  return s;
}

const opt = args();
const key = await loadKey();
let raw;
try {
  raw = opt.stdin ? await readStdin() : await readFile(opt.stops, "utf8");
} catch (e) {
  fail(`Could not read stops: ${e.message}`);
}
let cfg;
try { cfg = JSON.parse(raw); }
catch (e) { fail(`Stops JSON is malformed: ${e.message}`); }
if (!cfg.home) fail(`"home" missing from ${opt.stops}.`);
if (!Array.isArray(cfg.stops) || !cfg.stops.length) fail(`"stops" missing or empty in ${opt.stops}.`);
if (cfg.stops.length > 25) fail(`${cfg.stops.length} stops exceeds the Routes API intermediate limit.`);

const date = opt.date ?? today();
const capMin = hhmm(opt.cap);
const serviceMin = Number.isFinite(cfg.serviceMinutes) ? cfg.serviceMinutes : 8;

function report(route, ordered, departMin, label) {
  const { rows, lastDrop, homeMin } = schedule(route, ordered, departMin, serviceMin);
  console.log(`\n${label}`);
  console.log(`${"─".repeat(72)}`);
  console.log(`Leave ${clock(departMin)} · ${route.totalMi.toFixed(1)} mi · ` +
              `${(route.totalSec / 3600).toFixed(2)} h driving with traffic · home ${clock(homeMin)}`);
  console.log(`${"─".repeat(72)}`);
  // Windows are built FORWARD from the ETA and clamped at the cap — not centred on the ETA.
  // Juan's ruling: he would rather beat the window than sit in the middle of it, and the
  // model runs optimistic (Aug 13 arrivals were +4 to +24 min), so a centred window turns
  // every estimate error into a late arrival. Clamping matters too: a centred window once
  // quoted "8:24–9:24pm", promising a doorbell the hard 9pm cap forbids.
  const narrow = [];
  for (const r of rows) {
    const winEnd = Math.min(r.arriveMin + 60, capMin);
    const width = Math.round(winEnd - r.arriveMin);
    if (width < 30) narrow.push({ seq: r.seq, name: r.name, width });
    const win = `${clock(r.arriveMin)}–${clock(winEnd)}`;
    console.log(` ${r.seq}. ${String(r.name).padEnd(20)} ${clock(r.arriveMin).padStart(8)}` +
                `   window ${win.padEnd(19)} ${r.legMi.toFixed(1)} mi leg` +
                (width < 30 ? `  ⚠️ only ${width}m wide` : ""));
  }
  if (narrow.length) {
    console.log(`${"─".repeat(72)}`);
    console.log(`⚠️  ${narrow.length} window(s) under 30 min wide once clamped at the cap — ` +
                `the run is too long for this departure.`);
    console.log(`    Leave earlier, or hold a stop for the next delivery day. Do NOT quote a narrow window.`);
  }
  const ok = lastDrop <= capMin;
  console.log(`${"─".repeat(72)}`);
  console.log(`${ok ? "✅" : "🔴"} Last drop ${clock(lastDrop)} — ` +
              `${ok ? `within the ${clock(capMin)} cap` : `PAST the ${clock(capMin)} cap by ${Math.round(lastDrop - capMin)} min`}`);
  return { rows, lastDrop, homeMin };
}

if (!opt.latest) {
  const departMin = hhmm(opt.depart);
  const { ordered, route } = await solveOrdered(key, cfg.home, cfg.stops, rfc3339(date, departMin));
  report(route, ordered, departMin, `TRAFFIC-AWARE ROUTE · ${date} · departing ${clock(departMin)}`);
} else {
  // Binary-search the latest departure whose FINAL drop still lands by the cap.
  // Traffic makes this non-linear, so each probe is a real API call.
  console.log(`\nSearching for the latest departure that lands every drop by ${clock(capMin)}…`);
  // departureTime must be in the FUTURE, so never probe earlier than ~15 min from now
  // (only relevant when --date is today, which is the normal case for the delivery routine).
  const nowLocal = new Date().toLocaleTimeString("en-GB", { timeZone: "America/Los_Angeles", hour12: false });
  const nowMin = +nowLocal.slice(0, 2) * 60 + +nowLocal.slice(3, 5);
  const floor = date === today() ? Math.max(8 * 60, nowMin + 15) : 8 * 60;
  if (floor >= capMin) fail(`It is already ${clock(nowMin)} — too late to search for a departure that holds ${clock(capMin)}.`);

  let lo = floor, hi = capMin, best = null, bestOrdered = null, bestDep = null, cachedOrder = null;

  for (let iter = 0; iter < 5 && hi - lo > 10; iter++) {
    const mid = Math.round((lo + hi) / 2 / 5) * 5;         // snap to 5-minute grid
    const { ordered, order, route } = await solveOrdered(key, cfg.home, cfg.stops, rfc3339(date, mid), cachedOrder);
    cachedOrder = order;                                    // solve the sequence once, reuse it
    const { lastDrop } = schedule(route, ordered, mid, serviceMin);
    const fits = lastDrop <= capMin;
    console.log(`  depart ${clock(mid)} → last drop ${clock(lastDrop)} ${fits ? "✓" : "✗"}`);
    if (fits) { best = route; bestOrdered = ordered; bestDep = mid; lo = mid; } else { hi = mid; }
  }

  if (!best) {
    fail(`Even departing ${clock(lo)} the final drop misses the ${clock(capMin)} cap.\n` +
         `  This run cannot hold the cap — split the stops across two delivery days,\n` +
         `  or spend the cap deliberately.`);
  }
  report(best, bestOrdered, bestDep, `LATEST FEASIBLE DEPARTURE · ${date}`);
  console.log(`\n➡️  Leave by ${clock(bestDep)} to keep every doorbell inside ${clock(capMin)}.`);
}

console.log(`\n📊 ${callCount} Routes API call${callCount === 1 ? "" : "s"} used ` +
            `(Pro tier · 5,000 free events/month).\n`);
