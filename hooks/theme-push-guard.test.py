"""Test suite for theme-push-guard.py.

⚠️ HERMETIC BY DESIGN — fixed Aug 14 2026, do not "simplify" back to real paths.

This suite used to point THEME at the real ~/Kova/kova-theme working tree. Two
cases ("bare push on main", "merge on main") have no explicit refspec, so the
guard has to read the CURRENT BRANCH to decide — which meant those two cases
silently tested Juan's checkout state rather than the guard's logic. Whenever he
was mid-branch (i.e. most of the time) the suite reported 2 FAILURES against a
guard that was behaving perfectly, and a real regression would have been
indistinguishable from that permanent noise.

So the fixture builds throwaway git repos: one named `kova-theme` parked on
`main`, one named `kova-docs` parked on `main`. Both are created fresh per run
and removed afterwards. The suite now asserts the guard's behaviour and nothing
about the machine it runs on.
"""
import json, os, shutil, subprocess, sys, tempfile

HOOK = "/Users/juanturcios/.claude/hooks/theme-push-guard.py"
GIT = "/opt/homebrew/bin/git" if os.path.exists("/opt/homebrew/bin/git") else "/usr/bin/git"


def make_repo(parent, name):
    """A throwaway git repo checked out on `main`, so branch-dependent cases are deterministic."""
    path = os.path.join(parent, name)
    os.makedirs(path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run([GIT, "init", "-q", "-b", "main", path], check=True, env=env)
    open(os.path.join(path, "f.txt"), "w").write("x")
    subprocess.run([GIT, "-C", path, "add", "-A"], check=True, env=env)
    subprocess.run([GIT, "-C", path, "commit", "-qm", "init"], check=True, env=env)
    return path


tmp = tempfile.mkdtemp(prefix="push-guard-test-")
THEME = make_repo(tmp, "kova-theme")
DOCS = make_repo(tmp, "kova-docs")

# (label, cwd, command, expect_ask)
CASES = [
 # --- MUST ASK: routes to production ---
 ("push origin main",            THEME, "git push origin main", True),
 ("push -u origin main",         THEME, "git push -u origin main", True),
 ("force push main",             THEME, "git push --force origin main", True),
 ("push HEAD:main",              THEME, "git push origin HEAD:main", True),
 ("push main:main",              THEME, "git push origin main:main", True),
 ("push +main",                  THEME, "git push origin +main", True),
 ("push refs/heads/main",        THEME, "git push origin refs/heads/main", True),
 ("push --all",                  THEME, "git push --all origin", True),
 ("push --mirror",               THEME, "git push --mirror", True),
 ("bare push on main",           THEME, "git push", True),
 ("merge on main",               THEME, "git merge feature/x", True),
 ("cd then push (from docs)",    DOCS,  "cd /Users/juanturcios/Kova/kova-theme && git push origin main", True),
 ("git -C theme push main",      DOCS,  "git -C kova-theme push origin main", True),
 ("compound; hidden push",       THEME, "git add -A ; git commit -m 'x' ; git push origin main", True),
 ("shopify theme push",          THEME, "shopify theme push", True),
 ("shopify theme push --live",   DOCS,  "cd kova-theme && shopify theme push --theme 123", True),
 # `gh pr merge` publishes exactly like a push to main and had NO coverage until
 # Aug 28 2026 — the segment loop skipped anything without a `git` token, so a
 # crafted-payload probe returned a silent ALLOW on a live deploy.
 ("gh pr merge --repo theme",    DOCS,  "gh pr merge 60 --repo juanrnsto/kova-theme --merge", True),
 ("gh pr merge from theme cwd",  THEME, "gh pr merge 60 --merge", True),
 ("gh pr merge --squash",        THEME, "gh pr merge 60 --squash --delete-branch", True),
 ("cd into theme then gh merge", DOCS,  f"cd {THEME} && gh pr merge 60 --merge", True),

 # --- MUST ALLOW SILENTLY: legitimate work ---
 ("push a branch",               THEME, "git push origin fix/hero-copy", False),
 ("push -u a branch",            THEME, "git push -u origin feat/waitlist", False),
 ("push branch w/ maintenance in name", THEME, "git push origin fix/maintenance-page", False),
 ("git status",                  THEME, "git status", False),
 ("git commit",                  THEME, "git commit -m 'v3 41: comment only'", False),
 ("shopify theme dev",           THEME, "shopify theme dev", False),
 ("docs repo bare push",         DOCS,  "git push", False),
 ("docs repo push main",         DOCS,  "git push origin main", False),
 ("unrelated cmd w/ 'push'",     THEME, "grep -rn 'push' assets/", False),
 ("npm run build",               THEME, "npm run build", False),
 # The inverse of "cd then push": the shell is parked in the theme from an
 # earlier call and the command cd's OUT to the docs repo before pushing. The
 # payload cwd alone said "theme", so this was a live false positive on Aug 28
 # 2026 — it blocked a legitimate kova-docs push.
 ("cd OUT of theme, push docs main", THEME, f"cd {DOCS} && git push origin main", False),
 ("cd OUT of theme, bare push",      THEME, f"cd {DOCS} && git push", False),
 ("gh pr merge in docs repo",        DOCS,  "gh pr merge 7 --merge", False),
 ("gh pr view from theme",           THEME, "gh pr view 60", False),
]

try:
    fails = 0
    for label, cwd, cmd, expect in CASES:
        payload = json.dumps({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": cmd}})
        r = subprocess.run(["/usr/bin/python3", HOOK], input=payload, capture_output=True, text=True)
        asked = "permissionDecision" in r.stdout and '"ask"' in r.stdout
        ok = (asked == expect)
        if not ok: fails += 1
        if r.stderr.strip():
            print("   STDERR:", r.stderr.strip()[:300]); fails += 1
        print(("  ok  " if ok else "  FAIL") + f"  [{'ASK ' if asked else 'pass'}] {label}")
    # --- permission_mode: ASK when a human can answer, DENY when none can ----
    # Added Aug 14 2026. An `ask` under bypassPermissions reaches nobody: the
    # run either waves it through or hangs, so the guard must deny instead.
    # Every other mode keeps `ask` — there is NO documented field separating a
    # scheduled run from an interactive one, and guessing one would risk
    # denying a push Juan meant to approve.
    MODE_CASES = [
        ("default -> ask",           "default",           "ask"),
        ("acceptEdits -> ask",       "acceptEdits",       "ask"),
        ("plan -> ask",              "plan",              "ask"),
        ("absent mode -> ask",       None,                "ask"),
        ("bypassPermissions -> DENY", "bypassPermissions", "deny"),
    ]
    print()
    for label, mode, expect in MODE_CASES:
        body = {"tool_name": "Bash", "cwd": THEME,
                "tool_input": {"command": "git push origin main"}}
        if mode is not None:
            body["permission_mode"] = mode
        r = subprocess.run(["/usr/bin/python3", HOOK], input=json.dumps(body),
                           capture_output=True, text=True)
        got = "deny" if '"deny"' in r.stdout else ("ask" if '"ask"' in r.stdout else "NONE")
        ok = (got == expect)
        if not ok:
            fails += 1
        print(("  ok  " if ok else "  FAIL") + f"  [{got:4}] {label}")

    # The guard must still FIRE on a real publish under bypass — deny, not allow.
    total = len(CASES) + len(MODE_CASES)
    print()
    print(f"{total-fails}/{total} passed" if not fails else f"*** {fails} FAILURES ***")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if fails else 0)
