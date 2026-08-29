# Skills

A skill is a versioned procedure: a markdown file that declares when it applies, what context it
must load before acting, and what counts as done.

The problem it solves is drift. A task done once in March and again in August should run the same
way, and shouldn't depend on whoever ran it last remembering the gotchas. Writing the procedure down
where the agent reads it — rather than in a doc a human is supposed to consult — is what makes that
true.

## Shape

```markdown
---
name: example-routine
description: One line. This is what gets matched against a request, so it carries the trigger words.
---

## When this applies
Explicit triggers. Ambiguous cases, and what to do instead.

## Load first
The canonical sources this task depends on, by identifier. Never work from memory —
a stale local copy of a rule is worse than no copy.

## Steps
The procedure.

## Done means
The observable end state.
```

## What was learned the hard way

- **A description is a trigger, not a summary.** If it doesn't contain the words someone would
  actually say out loud, the skill never fires.
- **"Load first" has to be non-negotiable.** Loading a lot of adjacent context *feels* like
  diligence and isn't. One authoritative source read first beats a thousand lines read afterward.
- **"Done means" must be observable.** "Ran the sync" is not a completion condition. "The record's
  status field reads Done" is.
- **Routines reference each other by name.** Renaming one silently breaks the others that call it,
  and nothing errors — the reference just stops matching. Count references before renaming.
