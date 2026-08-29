#!/usr/bin/env python3
"""PreToolUse hook (Bash): guard the LIVE Shopify theme against a silent publish.

`kova-theme` is GitHub-connected to its own repo's `main` branch — merging or
pushing `main` auto-deploys to the live storefront with no preview gate. The
middle-path policy (CLAUDE.md, Jul 2 2026) allows Claude to self-merge ONLY
changes that provably render nothing to a live visitor; everything
customer-facing goes out as a branch + PR for Juan to merge.

Until now that rule was prose only — nothing mechanically stopped a
`git push origin main` from the theme directory. This hook raises an `ask` on
any command that could publish, so the narrow self-merge lane still works but
can never happen silently.

Design notes (deliberate, do not "simplify" away):
  * Explicit-main patterns are matched on the command STRING FIRST, with no
    subprocess dependency — so the guard still fires if git is unavailable.
  * The ambiguous cases (bare `git push`, `git merge`) need the current branch.
    If that lookup fails we ASK rather than allow: we already know we are
    looking at a push inside the theme repo, so fail-SAFE, not fail-open.
  * Outside a kova-theme context the hook exits silently and allows.
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

THEME_DIRNAME = "kova-theme"
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
    named = THEME_DIRNAME in command

    for seg, seg_cwd in segment_cwds(cwd, command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if not tokens:
            continue
        if not (named or repo_name(seg_cwd) == THEME_DIRNAME):
            continue
        cwd = seg_cwd  # branch lookups below must ask the repo this segment runs in

        # --- Shopify CLI: the other route to production -------------------
        # CLAUDE.md: never `shopify theme push` to the published theme.
        if "shopify" in tokens and "theme" in tokens and "push" in tokens:
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
                "BLOCKED PENDING CONFIRMATION — `gh pr merge` on kova-theme. "
                "Merging a PR into `main` publishes to the LIVE storefront "
                "immediately, exactly like `git push origin main`. Claude may "
                "self-merge ONLY changes that provably render nothing to a "
                "visitor; anything customer-facing is Juan's merge to make."
                "\n\n  " + seg.strip()
            )

        if "git" not in tokens:
            continue

        # --- git merge into main ------------------------------------------
        if "merge" in tokens:
            branch = git_out(cwd, ["branch", "--show-current"])
            if branch is None:
                ask(
                    "BLOCKED PENDING CONFIRMATION — a `git merge` in kova-theme, "
                    "and the current branch could not be determined. If this "
                    "merges into `main` it publishes to the live store on the "
                    "next push.\n\n  " + seg.strip()
                )
            if branch == "main":
                ask(
                    "BLOCKED PENDING CONFIRMATION — merging into `main` in "
                    "kova-theme. `main` IS the published theme; this deploys "
                    "live once pushed. Claude may self-merge ONLY changes that "
                    "provably render nothing to a visitor (comments, intent "
                    "markers, unreachable templates, dev tooling). Anything "
                    "customer-facing belongs on a branch + PR.\n\n  " + seg.strip()
                )
            continue

        if "push" not in tokens:
            continue

        # --- git push -----------------------------------------------------
        # `--all` / `--mirror` sweep main along with everything else.
        if "--all" in tokens or "--mirror" in tokens:
            ask(
                "BLOCKED PENDING CONFIRMATION — `git push --all/--mirror` in "
                "kova-theme pushes every branch INCLUDING `main`, which "
                "auto-deploys to the live storefront.\n\n  " + seg.strip()
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
                        "kova-theme. The repo is GitHub-connected to the "
                        "published theme: this deploys to the LIVE storefront "
                        "immediately, with no preview gate. Push the branch and "
                        "open a PR unless this change provably renders nothing "
                        "to a visitor.\n\n  " + seg.strip()
                    )
            continue

        # Bare `git push` — dangerous only if the checked-out branch is main.
        branch = git_out(cwd, ["branch", "--show-current"])
        if branch is None:
            ask(
                "BLOCKED PENDING CONFIRMATION — a bare `git push` in kova-theme, "
                "and the current branch could not be determined. If this is "
                "`main` it deploys to the live storefront.\n\n  " + seg.strip()
            )
        if branch == "main":
            ask(
                "BLOCKED PENDING CONFIRMATION — bare `git push` while on `main` "
                "in kova-theme. This publishes to the LIVE storefront "
                "immediately. Branch + PR unless the change provably renders "
                "nothing to a visitor.\n\n  " + seg.strip()
            )

    allow()


if __name__ == "__main__":
    main()
