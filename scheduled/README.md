# Scheduled routines

Cron-triggered passes over live business data — nightly task rollover, weekly reconciliation checks,
periodic media sweeps.

## Constraints that are easy to miss

**The machine has to be awake.** These run on a Mac, not a server. A routine scheduled for 5am does
not run if the lid is shut. So every routine that writes guards its write: it must be safe to skip a
night, and safe to run twice.

**PATH is minimal under launchd.** Bare Homebrew binaries resolve interactively and fail silently on
a schedule. Use absolute paths, or resolve with a fallback:

```python
GIT = "/opt/homebrew/bin/git" if os.path.exists("/opt/homebrew/bin/git") else "/usr/bin/git"
```

The same trap applies to the interpreter itself — `python3` is 3.12 by hand and 3.9.6 under launchd.

**A dispatch record is not a completion record.** A `lastRunAt` timestamp proves the scheduler
fired, not that the work finished. Routines that matter write their own completion beacon, and the
health check reads the beacon rather than the schedule.

**Cadence drifts silently.** Nothing tells you a weekly routine stopped running — its absence
produces no output at all. A roll-call against a known roster catches that. A monitor sitting on the
success path never will, because it only ever sees the runs that happened.
