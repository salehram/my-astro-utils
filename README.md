# my_astro_utils

Lightweight Python utilities for astrophotography data quality checks.
Designed to run on-the-go from the command line — no PixInsight required.

## Tools

| Tool | Purpose |
|------|---------|
| [`astro_utils.py`](astro_utils.py) | Launcher — run any of the tools below from one place |
| [`focus_check`](focus_check/) | Compare FITS frames against a reference to evaluate focus quality (FWHM, HFR, eccentricity, SNR, star count, sky background) |
| [`star_check`](star_check/) | Analyze frames for star roundness, trailing direction, sky gradient, field tilt, and satellite/plane trail detection |

## Setup

```powershell
# Create the virtual environment (once)
python -m venv .venv

# Install dependencies (once)
.venv\Scripts\pip install -r requirements.txt
```

## Usage

Activate the virtual environment at the start of each terminal session:

```powershell
.venv\Scripts\Activate.ps1
```

Then either run a tool directly:

```powershell
cd focus_check
python analyze.py --help
```

…or drive any of them from the launcher:

```powershell
python astro_utils.py --list
python astro_utils.py -m analyze_stars -c my_session.yaml
```

Each tool lives in its own subdirectory with its own `README.md` and `example_config.yaml`.

## Launcher

`astro_utils.py` runs any workflow in this repo from one place. It auto-discovers
workflows — any subfolder containing `analyze.py` — and launches them as
subprocesses, so a workflow behaves exactly as if you had run it directly.

| Command | What it does |
|---------|--------------|
| `python astro_utils.py --list` | Show discovered workflows, aliases, and descriptions |
| `python astro_utils.py -m analyze_stars -c my_session.yaml` | Run one workflow |
| `python astro_utils.py -a -t "C:/data/*.fits"` | Run every workflow with the same arguments |
| `python astro_utils.py -c my_night.yaml` | Run whatever a master config specifies |
| `python astro_utils.py -a --dry-run -t "C:/data/*.fits"` | Print the commands without running them |

### Argument forwarding

**Everything after `-m NAME` (or after `-a`) is forwarded to the workflow untouched.**
That is what keeps `-c` unambiguous — it belongs to whichever level it follows:

```powershell
# -c is the workflow's session file, resolved in star_check/sessions/
python astro_utils.py -m analyze_stars -c my_session.yaml -t "*.fits"

# -c is the launcher's master config, resolved in sessions/
python astro_utils.py -c my_night.yaml
```

Launcher flags (`--list`, `--dry-run`, `--allow-watch`) must come *before* `-m` / `-a`.

Workflow names are case-insensitive and treat `-` and `_` as the same, so
`analyze_stars`, `Analyze-Stars`, and the `star_check` alias all work.

### Master config

A master config lives in `sessions/` next to `astro_utils.py` and says which
workflows to run and what to pass them. It is a separate thing from the
per-workflow session files in `focus_check/sessions/` and `star_check/sessions/`.

```yaml
workflows:
  - name: analyze_focus
    config: "focus_2026-06-20.yaml"     # shorthand for: args: ["-c", "focus_2026-06-20.yaml"]
  - name: analyze_stars
    args: ["-c", "stars_2026-06-20.yaml", "--csv", "stars_run.csv"]
```

Use `workflow: all` to trigger everything. See
[`example_master.yaml`](example_master.yaml) for all three
shapes. Trailing CLI args are appended after the config's args and win:
`python astro_utils.py -c my_night.yaml --csv override.csv`.

### Watch mode in a queue

Watch mode (`-w`) runs until Ctrl+C, so it would block anything queued behind it.
The launcher refuses `-w` when more than one workflow is queued, before starting
anything. Run the watcher on its own, or pass `--allow-watch`
(or set `allow_watch: true` in a master config) — Ctrl+C then advances to the
next workflow instead of aborting the run. A single-workflow run is never restricted.

> A `watch_dir:` set inside a workflow's *own* session YAML is invisible to the
> launcher and will not be caught by this guard.

### Adding a workflow

1. Create a folder with an `analyze.py` in it. That alone makes it discoverable.
2. Optionally add a `workflow.yaml` to give it a friendly name:

```yaml
name: analyze_guiding          # default: folder name
aliases: [guiding]             # default: none
description: Guiding error...  # default: first line of README.md
entry: analyze.py              # default: analyze.py
order: 30                      # default: 100; controls --all order
enabled: true                  # false = listed but skipped by --all
```

Canonical names follow a verb-first `<verb>_<noun>` convention (`analyze_stars`,
`analyze_focus`); keep the folder name as an alias. No launcher changes are needed.

Deferred work and past design decisions are tracked in [`BACKLOG.md`](BACKLOG.md).

