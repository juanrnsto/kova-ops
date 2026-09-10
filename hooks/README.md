# Safety hooks

Two PreToolUse hooks. They read a JSON payload on stdin describing a pending shell command, and
exit with a verdict: allow (silent), ask, or deny.

## Running the tests

```bash
python3 theme-push-guard.test.py       # run it and read the last line (61/61 as of Sep 9 2026)
python3 git-destructive-guard.test.py  # run it and read the last line (36/36 as of Sep 9 2026)
```

The suites cover: explicit-main pushes, bare pushes resolved against the current branch, compound
commands with `cd`, `gh pr merge`, `shopify theme push`, branch pushes (which must pass untouched),
quoted-command false positives, branch-lookup failure, and every permission mode.

## Installing

Register as a `PreToolUse` hook on `Bash` in your agent settings. Hook **registration** loads at
session start, so a newly wired hook is not live until the next session — but a hook's **script body is
re-read on every invocation**, so editing the logic of an already-registered hook takes effect
immediately and is testable on the spot.

## Reading the code

Start with `main()` at the bottom of each file and work up. The pattern in both:

1. Parse the payload; recover `cwd` and `permission_mode`.
2. Split the command into segments, tracking `cd` so each segment is judged against the directory it
   will actually run in.
3. Match unambiguous patterns on the string first, with no subprocess dependency — so the guard
   still fires if git itself is unavailable.
4. For ambiguous cases only, shell out for the current branch. On failure, ask.
5. Return `ask`, or `deny` under `bypassPermissions`. The deny path writes to **stderr and exits 2**:
   a `permissionDecision: "deny"` JSON is advisory and is not enforced, so a guard that printed it and
   exited 0 reported a block while the command ran anyway. Assert the **exit code**, never the verdict
   string — the string was always correct.

Step 2 is the one that was wrong for months: the payload's `cwd` is where the shell was BEFORE the
command runs, so any compound command containing a `cd` used to be judged against the wrong repo.
It was found by probing the hooks with crafted payloads — reading them said they were fine.
