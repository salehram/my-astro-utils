# Backlog

Deferred work and the reasoning behind decisions already made.

## Open

### Parallel execution (`--jobs N`)

**Problem** — `--all` runs workflows sequentially, so total wall time is the sum
of every run. This grows as more workflows are added.

**Approach** — parallelize only the safe subset: jobs with no `-w`/`--watch`
that will not hit a workflow's interactive session picker (i.e. `-c` or `-r` is
present). Everything else stays serial. Each child's output would need buffering
and flushing in one block per workflow so `rich` output does not interleave —
which means capturing stdout for parallel children, forfeiting their colors
unless `FORCE_COLOR` is set in the child environment.

**Why deferred** — it is a drop-in change: replace the single `Popen` + `wait()`
in `workflows.run()` with a bounded `Popen` pool. Nothing in the current design
forecloses it.

**Effort** — medium; touches `workflows.run()` and the orchestrator's result
collection in `astro_utils.py`.

**Trigger** — revisit only if `--all` wall time becomes a real complaint.

## Decided

### Watch mode is refused in a multi-workflow queue

`-w`/`--watch` runs until Ctrl+C, so one watcher starves every job behind it.
The launcher refuses when the resolved queue holds more than one job and any job
requests watch mode, validating before the first subprocess starts. Override
with `--allow-watch` or `allow_watch: true`; Ctrl+C then advances to the next
workflow instead of aborting the run. A single-job run is never restricted.

Accepted gap: a `watch_dir:` set inside a workflow's own session YAML is
invisible to the launcher. Detecting it would mean parsing each workflow's
`sessions/*.yaml`, duplicating their `_load_config()` and coupling the launcher
to a schema it must not own.

### Verb-first workflow naming

Canonical names are `<verb>_<noun>` — `analyze_stars`, `analyze_focus` — with
folder names kept as aliases in each `workflow.yaml`. Matches how the launcher
reads at the call site (`-m analyze_stars`) and scales to future tools.

### Subprocess dispatch, not imports

Every workflow folder ships its own `metrics.py` and `report.py` and imports
them by bare name, so two workflows cannot share one interpreter without
colliding in `sys.modules`. Combined with `main()` taking no argv parameter and
calling `sys.exit()` on errors, in-process dispatch would need argv
monkeypatching, `SystemExit` trapping, and `sys.modules`/`sys.path` juggling.
Subprocess dispatch is less code and makes the launcher's promise literal: a
workflow behaves exactly as if invoked directly.
