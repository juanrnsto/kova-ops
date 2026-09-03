import json, subprocess, sys, os, tempfile
HOOK=os.path.join(os.path.dirname(os.path.abspath(__file__)), "git-destructive-guard.py")

# Build two throwaway repos: one DIRTY, one CLEAN — so the noise-control logic is
# tested against real repo state, not mocked.
base=tempfile.mkdtemp()
def mkrepo(name, dirty):
    p=os.path.join(base,name); os.makedirs(p)
    q=lambda *a: subprocess.run(["git","-C",p]+list(a),capture_output=True)
    q("init","-q"); q("config","user.email","t@t"); q("config","user.name","t")
    open(os.path.join(p,"a.txt"),"w").write("one\n")
    q("add","-A"); q("commit","-q","-m","init")
    if dirty:
        open(os.path.join(p,"a.txt"),"w").write("two\n")     # modified
        open(os.path.join(p,"new.txt"),"w").write("x\n")      # untracked
    return p
DIRTY=mkrepo("dirty",True); CLEAN=mkrepo("clean",False)

CASES=[
 # --- history destruction: ALWAYS ask, even on a clean repo ---
 ("reset --hard <ref> on CLEAN repo (destroys commits)", CLEAN,"git reset --hard HEAD~1",True),
 ("reset --hard <ref> on dirty repo",                    DIRTY,"git reset --hard origin/main",True),
 ("branch -D",                                           CLEAN,"git branch -D feature/x",True),
 ("stash drop",                                          CLEAN,"git stash drop",True),
 ("stash clear",                                         CLEAN,"git stash clear",True),
 ("force push",                                          CLEAN,"git push --force origin main",True),
 ("force push -f short flag",                            CLEAN,"git push -f",True),

 # --- tree destruction: ask ONLY when something is at risk ---
 ("bare reset --hard, DIRTY repo",                       DIRTY,"git reset --hard",True),
 ("bare reset --hard, CLEAN repo (nothing to lose)",     CLEAN,"git reset --hard",False),
 ("checkout . with modified files",                      DIRTY,"git checkout .",True),
 ("checkout . on clean repo",                            CLEAN,"git checkout .",False),
 ("restore . with modified files",                       DIRTY,"git restore .",True),
 ("restore . on clean repo",                             CLEAN,"git restore .",False),
 ("clean -fd with untracked",                            DIRTY,"git clean -fd",True),
 ("clean -fd, nothing untracked",                        CLEAN,"git clean -fd",False),

 # --- MUST pass silently: ordinary work ---
 ("git status",                                          DIRTY,"git status",False),
 ("git checkout a branch",                               DIRTY,"git checkout -b feature/y",False),
 ("normal push",                                         DIRTY,"git push origin main",False),
 ("git add + commit",                                    DIRTY,"git add -A && git commit -m 'x'",False),
 ("git stash (saving, not dropping)",                    DIRTY,"git stash",False),
 ("git branch -d (safe delete)",                         DIRTY,"git branch -d merged-branch",False),
 ("git clean -n (dry run, no -f)",                       DIRTY,"git clean -n",False),
 ("grep for the word reset",                             DIRTY,"grep -rn 'reset' .",False),
 ("npm run build",                                       DIRTY,"npm run build",False),
 ("compound hiding a reset --hard",                      DIRTY,"echo hi && git reset --hard",True),
 # --- cd blind spot, added Aug 28 2026 -------------------------------------
 # The payload cwd is the directory BEFORE the command runs, so every at_risk()
 # question used to be asked of the wrong repo whenever a segment cd'd away.
 # Probed, not reasoned about: parked in CLEAN, `cd <dirty> && git clean -fd` was
 # ALLOWED SILENTLY — a real wipe of another repo's untracked work, waved through.
 ("cd into DIRTY repo then wipe",   CLEAN, f"cd {DIRTY} && git clean -fd", True),
 ("cd into DIRTY then reset --hard",CLEAN, f"cd {DIRTY} && git reset --hard", True),
 ("cd into CLEAN repo then wipe",   DIRTY, f"cd {CLEAN} && git clean -fd", False),
 ("cd into CLEAN then reset --hard",DIRTY, f"cd {CLEAN} && git reset --hard", False),
]
fails=0
for label,cwd,cmd,expect in CASES:
    payload=json.dumps({"tool_name":"Bash","cwd":cwd,"tool_input":{"command":cmd}})
    r=subprocess.run(["/usr/bin/python3",HOOK],input=payload,capture_output=True,text=True)
    asked='"ask"' in r.stdout
    ok=(asked==expect) and r.returncode==0 and not r.stderr.strip()
    if not ok:
        fails+=1; print(f"  FAIL [{'ASK' if asked else 'pass'}] {label}")
        if r.stderr.strip(): print("       stderr:",r.stderr.strip()[:200])
    else:
        print(f"  ok   [{'ASK ' if asked else 'pass'}] {label}")
# --- permission_mode: ASK when a human can answer, DENY when none can -------
# Added Aug 14 2026. An `ask` under bypassPermissions reaches nobody — the run
# waves it through or hangs — so the guard must deny and let the run finish.
# Every other mode keeps `ask`: destructive git is exactly where the in-the-
# moment choice matters, and no documented field identifies a scheduled run.
MODE_CASES = [
    ("default -> ask",            "default",           "ask"),
    ("acceptEdits -> ask",        "acceptEdits",       "ask"),
    ("plan -> ask",               "plan",              "ask"),
    ("absent mode -> ask",        None,                "ask"),
    ("bypassPermissions -> DENY", "bypassPermissions", "deny"),
]
print()
for label, mode, expect in MODE_CASES:
    body = {"tool_name": "Bash", "cwd": CLEAN,
            "tool_input": {"command": "git reset --hard HEAD~3"}}
    if mode is not None:
        body["permission_mode"] = mode
    r = subprocess.run(["/usr/bin/python3", HOOK], input=json.dumps(body),
                       capture_output=True, text=True)
    got = "deny" if '"deny"' in r.stdout else ('"ask"' in r.stdout and "ask" or "NONE")
    ok = (got == expect) and r.returncode == 0 and not r.stderr.strip()
    if not ok:
        fails += 1
        print(f"  FAIL [{got}] {label}")
        if r.stderr.strip(): print("       stderr:", r.stderr.strip()[:200])
    else:
        print(f"  ok   [{got:4}] {label}")

total = len(CASES) + len(MODE_CASES)
print()
print(f"{total-fails}/{total} passed" if not fails else f"*** {fails} FAILURES ***")
sys.exit(1 if fails else 0)
