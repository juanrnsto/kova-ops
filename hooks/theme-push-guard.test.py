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

Sep 1 2026: three more fixtures for `juanernesto-site`, which the guard has to
identify by its origin REMOTE rather than its directory name — the directory is
called `site`, and so is `another project’s `site``. Hence `MASA`, a repo identical
in every way except its remote, which must be allowed. That negative case is the
point of the remote-slug lookup; without it the guard would block an unrelated
client repo for sharing a common directory name.
"""
import json, os, shutil, subprocess, sys, tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme-push-guard.py")
GIT = "/opt/homebrew/bin/git" if os.path.exists("/opt/homebrew/bin/git") else "/usr/bin/git"


def make_repo(parent, name, remote=None, branch="main"):
    """A throwaway git repo on a known branch, so branch-dependent cases are deterministic.

    `remote` matters for the juanernesto-site fixtures: that repo's directory is
    called `site`, so its identity lives only in the origin URL.
    """
    path = os.path.join(parent, name)
    os.makedirs(path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run([GIT, "init", "-q", "-b", branch, path], check=True, env=env)
    open(os.path.join(path, "f.txt"), "w").write("x")
    subprocess.run([GIT, "-C", path, "add", "-A"], check=True, env=env)
    subprocess.run([GIT, "-C", path, "commit", "-qm", "init"], check=True, env=env)
    if remote:
        subprocess.run([GIT, "-C", path, "remote", "add", "origin", remote],
                       check=True, env=env)
    return path


tmp = tempfile.mkdtemp(prefix="push-guard-test-")
THEME = make_repo(tmp, "kova-theme")
DOCS = make_repo(tmp, "kova-docs")

# Three directories all called `site`, distinguished only by their remotes.
JE = "https://github.com/juanrnsto/juanernesto-site.git"
SITE_MAIN = make_repo(os.path.join(tmp, "je-main"), "site", remote=JE)
SITE_PREV = make_repo(os.path.join(tmp, "je-prev"), "site", remote=JE, branch="preview")
MASA = make_repo(os.path.join(tmp, "masa"), "site",
                 remote="https://github.com/juanrnsto/masa-site.git")

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
 ("cd then push (from docs)",    DOCS,  "cd ~/work/kova-theme && git push origin main", True),
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

 # --- juanernesto-site (Sep 1 2026) -------------------------------------
 # Same shape as kova-theme: Git-connected Pages, `main` deploys live. Every
 # case here is matched via the origin remote, since the directory is `site`.
 ("je: push origin main",        SITE_MAIN, "git push origin main", True),
 ("je: bare push on main",       SITE_MAIN, "git push", True),
 ("je: merge into main",         SITE_MAIN, "git merge preview", True),
 ("je: push --all",              SITE_MAIN, "git push --all origin", True),
 ("je: push HEAD:main",          SITE_MAIN, "git push origin HEAD:main", True),
 ("je: gh pr merge by slug",     DOCS,      "gh pr merge 3 --repo juanrnsto/juanernesto-site --merge", True),

 # `preview` is DELIBERATELY unguarded: every branch builds a real deployment,
 # and shipping there for Juan to look at is the intended workflow. A guard on
 # preview would fight his standing "don't stage and ask" rule.
 ("je: push origin preview",     SITE_MAIN, "git push origin preview", False),
 ("je: bare push on preview",    SITE_PREV, "git push", False),
 ("je: merge while on preview",  SITE_PREV, "git merge fix/x", False),

 # A DIFFERENT repo whose directory is also called `site`. Guarding by basename
 # would block this; guarding by remote slug must not.
 ("masa site: push main",        MASA,      "git push origin main", False),
 ("masa site: bare push",        MASA,      "git push", False),

 # --- THE SUBSTRING FALSE POSITIVE (Sep 1 2026) -----------------------------
 # `named` matched a GUARDED key ANYWHERE in the raw command string, including
 # inside a commit MESSAGE. So writing the guarded repo's name in prose — which
 # a handoff commit does constantly — made an UNRELATED repo's push ask, and in
 # a bypassPermissions session that is an auto-DENY on work that was never near
 # production. The message is documentation; only a PATH argument names a repo.
 ("msg names guarded repo",      MASA,
  "git commit -m 'docs: juanernesto-site orphan check was broken' && git push origin main", False),
 ("msg names kova-theme",        DOCS,
  "git commit -m 'note: kova-theme ships from main' && git push origin main", False),
 # ⚠️ AND THE CAPABILITY THE SUBSTRING MATCH EXISTED FOR MUST SURVIVE. These two
 # name the repo in a real ARGUMENT, from a cwd that is not it, and must ask.
 ("-C path still asks",          DOCS,  "git -C juanernesto-site push origin main", True),
 ("cd path still asks",          MASA,
  "git push origin main", False),

 # --- HEREDOC BODIES ARE DATA TOO (Sep 1 2026) -------------------------------
 # The message-flag fix above was INCOMPLETE and this is how it was found: the
 # guard blocked a legitimate workbench push because the same Bash call also
 # carried a `python3 - <<'PY' ... PY` heredoc whose prose mentioned the guarded
 # repo. shlex does not know what a heredoc is, so every word of that body
 # survived tokenising as an ordinary argument. A heredoc body is stdin, never a
 # path, so it is stripped before matching.
 ("heredoc quoted body",         MASA,
  "python3 - <<'PY'\nprint('juanernesto-site is stale')\nPY\ngit push origin main", False),
 ("heredoc unquoted body",       DOCS,
  "cat <<EOF\nkova-theme ships from main\nEOF\ngit push origin main", False),
 ("heredoc dash form",           MASA,
  "git commit -F - <<-MSG\n\tdocs: juanernesto-site notes\n\tMSG\ngit push origin main", False),
 ("unterminated heredoc",        MASA,
  "git push origin main && cat <<EOF\njuanernesto-site", False),
 # ⚠️ and the real thing must STILL ask, heredoc or not
 ("heredoc + real -C path",      DOCS,
  "cat <<'EOF'\nnotes\nEOF\ngit -C juanernesto-site push origin main", True),
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
        # A deny arrives as EXIT 2 + stderr (the enforceable path); an ask stays
        # on the advisory JSON contract with exit 0. See the Sep 3 2026 comment
        # in the hook's ask() for why the deny cannot ride on stdout.
        if r.returncode == 2:
            got = "deny"
            ok = (expect == "deny") and "AUTO-DENIED" in r.stderr and not r.stdout.strip()
        else:
            got = "ask" if '"ask"' in r.stdout else "NONE"
            ok = (got == expect) and r.returncode == 0
        if not ok:
            fails += 1
        print(("  ok  " if ok else "  FAIL") + f"  [{got:4}] {label} (rc={r.returncode})")

    # --- the deny must be ENFORCEABLE, not merely reported -------------------
    # Added Sep 3 2026, regression test for the measured Sep 2 defect: the guard
    # printed AUTO-DENIED, exited 0, and the harness ran `git merge --ff-only`
    # and `git push origin main` anyway — juanernesto.com deployed while the
    # session was told it was blocked. This guard also covers the Kova theme
    # repo, where a push to main publishes the live storefront. Asserting the
    # decision STRING never caught it; the string was always right. Only the
    # exit code blocks a tool call, so the exit code is what gets asserted.
    print()
    ENFORCE = [
        ("theme main push: deny is exit 2 + stderr, no stdout", "bypassPermissions", 2),
        ("theme main push: ask stays exit 0 + stdout JSON",     "default",           0),
    ]
    for label, mode, want_rc in ENFORCE:
        r = subprocess.run(["/usr/bin/python3", HOOK],
                           input=json.dumps({"tool_name": "Bash", "cwd": THEME,
                                             "permission_mode": mode,
                                             "tool_input": {"command": "git push origin main"}}),
                           capture_output=True, text=True)
        if want_rc == 2:
            ok = (r.returncode == 2 and "AUTO-DENIED" in r.stderr
                  and not r.stdout.strip())
        else:
            ok = (r.returncode == 0 and '"ask"' in r.stdout
                  and not r.stderr.strip())
        if not ok:
            fails += 1
        print(("  ok  " if ok else "  FAIL")
              + f"  {label} (rc={r.returncode}, out={len(r.stdout)}B, err={len(r.stderr)}B)")

    # The guard must still FIRE on a real publish under bypass — deny, not allow.
    total = len(CASES) + len(MODE_CASES) + len(ENFORCE)
    print()
    print(f"{total-fails}/{total} passed" if not fails else f"*** {fails} FAILURES ***")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if fails else 0)
