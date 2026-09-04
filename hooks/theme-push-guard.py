#!/usr/bin/env python3
"""PreToolUse hook (Bash): guard live-deploying repos against a silent publish.

`kova-theme` is GitHub-connected to its own repo's `main` branch — merging or
pushing `main` auto-deploys to the live storefront with no preview gate. The
middle-path policy (CLAUDE.md, Jul 2 2026) allows Claude to self-merge ONLY
changes that provably render nothing to a live visitor; everything
customer-facing goes out as a branch + PR for Juan to merge.

Until now that rule was prose only — nothing mechanically stopped a
`git push origin main` from the theme directory. This hook raises an `ask` on
any command that could publish, so the narrow self-merge lane still works but
can never happen silently.

Two repos qualify (Sep 1 2026). `kova-theme` publishes the Shopify storefront;
`juanernesto-site` publishes juanernesto.com via Cloudflare Pages. Both are
Git-connected on `main`, so a merge or push there deploys with no preview gate.
Only `main` is guarded: `juanernesto-site`'s `preview` branch also builds a real
deployment, and shipping there is the intended workflow rather than a risk.

Design notes (deliberate, do not "simplify" away):
  * Explicit-main patterns are matched on the command STRING FIRST, with no
    subprocess dependency — so the guard still fires if git is unavailable.
  * The ambiguous cases (bare `git push`, `git merge`) need the current branch.
    If that lookup fails we ASK rather than allow: we already know we are
    looking at a push inside the theme repo, so fail-SAFE, not fail-open.
  * Outside a guarded repo the hook exits silently and allows.
  * Written for Python 3.9 (/usr/bin/python3 under a minimal hook PATH) —
    no tomllib, no match/case, no `X | Y` type unions. See the scheduler
    PATH trap: bare `python3` is 3.12 by hand and 3.9.6 under launchd/hooks.
"""
import json
import os
import re
import shlex
import subprocess
import sys

# Repos whose `main` is wired to a live deploy, keyed by an identifier the repo
# answers to: its directory basename OR its origin remote's slug. Both, because
# `~/juanernesto/site` is a directory called `site` and only the remote says WHICH
# site — `another project’s `site`` is also a directory called `site`, and matching on
# basename alone would guard a repo nobody asked to guard.
MSG_FLAGS = ("-m", "--message", "-F", "--file")

# <<EOF / <<'EOF' / <<"EOF" / <<-EOF, up to a line that is just the delimiter.
_HEREDOC = re.compile(r"<<-?\s*[\'\"]?(\w+)[\'\"]?\r?\n.*?^\s*\1\s*$",
                      re.S | re.M)
# the same opener with no closing delimiter anywhere: the body runs to the end.
_HEREDOC_OPEN = re.compile(r"<<-?\s*[\'\"]?\w+[\'\"]?\r?\n[\s\S]*\Z")


def strip_heredocs(command):
    """Remove heredoc BODIES before tokenising.

    🔴 shlex has no idea what a heredoc is, so `python3 - <<'PY' ... PY` hands it
    every word of the body as an ordinary argument. That blocked a legitimate
    push whose only sin was a doc-writing heredoc in the same Bash call that
    mentioned a guarded repo in prose — the exact false positive the message-flag
    fix was meant to end, arriving through a second door.

    A heredoc body is STDIN. It is never a path, a remote or a refspec, so
    nothing here can hide a real publish: the `git push` itself lives outside the
    body and is still tokenised normally.
    """
    prev = None
    while prev != command:                    # nested / multiple heredocs
        prev = command
        command = _HEREDOC.sub(" <<HEREDOC ", command)
    return _HEREDOC_OPEN.sub(" <<HEREDOC", command)


def named_repos(command):
    """GUARDED keys appearing as an ARGUMENT — never inside a commit message.

    Only the message-bearing flags have their payload dropped. `-C <path>` and
    every other token still count, because a repo named in a path IS the case
    this lookup exists for. Substring matching within a surviving token is kept
    on purpose: `~/work/kova-theme` has to match `kova-theme`.
    """
    command = strip_heredocs(command)
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    args, skip = [], False
    for t in tokens:
        if skip:                                  # this token IS the message
            skip = False
            continue
        if t in MSG_FLAGS:
            skip = True
            continue
        # the attached forms: --message=..., -F=..., -mfixed a thing
        if t.startswith(("--message=", "--file=")):
            continue
        if len(t) > 2 and t.startswith("-m") and not t.startswith("--"):
            continue
        args.append(t)
    return [k for k in GUARDED if any(k in a for a in args)]


GUARDED = {
    "kova-theme": {
        "surface": "the LIVE Kova storefront",
        "policy": (
            "Claude may self-merge ONLY changes that provably render nothing to a "
            "visitor (comments, intent markers, unreachable templates, dev tooling). "
            "Anything customer-facing belongs on a branch + PR."
        ),
    },
    # Added Sep 1 2026, same shape as kova-theme: Git-connected Cloudflare Pages,
    # so `main` publishes juanernesto.com with no gate. `preview` is deliberately
    # NOT guarded — every branch builds, and Juan's standing rule for this repo is
    # ship-to-preview and let him look, rather than stage-and-ask.
    "juanernesto-site": {
        "surface": "juanernesto.com (live, public, the job-search site)",
        "policy": (
            "`preview` is unguarded on purpose: push there freely and hand Juan "
            "preview.juanernesto-site.pages.dev to look at. Only `main` is gated. "
            "Once he approves, push BOTH branches — that is how origin/preview "
            "silently fell two commits behind production before."
        ),
    },
}
GIT = "/opt/homebrew/bin/git" if os.path.exists("/opt/homebrew/bin/git") else "/usr/bin/git"


# Set in main() from the hook payload's documented `permission_mode` field.
_MODE = "default"


def allow():
    """Say nothing; the tool call proceeds normally."""
    sys.exit(0)


def _log_fire(decision, reason):
    """Append one diagnostic line per fire. MUST never change the decision.

    We do not know what `permission_mode` a scheduled run reports — there is no
    documented interactive-vs-headless field — so record the real values the
    first time this guard fires anywhere, and tighten `decide()` from evidence
    rather than from a guess. Wrapped so a logging failure can never turn a
    deny into an allow.
    """
    try:
        import datetime
        path = os.path.join(os.path.expanduser("~"), ".claude", "hooks", "guard-fires.log")
        with open(path, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "guard": "theme-push-guard",
                "decision": decision,
                "permission_mode": _MODE,
                "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""),
                "remote": os.environ.get("CLAUDE_CODE_REMOTE", ""),
                "reason": reason.split("\n")[0][:140],
            }) + "\n")
    except Exception:
        pass


def ask(reason):
    """Raise the guard: ASK when a human can answer, DENY when nothing can.

    `ask` is the right verdict with Juan at the keyboard — the narrow
    self-merge lane depends on his being able to say "yes, this one is fine".
    But an `ask` in an unattended run asks a room with nobody in it: the run
    PAUSES on a prompt that never gets answered, so the routine silently stops
    mid-task. From outside, a stalled routine is indistinguishable from one
    that ran clean. `deny` is strictly better there — it blocks the command AND
    lets the run finish and report what it was stopped from doing.

    The one case we can identify from documented input is `bypassPermissions`,
    where the prompt machinery is exactly what is being skipped, so an `ask`
    is either waved through or hangs. There deny is unambiguous.

    Everything else keeps today's behaviour (`ask`) deliberately: there is NO
    documented field distinguishing a scheduled run from an interactive one,
    and inventing a signal here would risk denying a push Juan wanted to
    approve. `_log_fire` captures the real values so the unknown case can be
    closed with data. See the guard-fires.log note in the module docstring.
    """
    decision = "deny" if _MODE == "bypassPermissions" else "ask"
    if decision == "deny":
        reason = ("AUTO-DENIED — permission_mode is `bypassPermissions`, so no "
                  "confirmation prompt can reach a human. Blocking rather than "
                  "stalling. Re-run interactively if you meant to do this.\n\n" + reason)
    _log_fire(decision, reason)

    # 🔴 THE DENY CASE MUST EXIT 2, NOT 0 — measured Sep 2 2026, fixed Sep 3 2026.
    #
    # The JSON `permissionDecision: "deny"` below is ADVISORY: the harness shows
    # it to the model but does not enforce it under `bypassPermissions` — which
    # is the ONLY mode that produces a deny here. Measured consequence: in a
    # bypass session this guard printed AUTO-DENIED for `git merge --ff-only
    # preview` on main AND `git push origin main`, and both executed. The reflog
    # showed the fast-forward and origin/main moved, so juanernesto.com deployed
    # while the session was being told it had been blocked. The same guard covers
    # the Kova theme repo, where a push to `main` publishes the live storefront —
    # so "reports blocked, publishes anyway" was a live Kova safety hole.
    #
    # Exit code 2 is the path the harness enforces regardless of permission mode:
    # the tool call is blocked and stderr is fed back to the model. So the deny
    # case writes its reason to stderr and exits 2. The ask case is UNCHANGED —
    # it keeps the JSON contract, because `ask` genuinely works when a human is
    # at the keyboard and that is what the narrow self-merge lane depends on.
    #
    # ⚠️ Do NOT "simplify" this into one exit path. `ask` via exit 2 would turn
    # every approvable push into a hard block and remove Juan's yes.
    if decision == "deny":
        sys.stderr.write(reason + "\n")
        sys.exit(2)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def git_out(cwd, args):
    """Run git, returning stripped stdout or None on any failure."""
    try:
        r = subprocess.run([GIT, "-C", cwd] + args, capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def repo_name(path):
    """Basename of the git repo containing `path`, or "" if there isn't one."""
    top = git_out(path, ["rev-parse", "--show-toplevel"])
    return os.path.basename(top) if top else ""


def repo_ids(path):
    """Every identifier the repo at `path` answers to: dirname AND remote slug.

    Both are needed. The test fixtures are throwaway repos with no remote, so
    dirname has to work; `~/juanernesto/site` carries its identity only in its
    remote, so the slug has to work too.
    """
    ids = set()
    name = repo_name(path)
    if name:
        ids.add(name)
    remote = git_out(path, ["remote", "get-url", "origin"])
    if remote:
        slug = remote.rstrip("/").rsplit("/", 1)[-1]
        if slug.endswith(".git"):
            slug = slug[:-4]
        if slug:
            ids.add(slug)
    return ids


def segment_cwds(cwd, command):
    """Yield (segment, cwd-in-effect) pairs, following `cd` across a compound command.

    WHY (Aug 28 2026): the hook payload carries the working directory as it was
    BEFORE the command runs. So `cd ~/Kova && git push origin main`, issued while
    the shell happened to still be parked in kova-theme from an earlier call, was
    judged a theme push and blocked — a real false positive, hit in a live session
    trying to push the DOCS repo.

    It failed safe, which is the right direction, but a guard that cries wolf on
    legitimate work is a guard people learn to route around, and that costs more
    than it saves. Following `cd` per segment makes the check answer the question
    it was always asking: which repo does THIS segment actually run in.
    """
    here = cwd
    for seg in segments(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        yield seg, here
        if tokens and tokens[0] == "cd":
            targets = [t for t in tokens[1:] if not t.startswith("-")]
            here = (os.path.abspath(os.path.join(here, os.path.expanduser(targets[0])))
                    if targets else os.path.expanduser("~"))


def segments(command):
    """Split a compound shell command into individual command segments."""
    return re.split(r"&&|\|\||;|\|", command)


def positional_args(tokens, verb):
    """Args following `verb`, with flags and their attached values dropped."""
    try:
        rest = tokens[tokens.index(verb) + 1:]
    except ValueError:
        return []
    return [t for t in rest if not t.startswith("-")]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        allow()

    global _MODE
    _MODE = data.get("permission_mode") or "default"

    if data.get("tool_name") != "Bash":
        allow()

    command = (data.get("tool_input") or {}).get("command", "") or ""
    if not command:
        allow()

    cwd = data.get("cwd") or os.getcwd()

    # Cheap reject: nothing here can publish.
    if "push" not in command and "merge" not in command:
        allow()

    # Naming the repo anywhere in the command still counts, whatever the cwd —
    # that is what catches `git -C kova-theme push` issued from elsewhere.
    #
    # 🔴 BUT ONLY IN AN ARGUMENT, NEVER IN A COMMIT MESSAGE (fixed Sep 1 2026).
    # This was `[k for k in GUARDED if k in command]` — a raw substring test over
    # the whole command line, so the guard could not tell a PATH from PROSE. Any
    # commit whose message mentioned a guarded repo made an UNRELATED repo's push
    # ask; under bypassPermissions, ask means auto-DENY, so writing "juanernesto-site"
    # in a handoff message blocked a push that was never near production. Handoff
    # commits name repos constantly, so this fired often and looked like the guard
    # working.
    # ⚠️ THE NARROWING IS THE RISK — every case it stops matching is an unguarded
    # deploy — so it is deliberately the SMALLEST one that fixes the bug: drop the
    # payload of the message-bearing flags and nothing else. `-C` is untouched
    # because its argument is exactly the path this feature exists to catch, and
    # the suite asserts both directions (see § THE SUBSTRING FALSE POSITIVE).
    named = named_repos(command)

    for seg, seg_cwd in segment_cwds(cwd, command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if not tokens:
            continue
        ids = repo_ids(seg_cwd)
        key = next((k for k in GUARDED if k in named or k in ids), None)
        if key is None:
            continue
        site = GUARDED[key]
        cwd = seg_cwd  # branch lookups below must ask the repo this segment runs in

        # --- Shopify CLI: the other route to production -------------------
        # CLAUDE.md: never `shopify theme push` to the published theme.
        if (key == "kova-theme" and "shopify" in tokens
                and "theme" in tokens and "push" in tokens):
            ask(
                "BLOCKED PENDING CONFIRMATION — `shopify theme push` targets the "
                "LIVE published theme directly, bypassing git and the PR gate "
                "entirely. CLAUDE.md hard rule: never push to the published "
                "theme. Confirm only if you intend to overwrite the live "
                "storefront right now.\n\n  " + seg.strip()
            )

        # --- gh pr merge: the publish route with no `git` in it -----------
        # Merging a PR into `main` deploys exactly like `git push origin main`,
        # and until Aug 28 2026 this guard never saw it: the `git` filter below
        # skipped the segment entirely. Found by PROBING the guard with a
        # crafted payload rather than reading it — `gh pr merge 60 --repo
        # juanrnsto/kova-theme --merge` returned a silent ALLOW. That is the
        # dangerous direction of wrong, and it was the exact command that would
        # have run when asked to "push #60".
        if tokens[0] == "gh" and "merge" in tokens:
            ask(
                "BLOCKED PENDING CONFIRMATION — `gh pr merge` on " + key + ". "
                "Merging a PR into `main` publishes " + site["surface"] + " "
                "immediately, exactly like pushing that branch.\n\n"
                + site["policy"] + "\n\n  " + seg.strip()
            )

        if "git" not in tokens:
            continue

        # --- git merge into main ------------------------------------------
        if "merge" in tokens:
            branch = git_out(cwd, ["branch", "--show-current"])
            if branch is None:
                ask(
                    "BLOCKED PENDING CONFIRMATION — a `git merge` in " + key + ", "
                    "and the current branch could not be determined. If this "
                    "merges into `main` it publishes " + site["surface"] + " on "
                    "the next push.\n\n  " + seg.strip()
                )
            if branch == "main":
                ask(
                    "BLOCKED PENDING CONFIRMATION — merging into `main` in "
                    + key + ". `main` IS what is published: this deploys "
                    + site["surface"] + " once pushed.\n\n"
                    + site["policy"] + "\n\n  " + seg.strip()
                )
            continue

        if "push" not in tokens:
            continue

        # --- git push -----------------------------------------------------
        # `--all` / `--mirror` sweep main along with everything else.
        if "--all" in tokens or "--mirror" in tokens:
            ask(
                "BLOCKED PENDING CONFIRMATION — `git push --all/--mirror` in "
                + key + " pushes every branch INCLUDING `main`, which "
                "auto-deploys " + site["surface"] + ".\n\n  " + seg.strip()
            )

        refspecs = positional_args(tokens, "push")[1:]  # drop the remote

        if refspecs:
            # Explicit refspec: only ask when it actually names main.
            # Covers `main`, `main:main`, `HEAD:main`, `+main`, `refs/heads/main`.
            for spec in refspecs:
                dest = spec.split(":")[-1].lstrip("+")
                dest = dest.rsplit("/", 1)[-1]
                if dest == "main":
                    ask(
                        "BLOCKED PENDING CONFIRMATION — pushing to `main` in "
                        + key + ". The repo is Git-connected to its host, so "
                        "this deploys " + site["surface"] + " immediately, with "
                        "no preview gate.\n\n"
                        + site["policy"] + "\n\n  " + seg.strip()
                    )
            continue

        # Bare `git push` — dangerous only if the checked-out branch is main.
        branch = git_out(cwd, ["branch", "--show-current"])
        if branch is None:
            ask(
                "BLOCKED PENDING CONFIRMATION — a bare `git push` in " + key + ", "
                "and the current branch could not be determined. If this is "
                "`main` it deploys " + site["surface"] + ".\n\n  " + seg.strip()
            )
        if branch == "main":
            ask(
                "BLOCKED PENDING CONFIRMATION — bare `git push` while on `main` "
                "in " + key + ". This publishes " + site["surface"] + " "
                "immediately.\n\n" + site["policy"] + "\n\n  " + seg.strip()
            )

    allow()


if __name__ == "__main__":
    main()
