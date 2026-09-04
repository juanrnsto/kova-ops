/**
 * route-core.mjs — the delivery-routing engine, in a form a server can call.
 *
 * This is the same two-pass Google Routes API method proven in `../../routing/route.mjs`,
 * which is the CLI Kova's delivery routine actually runs. That script is deliberately NOT
 * refactored to import this module: it runs live deliveries, and its operational original
 * lives outside this repo. This module is the server-safe form of the same method, and the
 * differences are the whole reason it exists:
 *
 *   CLI (route.mjs)                     server core (this file)
 *   ─────────────────────────────       ──────────────────────────────
 *   process.exit(1) on bad input   →    throws RouteError (a server must survive it)
 *   console.log a table            →    returns structured data
 *   one module-level call counter  →    a budget object passed per request
 *
 * The API method itself is unchanged, including the finding that forced it: Routes API
 * refuses to combine optimizeWaypointOrder with TRAFFIC_AWARE_OPTIMAL ("optimize_waypoint_order
 * is not supported for RoutingPreference TRAFFIC_AWARE_OPTIMAL", verified live Aug 13 2026),
 * so every solve is two calls — pass 1 optimises the ORDER under TRAFFIC_AWARE, pass 2 prices
 * that order under TRAFFIC_AWARE_OPTIMAL.
 */

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

const ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes";
const KEY_FILE = join(homedir(), ".config", "kova-automation", "google-maps-api-key");
const TZ = "America/Los_Angeles";

export const MAX_STOPS = 25;          // Routes API intermediate-waypoint limit
export const DEFAULT_SERVICE_MIN = 8; // minutes at the door

/** Every bad input, bad key and bad upstream response arrives as this. Never process.exit. */
export class RouteError extends Error {
  constructor(message, { hint } = {}) {
    super(hint ? `${message}\n${hint}` : message);
    this.name = "RouteError";
  }
}

// ------------------------------------------------------------------ budget

/**
 * A per-request call budget. The CLI has one module-level counter because it runs once and
 * exits; a long-lived server needs the ceiling to be per request AND per process, or one
 * looping client can spend a month of quota. Routes API "Pro" gives 5,000 free events/month.
 */
export function makeBudget(perRequest = 12, shared = null) {
  let used = 0;
  return {
    spend() {
      if (++used > perRequest)
        throw new RouteError(`Refusing to make more than ${perRequest} Routes API calls for one request.`);
      if (shared) shared.spend();
    },
    get used() { return used; },
  };
}

/** Process-wide ceiling, so an unattended client cannot run up a bill across many requests. */
export function makeProcessBudget(max = 250) {
  let used = 0;
  return {
    spend() {
      if (++used > max)
        throw new RouteError(
          `This server has used its process budget of ${max} Routes API calls. Restart it deliberately.`);
    },
    get used() { return used; },
    get max() { return max; },
  };
}

// ------------------------------------------------------------------ key

export async function loadKey() {
  if (process.env.GOOGLE_MAPS_API_KEY?.trim()) return process.env.GOOGLE_MAPS_API_KEY.trim();
  try {
    const k = (await readFile(KEY_FILE, "utf8")).trim();
    if (k) return k;
  } catch { /* fall through */ }
  throw new RouteError("No Google Maps API key found.", {
    hint: `Put it in either place:\n` +
          `  1. ${KEY_FILE}   (chmod 600)\n` +
          `  2. export GOOGLE_MAPS_API_KEY=...`,
  });
}

// ------------------------------------------------------------------ time

/** "20:00" | "8:30" -> minutes past midnight. Throws on anything else. */
export function hhmm(s) {
  const m = String(s ?? "").trim().match(/^(\d{1,2}):?(\d{2})$/);
  if (!m) throw new RouteError(`Bad time "${s}". Use 24-hour HH:MM, e.g. 20:00 or 16:30.`);
  const h = +m[1], mm = +m[2];
  if (h > 23 || mm > 59) throw new RouteError(`Bad time "${s}".`);
  return h * 60 + mm;
}

/** minutes past midnight -> "20:24". 24-hour on purpose: this is machine-facing output. */
export const hm24 = (min) => {
  const t = ((Math.round(min) % 1440) + 1440) % 1440;
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};

/** minutes past midnight -> "8:24pm", for the human-readable summary only. */
export const clock = (min) => {
  const t = ((Math.round(min) % 1440) + 1440) % 1440;
  const h = Math.floor(t / 60), m = t % 60;
  return `${h % 12 === 0 ? 12 : h % 12}:${String(m).padStart(2, "0")}${h >= 12 ? "pm" : "am"}`;
};

export const todayLA = () => new Date().toLocaleDateString("en-CA", { timeZone: TZ });

export function nowMinutesLA() {
  const t = new Date().toLocaleTimeString("en-GB", { timeZone: TZ, hour12: false });
  return +t.slice(0, 2) * 60 + +t.slice(3, 5);
}

/**
 * The UTC offset Los Angeles is actually on, on the given DATE — not a constant.
 *
 * The CLI this method came from hardcodes "-07:00" with a comment to change it during PST, and
 * that constant is a dated time bomb: clocks go back on 1 Nov 2026, after which every arrival time
 * it reports is an hour out, silently, in a season when the 9pm cap is tightest. A server published
 * as a work sample cannot carry that, so the offset is derived instead.
 *
 * Noon UTC is used as the probe instant deliberately: it is never within a DST transition in this
 * zone, so the date's offset is unambiguous.
 */
function zoneOffset(date) {
  const probe = new Date(`${date}T12:00:00Z`);
  const name = new Intl.DateTimeFormat("en-US", { timeZone: TZ, timeZoneName: "longOffset" })
    .formatToParts(probe).find((p) => p.type === "timeZoneName")?.value ?? "GMT+00:00";
  const off = name.replace("GMT", "").trim();
  return off === "" ? "+00:00" : off;          // "GMT" alone means UTC
}

/** Exported for the test suite only: the DST flip is the thing worth locking. */
export const zoneOffsetForTest = zoneOffset;

const rfc3339 = (date, minutes) =>
  `${date}T${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}:00${zoneOffset(date)}`;

// ------------------------------------------------------------------ validation

/** Validate a request's shape before spending a single API call on it. */
export function normalizeRequest({ home, stops, cap, date, service_minutes }) {
  if (!home || typeof home !== "string" || !home.trim())
    throw new RouteError(`"home" must be a geocodable address string.`);
  if (!Array.isArray(stops) || stops.length === 0)
    throw new RouteError(`"stops" must be a non-empty array of { name, address }.`);
  if (stops.length > MAX_STOPS)
    throw new RouteError(`${stops.length} stops exceeds the Routes API limit of ${MAX_STOPS} intermediates.`);
  stops.forEach((s, i) => {
    if (!s?.address || typeof s.address !== "string" || !s.address.trim())
      throw new RouteError(`stops[${i}] is missing an "address".`);
  });

  const d = date ?? todayLA();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) throw new RouteError(`Bad date "${d}". Use YYYY-MM-DD.`);
  const serviceMin = Number.isFinite(service_minutes) ? service_minutes : DEFAULT_SERVICE_MIN;
  if (serviceMin < 0 || serviceMin > 120)
    throw new RouteError(`service_minutes must be between 0 and 120.`);

  return {
    home: home.trim(),
    stops: stops.map((s, i) => ({ name: s.name?.trim() || `Stop ${i + 1}`, address: s.address.trim() })),
    capMin: hhmm(cap ?? "21:00"),
    date: d,
    serviceMin,
  };
}

/**
 * departureTime must be in the FUTURE or the API returns 400. The CLI hits this as a
 * confusing upstream error; a tool should say so before spending the call.
 */
function assertFutureDeparture(date, departMin) {
  if (date !== todayLA()) return;
  const now = nowMinutesLA();
  if (departMin <= now + 1)
    throw new RouteError(
      `departure ${hm24(departMin)} is not in the future (it is ${hm24(now)} in Los Angeles).`,
      { hint: `Routes API requires a future departureTime. Pass a later time, or a later "date".` });
}

// ------------------------------------------------------------------ API

async function computeRoute(key, home, stops, departureISO, { optimize, preference, budget }) {
  budget.spend();

  const body = {
    origin: { address: home },
    destination: { address: home },      // closed loop: out and back from the same door
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
    if (res.status === 403) hint = "Usually: Routes API not enabled on the project, or billing is off.";
    else if (res.status === 400 && text.includes("departureTime")) hint = "departureTime must be in the future.";
    else if (res.status === 400 && text.includes("API key")) hint = "The key is malformed, or restricted to the wrong API.";
    throw new RouteError(`Routes API returned ${res.status}. ${text.slice(0, 500)}`, { hint });
  }

  let data;
  try { data = JSON.parse(text); }
  catch { throw new RouteError(`Routes API returned unparseable JSON.`); }

  const r = data.routes?.[0];
  if (!r) throw new RouteError(`No route returned — check the addresses are geocodable.`);

  return {
    order: r.optimizedIntermediateWaypointIndex ?? stops.map((_, i) => i),
    legs: (r.legs ?? []).map((l) => ({
      sec: parseInt(String(l.duration ?? "0s"), 10),
      mi: (l.distanceMeters ?? 0) / 1609.344,
    })),
    totalSec: parseInt(String(r.duration ?? "0s"), 10),
    totalMi: (r.distanceMeters ?? 0) / 1609.344,
  };
}

/**
 * Pass 1 (order) then pass 2 (accurate legs). `cachedOrder` lets a departure search reuse a
 * solved sequence: traffic shifts leg durations, not usually the best sequence, and
 * re-optimising every probe would double the call count.
 */
async function solveOrdered(key, home, stops, departureISO, budget, cachedOrder = null) {
  let order = cachedOrder;
  // A single intermediate has nothing to optimise, and asking Google to optimise one returned
  // an order that did not map back onto `stops` — pass 2 then got an undefined stop. Found
  // Aug 14 2026 against a live order book holding exactly one order, which is an ordinary Sunday.
  if (!order && stops.length < 2) order = stops.map((_, i) => i);
  if (!order) {
    const pass1 = await computeRoute(key, home, stops, departureISO,
      { optimize: true, preference: "TRAFFIC_AWARE", budget });
    order = pass1.order;
  }
  const ordered = order.map((i) => stops[i]);
  if (ordered.some((s) => !s?.address))
    throw new RouteError(
      `The optimizer returned an order that does not map onto the stops ` +
      `(order=${JSON.stringify(order)}, stops=${stops.length}).`);

  const pass2 = await computeRoute(key, home, ordered, departureISO,
    { optimize: false, preference: "TRAFFIC_AWARE_OPTIMAL", budget });
  return { ordered, order, route: pass2 };
}

/** Walk the legs, adding service time, to get each arrival, the final drop and the way home. */
function walk(route, ordered, departMin, serviceMin) {
  const rows = [];
  let t = departMin;
  ordered.forEach((s, i) => {
    t += (route.legs[i]?.sec ?? 0) / 60;      // leg i: previous point -> this stop
    rows.push({ seq: i + 1, ...s, arriveMin: t, legMi: route.legs[i]?.mi ?? 0 });
    t += serviceMin;
  });
  return {
    rows,
    lastDrop: rows.length ? rows[rows.length - 1].arriveMin : departMin,
    homeMin: t + (route.legs[ordered.length]?.sec ?? 0) / 60,
  };
}

/**
 * Build the customer-facing windows. Forward from the ETA and CLAMPED at the cap — not
 * centred on it. Juan's ruling: he would rather beat the window than sit in the middle of
 * one, and the model runs optimistic (Aug 13 arrivals were +4 to +24 min), so a centred
 * window turns every estimate error into a late arrival. The clamp matters independently:
 * a centred window once quoted "8:24-9:24pm", promising a doorbell the 9pm cap forbids.
 */
function windows(rows, capMin) {
  const warnings = [];
  const stops = rows.map((r) => {
    const winEnd = Math.min(r.arriveMin + 60, capMin);
    const width = Math.round(winEnd - r.arriveMin);
    if (width < 30)
      warnings.push(`Stop ${r.seq} (${r.name}) has only a ${width}-minute window once clamped at the cap — do not quote it.`);
    return {
      seq: r.seq,
      name: r.name,
      address: r.address,
      eta: hm24(r.arriveMin),
      window_start: hm24(r.arriveMin),
      window_end: hm24(winEnd),
      window_minutes: width,
      leg_miles: +r.legMi.toFixed(1),
      narrow_window: width < 30,
    };
  });
  if (warnings.length)
    warnings.push("The run is too long for this departure: leave earlier, or hold a stop for the next delivery day.");
  return { stops, warnings };
}

function shape({ date, departMin, capMin, route, ordered, serviceMin, budget }) {
  const { rows, lastDrop, homeMin } = walk(route, ordered, departMin, serviceMin);
  const { stops, warnings } = windows(rows, capMin);
  return {
    date,
    depart_at: hm24(departMin),
    cap: hm24(capMin),
    service_minutes: serviceMin,
    total_miles: +route.totalMi.toFixed(1),
    driving_hours: +(route.totalSec / 3600).toFixed(2),
    home_at: hm24(homeMin),
    last_drop: hm24(lastDrop),
    within_cap: lastDrop <= capMin,
    minutes_past_cap: Math.max(0, Math.round(lastDrop - capMin)),
    stops,
    warnings,
    api_calls: budget.used,
  };
}

// ------------------------------------------------------------------ the two questions

/** What is the best stop ORDER leaving at a given time, and does it hold the cap? */
export async function planRoute(input, { key, budget }) {
  const { home, stops, capMin, date, serviceMin } = normalizeRequest(input);
  const departMin = hhmm(input.depart_at ?? "20:00");
  assertFutureDeparture(date, departMin);

  const { ordered, route } = await solveOrdered(key, home, stops, rfc3339(date, departMin), budget);
  return shape({ date, departMin, capMin, route, ordered, serviceMin, budget });
}

/**
 * What is the LATEST departure whose final drop still lands by the cap? Traffic makes this
 * non-linear, so each probe is a real API call — hence the binary search and the 5-minute grid.
 */
export async function latestDeparture(input, { key, budget }) {
  const { home, stops, capMin, date, serviceMin } = normalizeRequest(input);

  const floor = date === todayLA() ? Math.max(8 * 60, nowMinutesLA() + 15) : 8 * 60;
  if (floor >= capMin)
    throw new RouteError(
      `It is already ${hm24(nowMinutesLA())} in Los Angeles — too late to search for a departure that holds ${hm24(capMin)}.`);

  let lo = floor, hi = capMin, best = null, bestOrdered = null, bestDep = null, cachedOrder = null;
  const probes = [];

  for (let iter = 0; iter < 5 && hi - lo > 10; iter++) {
    const mid = Math.round((lo + hi) / 2 / 5) * 5;               // snap to a 5-minute grid
    const { ordered, order, route } =
      await solveOrdered(key, home, stops, rfc3339(date, mid), budget, cachedOrder);
    cachedOrder = order;                                          // solve the sequence once, reuse it
    const { lastDrop } = walk(route, ordered, mid, serviceMin);
    const fits = lastDrop <= capMin;
    probes.push({ depart: hm24(mid), last_drop: hm24(lastDrop), fits });
    if (fits) { best = route; bestOrdered = ordered; bestDep = mid; lo = mid; } else { hi = mid; }
  }

  if (!best) {
    return {
      feasible: false,
      latest_depart: null,
      probes,
      date,
      cap: hm24(capMin),
      api_calls: budget.used,
      warnings: [
        `Even departing ${hm24(lo)} the final drop misses the ${hm24(capMin)} cap.`,
        `Split the stops across two delivery days, or spend the cap deliberately.`,
      ],
    };
  }

  return {
    feasible: true,
    latest_depart: hm24(bestDep),
    probes,
    ...shape({ date, departMin: bestDep, capMin, route: best, ordered: bestOrdered, serviceMin, budget }),
  };
}

/** A one-screen human summary, so a chat client shows something readable beside the JSON. */
export function summarize(r) {
  const lines = [];
  if (r.feasible === false)
    return `No feasible departure holds the ${r.cap} cap on ${r.date}.\n` + r.warnings.map((w) => `  - ${w}`).join("\n");
  if (r.latest_depart) lines.push(`Latest feasible departure: ${clock(hhmm(r.latest_depart))}`);
  lines.push(`${r.date} - leave ${clock(hhmm(r.depart_at))} - ${r.total_miles} mi - ` +
             `${r.driving_hours} h driving with traffic - home ${clock(hhmm(r.home_at))}`);
  for (const s of r.stops)
    lines.push(`  ${s.seq}. ${s.name} - ETA ${clock(hhmm(s.eta))}, quote ` +
               `${clock(hhmm(s.window_start))}-${clock(hhmm(s.window_end))}` +
               (s.narrow_window ? `  (only ${s.window_minutes}m wide)` : ""));
  lines.push(r.within_cap
    ? `Last drop ${clock(hhmm(r.last_drop))} - within the ${clock(hhmm(r.cap))} cap.`
    : `Last drop ${clock(hhmm(r.last_drop))} - PAST the ${clock(hhmm(r.cap))} cap by ${r.minutes_past_cap} min.`);
  for (const w of r.warnings) lines.push(`  ! ${w}`);
  lines.push(`${r.api_calls} Routes API call${r.api_calls === 1 ? "" : "s"} used (Pro tier, 5,000 free events/month).`);
  return lines.join("\n");
}
