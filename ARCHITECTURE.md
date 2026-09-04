# Architecture

How the pieces fit, and the failure modes that shaped them.

## The stack

**Business systems.** Shopify (storefront, orders, subscriptions), Xero (books), Square (in-person
sales across four locations), Notion (task and content databases), Gmail, Google Calendar,
Cloudflare (Worker + Pages). Each is reached through an MCP connector rather than a hand-rolled
client, so credentials live in one place and the agent layer talks to all of them the same way.

**Agent layer.** Two shapes:

- **Skills** — reusable procedures with explicit trigger conditions. A skill is a markdown file
  describing when it applies, what it must load first, and what "done" means. The point is that the
  *procedure* is version-controlled, so a task done in March and the same task done in August run
  the same way.
- **Scheduled routines** — cron-triggered passes over live data: nightly task rollover, a weekly
  reconciliation check, a media sweep.
- **Tools of my own** — `mcp/` is an MCP server I had built for the delivery routing, so the two
  questions the `routing/` CLI answers in a terminal are also callable by an agent. Everywhere else
  in the stack I consume connectors somebody else wrote; this is the one I author.

**Safety layer.** PreToolUse hooks that see every shell command before it runs. This repo.

**Real world.** A live storefront, a delivery route, and a freezer with a hard capacity ceiling.

---

## Failure modes that shaped the design

### A rule written in prose is not enforced

The storefront deploy policy existed in documentation for months. Nothing mechanically stopped a
push to `main`. The hooks don't replace the policy — they make breaking it non-silent.

**Generalization:** if a rule matters, ask what physically happens when someone ignores it. If the
answer is "nothing," the rule is a preference.

### A guard that cries wolf gets routed around

The first version of the theme guard fired on a legitimate push in an unrelated repository. That's
"safe" in the narrow sense and corrosive in practice — a guard people learn to dismiss is worse than
no guard, because it trains the dismissal reflex.

**Generalization:** false positives have a real cost, and it is paid later.

### The scheduler runs a different Python than you do

`python3` on the interactive PATH is 3.12. Under launchd and hook execution it's 3.9.6. Anything
using a newer stdlib module or syntax works by hand and fails silently in production.

These hooks are written for 3.9 deliberately — no `tomllib`, no `match`/`case`, no `X | Y` type
unions. Same class of problem as bare Homebrew binaries failing under a minimal PATH.

**Generalization:** the environment that runs your code on a schedule is not the environment you
tested it in.

### An `ask` that nobody can answer is a hang

Covered in the README. The short version: an interactive safety prompt in an unattended run doesn't
protect anything, it just stops the run in a way that looks like success from the outside.

**Generalization:** every interactive fallback needs a defined non-interactive behaviour.

### String matching cannot tell a command from data

Both guards match on the command string, so quoting a dangerous command inside a commit message or
a test fixture trips them. This is inherent to the approach and it fails in the safe direction, so
it stays — but it means anything carrying such text goes in a file passed by path (`git commit -F`)
rather than inline.

**Generalization:** know which of your false positives are structural, and document the workaround
next to the rule.

---

### A script exits; a server has to survive

The routing CLI calls `process.exit(1)` on a bad address, a bad time, a missing key. That is correct
for a script: it runs once, a person is watching, and the exit code is the message. Wrapping the same
logic in an MCP server inverts every one of those assumptions. Nobody is watching, the process is
meant to outlive the request, and an exit takes down a tool the client has already listed.

So the server core throws instead, and one `respond()` wrapper turns a domain failure into the tool's
*answer* — `isError` plus the reason the caller can act on. A bad time, an ungeocodable address, a
departure in the past, no feasible departure at all: each is a result, not a crash. The test suite
makes six malformed calls and then asserts the server is still serving, because that is the property
that actually matters and the one no amount of reading the source can confirm.

Two more inversions came with it:

- **The API key is read on the first tool call, not at boot.** A client starts every configured
  server at once. A server that refuses to start is harder to diagnose than a tool that says which
  two places a key can live.
- **The call budget had to become two budgets.** The CLI counts calls in a module variable and then
  exits, which is a ceiling by accident. A long-lived server needs a limit per request *and* per
  process, or one looping client spends a month of quota against a metered API.

---

## What's deliberately not here

- Customer data of any kind. Delivery stop files are generated per run and gitignored.
- Financial detail — costs, margins, revenue.
- Credentials. All secrets live outside the repo in a config directory, never in code.
- The DaVinci Resolve render pipeline, the Instagram reader, and the livestream rig control layer.
  They're real and they work; they're just not what this sample is about.
