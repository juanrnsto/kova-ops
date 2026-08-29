#!/usr/bin/env python3
"""PreToolUse hook (Bash): guard git commands that destroy work with no undo.

Sibling of theme-push-guard.py, deliberately a SEPARATE file (one file, one job):
that hook guards PUBLISHING the live storefront and is scoped to kova-theme; this
one guards LOSING LOCAL WORK and applies in every repo.

The idea came from mattpocock/skills `git-guardrails-claude-code` (reviewed Aug 13
2026, NOT installed). That skill hard-blocks every `git push` in every repo with
exit code 2 — which would break the branch + PR workflow this machine depends on,
and duplicates the theme guard badly. But the commands it names beyond push are a
real gap: nothing here guarded `reset --hard`, `clean -f`, `checkout .`, or
`branch -D`, and at the moment this was written `kova-automation` had 17 lines of
uncommitted work that any of them would have erased.

Design (same discipline as theme-push-guard.py):
  * `ask`, never `deny`. These are legitimate commands; the failure mode is doing
    them without realising what is uncommitted, not doing them at all.
  * NOISE CONTROL — ask only when something is actually at risk. `git checkout .`
    against a clean tree destroys nothing, and a guard that cries wolf gets
    click-through-approved, which is worse than no guard. So the tree is inspected
    and the prompt NAMES what would be lost.
  * ...but the exceptions are load-bearing: `reset --hard <ref>` destroys COMMITS,
    which a clean tree does not protect. Anything touching history always asks.
  * Fail-SAFE: if the repo state cannot be read, ask.
  * Python 3.9 (/usr/bin/python3 under a minimal hook PATH) — no 3.10+ syntax.
"""
import json
import os
import re
import shlex
import subprocess
import sys

GIT = "/opt/homebrew/bin/git" if os.path.exists("/opt/homebrew/bin/git") else "/usr/bin/git"


# Set in main() from the hook payload's documented `permission_mode` field.
_MODE = "default"


def allow():
    sys.exit(0)


def _log_fire(decision, reason):
    """One diagnostic line per fire. MUST never change the decision.

    No documented hook field separates a scheduled run from an interactive
    one, so record what a real fire actually reports and tighten `decide()`
    from evidence instead of a guess. Wrapped so a logging failure can never
    turn a deny into an allow.
    """
    try:
        import datetime
        path = os.path.join(os.path.expanduser("~"), ".claude", "hooks", "guard-fires.log")
        with open(path, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "guard": "git-destructive-guard",
                "decision": decision,
                "permission_mode": _MODE,
                "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""),
                "remote": os.environ.get("CLAUDE_CODE_REMOTE", ""),
                "reason": reason.split("\n")[0][:140],
            }) + "\n")
    except Exception:
        pass


def ask(reason, seg):
    """Raise the guard: ASK when a human can answer, DENY when nothing can.

    Same reasoning as theme-push-guard. An `ask` in an unattended run pauses
    on a prompt nobody answers, so the run stops mid-task and a stall becomes
    indistinguishable from a clean pass. Under `bypassPermissions` the prompt
    machinery is precisely what is being skipped, so deny is the only verdict
    that actually holds — and it lets the run finish and report the block.

    Every other mode keeps `ask` deliberately: destructive git is exactly where
    Juan most wants the in-the-moment choice, and no documented field
    identifies a scheduled run, so inventing one could block work he intended.
    """
    decision = "deny" if _MODE == "bypassPermissions" else "ask"
    full = reason + "\n\n  " + seg.strip()
    if decision == "deny":
        full = ("AUTO-DENIED — permission_mode is `bypassPermissions`, so no "
                "confirmation can reach a human. Blocking rather than stalling. "
                "Re-run interactively if this was intended.\n\n" + full)
    _log_fire(decision, full)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": full,
        }
    }))
    sys.exit(0)


def git_out(cwd, args):
    try:
        r = subprocess.run([GIT, "-C", cwd] + args, capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def at_risk(cwd):
    """(modified_count, untracked_count, description) or None if unreadable."""
    out = git_out(cwd, ["status", "--porcelain"])
    if out is None:
        return None
    modified = untracked = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    bits = []
    if modified:
        bits.append("{} uncommitted change{}".format(modified, "" if modified == 1 else "s"))
    if untracked:
        bits.append("{} untracked file{}".format(untracked, "" if untracked == 1 else "s"))
    return (modified, untracked, " and ".join(bits) if bits else "nothing")


def segment_cwds(cwd, command):
    """Yield (segment, cwd-in-effect) pairs, following `cd` across a compound command.

    WHY (Aug 28 2026): the hook payload carries the working directory as it was
    BEFORE the command runs, and every risk question here — "is anything at risk
    in this repo?" — was being asked of THAT directory rather than the one the
    destructive command actually lands in.

    Probed, not reasoned about. With the shell parked in a CLEAN repo, the command
    `cd <dirty-repo> && git clean -fd` was ALLOWED SILENTLY: the guard read the
    clean repo, saw nothing at risk, and waved through a wipe of another repo's
    untracked work. The inverse (parked dirty, cd into clean) warned about work
    that was never in danger. One is noise; the other is the guard's whole purpose
    failing quietly, which is the reason this is a fix and not a polish.

    Kept byte-identical in shape to the twin in theme-push-guard.py so the two
    cannot drift apart.
    """
    here = cwd
    for seg in re.split(r"&&|\|\||;|\|", command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        yield seg, here
        if tokens and tokens[0] == "cd":
            targets = [t for t in tokens[1:] if not t.startswith("-")]
            here = (os.path.abspath(os.path.join(here, os.path.expanduser(targets[0])))
                    if targets else os.path.expanduser("~"))


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
    if "git" not in command:
        allow()
    # Cheap reject before any subprocess work.
    if not re.search(r"reset|clean|checkout|restore|branch|stash|push", command):
        allow()

    cwd = data.get("cwd") or os.getcwd()

    for seg, seg_cwd in segment_cwds(cwd, command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if "git" not in tokens:
            continue
        cwd = seg_cwd  # every at_risk() below must read the repo THIS segment runs in

        flags = [t for t in tokens if t.startswith("-")]
        args = [t for t in tokens if not t.startswith("-")]

        # --- history-destroying: ALWAYS ask, a clean tree is no protection ----
        if "reset" in tokens and ("--hard" in flags):
            # `reset --hard <ref>` throws away commits; bare `reset --hard` only
            # throws away the working tree.
            after = args[args.index("reset") + 1:] if "reset" in args else []
            if after:
                ask("BLOCKED PENDING CONFIRMATION — `git reset --hard` with a target "
                    "discards COMMITS, not just working-tree changes. This is not "
                    "recoverable from the working tree, and a clean tree does not "
                    "make it safe.", seg)
            risk = at_risk(cwd)
            if risk is None:
                ask("BLOCKED PENDING CONFIRMATION — `git reset --hard`, and the repo "
                    "state could not be read.", seg)
            if risk[0] or risk[1]:
                ask("BLOCKED PENDING CONFIRMATION — `git reset --hard` discards ALL "
                    "uncommitted work. Currently at risk: " + risk[2] + ".", seg)
            allow()

        if "branch" in tokens and ("-D" in flags or "--delete" in flags and "--force" in flags):
            ask("BLOCKED PENDING CONFIRMATION — `git branch -D` force-deletes a branch "
                "even if it is unmerged. Any commits only on that branch become "
                "unreachable.", seg)

        if "stash" in tokens and ("drop" in args or "clear" in args):
            ask("BLOCKED PENDING CONFIRMATION — dropping or clearing a stash "
                "permanently discards the stashed work.", seg)

        if "push" in tokens and ("--force" in flags or "-f" in flags):
            # theme-push-guard.py owns kova-theme; this covers every other repo,
            # where a force-push still rewrites published history.
            ask("BLOCKED PENDING CONFIRMATION — force-push rewrites remote history. "
                "Anyone holding a clone will need to re-clone or reset.", seg)

        # --- working-tree-destroying: ask only if something is actually at risk ---
        destructive_tree = False
        if "clean" in tokens and any(f.startswith("-") and "f" in f for f in flags):
            destructive_tree = "untracked"
        elif "checkout" in tokens and ("." in args or "--" in tokens):
            destructive_tree = "modified"
        elif "restore" in tokens and "." in args:
            destructive_tree = "modified"

        if destructive_tree:
            risk = at_risk(cwd)
            if risk is None:
                ask("BLOCKED PENDING CONFIRMATION — this discards local changes, and "
                    "the repo state could not be read to see what is at risk.", seg)
            modified, untracked, desc = risk
            relevant = untracked if destructive_tree == "untracked" else modified
            if relevant:
                ask("BLOCKED PENDING CONFIRMATION — this permanently discards local "
                    "changes that were never committed, so there is no reflog and no "
                    "undo. Currently at risk: " + desc + ".", seg)

    allow()


if __name__ == "__main__":
    main()
