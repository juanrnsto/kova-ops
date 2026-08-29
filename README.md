# kova-ops

The automation layer that runs [Kova](https://kovakream.com), a small ice cream company in Los Angeles.

I'm the founder, and I'm also the only engineer. Everything here is production code — it schedules
deliveries, guards a live storefront from bad deploys, and runs a set of AI agents against the
business systems I actually operate on: Shopify, Xero, Square, Notion, Gmail, Google Calendar,
Cloudflare.

This repository is a **curated, sanitized subset** of that stack, published as a work sample. The
private version carries customer addresses, financial detail, and API credentials; none of that is
here. What is here runs, and is tested.

```
73/73 tests passing across two safety hooks
```

---

## Why this exists

Kova's storefront is a Shopify theme on a repo whose `main` branch is GitHub-connected. Merging
`main` publishes to the live store immediately — no preview, no staging gate. I use AI agents heavily
for day-to-day work, which means an agent holding a shell can publish to a live storefront by
accident.

The written policy said *don't do that*. Prose does not stop a `git push`. So I built the
mechanical version.

That is the theme of this repo: **turning a rule into something that cannot be quietly broken.**

---

## What's here

### `hooks/` — two PreToolUse safety guards

Python hooks that intercept shell commands before an agent runs them.

| Hook | Stops |
|---|---|
| `theme-push-guard.py` | Anything that could publish the live storefront — push/merge to `main` in the theme repo, `gh pr merge`, `shopify theme push` |
| `git-destructive-guard.py` | Destructive git in any repo — hard resets, forced working-tree wipes, branch deletion |

Both ship with their own test suite (`*.test.py`, 39 and 34 cases). Run them directly:

```bash
python3 hooks/theme-push-guard.py < payload.json     # reads a hook payload on stdin
python3 hooks/theme-push-guard.test.py               # 39/39
python3 hooks/git-destructive-guard.test.py          # 34/34
```

**Three design decisions worth explaining**, because they're the interesting part:

**1. Fail safe, not fail open.** Some commands are unambiguous from the string alone (`git push
origin main`). Others need to know the current branch (`git push` with no arguments). If that branch
lookup fails, the guard **asks** rather than allows — we already know we're looking at a push inside
the theme repo, so the safe default is to interrupt.

**2. The verdict depends on who's watching.** These hooks return `ask` normally and `deny` under
`bypassPermissions`. The reason: an `ask` only works when a human is at the keyboard. In an
unattended run it asks nobody — the run *pauses forever* on a prompt nothing will answer, and a
stall becomes indistinguishable from a clean pass. Under `bypassPermissions` the prompt machinery is
exactly what's skipped, so `deny` is the only verdict that holds, and it lets the run finish and
*report* what it was stopped from doing.

Every fire appends to a local log recording the real permission mode, so the undocumented cases can
be closed with evidence instead of a guess.

**3. They follow `cd`.** This one was a bug, and finding it is the part I'd want to be judged on.

Both hooks read the payload's `cwd` — which is where the shell was *before* the command runs. So any
compound command containing a `cd` was being judged against the wrong repository. Two different
failures, one cause: the theme guard blocked a legitimate push in an unrelated docs repo (safe, but
a guard that cries wolf gets routed around), while the destructive guard **silently allowed** a
forced working-tree wipe of a dirty repo reached by `cd` from a clean one.

Separately, the theme guard's parser skipped any command segment without a `git` token — so
`gh pr merge` returned a silent ALLOW. A live deploy path the documentation already claimed was
guarded.

**All three were found by probing the hooks with crafted payloads. Reading the code said they were
fine.** That's why the test suites exist and why they're this large.

### `routing/` — traffic-aware delivery routing

`route.mjs` solves the delivery run against the Google Routes API. It answers two questions a
straight-line distance model can't:

1. What's the best stop **order** at a given departure time, in real traffic?
2. What's the **latest** departure that still lands every drop before the courtesy cutoff?

The second is a binary search over departure times. It costs about six API calls, and the script
refuses to exceed a hard call ceiling in a single run so a bug can't run up a bill.

`sample-stops.json` is synthetic. Real stop files are generated per delivery day from unfulfilled
paid orders and are gitignored — they're customer addresses.

### `skills/` and `scheduled/`

How the agent layer is organized: reusable skills with explicit trigger conditions, and scheduled
routines that run against live business data. See each directory's README.

---

## Architecture

```
                      ┌─────────────────────────────┐
   business systems   │  Shopify · Xero · Square     │
                      │  Notion · Gmail · Calendar   │
                      └──────────────┬───────────────┘
                                     │  MCP connectors
                      ┌──────────────▼───────────────┐
   agent layer        │  skills (triggered)          │
                      │  scheduled routines (cron)   │
                      └──────────────┬───────────────┘
                                     │  shell
                      ┌──────────────▼───────────────┐
   safety layer       │  PreToolUse hooks            │  ← this repo
                      │  ask / deny by permission    │
                      └──────────────┬───────────────┘
                                     │
                      ┌──────────────▼───────────────┐
   real world         │  live storefront · deliveries │
                      └───────────────────────────────┘

   Cloudflare Worker receives Shopify webhooks → logs to Notion → push alerts
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full write-up, including the failure modes that
shaped it.

---

## What I'd want you to take from this

- I write **defensive** infrastructure. The interesting decisions here are about what happens when
  something fails, not when it works.
- I **test what I can't see**. The three real bugs in these hooks were invisible to code review and
  obvious to a crafted payload.
- I build against **live systems with real consequences** — a bad merge publishes to a storefront
  that customers are looking at, and a bad route means ice cream melts.
- I document **why**, not just what. Every non-obvious decision in this repo carries its reasoning,
  because the next person to touch it is usually me, six weeks later, having forgotten.

---

## Contact

Juan Turcios — Los Angeles
[juanernesto.com](https://juanernesto.com) · [kovakream.com](https://kovakream.com)
