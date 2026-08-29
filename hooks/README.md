# Safety hooks

Two PreToolUse hooks. They read a JSON payload on stdin describing a pending shell command, and
exit with a verdict: allow (silent), ask, or deny.

## Running the tests

```bash
python3 theme-push-guard.test.py       # 39/39
python3 git-destructive-guard.test.py  # 34/34
```

The suites cover: explicit-main pushes, bare pushes resolved against the current branch, compound
commands with `cd`, `gh pr merge`, `shopify theme push`, branch pushes (which must pass untouched),
quoted-command false positives, branch-lookup failure, and every permission mode.

## Installing

Register as a `PreToolUse` hook on `Bash` in your agent settings. Hooks load at session start — one
wired mid-session is not live until the next one.

## Reading the code

Start with `main()` at the bottom of each file and work up. The pattern in both:

1. Parse the payload; recover `cwd` and `permission_mode`.
2. Split the command into segments, tracking `cd` so each segment is judged against the directory it
   will actually run in.
3. Match unambiguous patterns on the string first, with no subprocess dependency — so the guard
   still fires if git itself is unavailable.
4. For ambiguous cases only, shell out for the current branch. On failure, ask.
5. Return `ask`, or `deny` under `bypassPermissions`.

Step 2 is the one that was wrong for months. See the README's third design note.
