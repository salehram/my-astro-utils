# focus_check

Compare FITS subs against a known-good reference frame to quickly decide
whether tonight's data is worth keeping.

## Directory layout

```
focus_check/
├── analyze.py
├── metrics.py
├── report.py
├── example_config.yaml   ← template; copy into sessions/ for each night
├── sessions/             ← your per-night YAML configs
│   └── cygnus_loop_focus_check_20-06-2026.yaml
└── results/              ← CSV outputs land here by default
```

## CSV path resolution

| What you write in the YAML / `--csv` flag | Where the file is saved |
|-------------------------------------------|-------------------------|
| `"results.csv"` (bare filename) | `focus_check/results/results.csv` |
| `"C:/absolute/path/results.csv"` | exactly that path |
| `"../../my_reports/results.csv"` | relative to wherever you run the script |

| Metric | What it tells you |
|--------|-------------------|
| **FWHM (px / ")** | Median star size in pixels. If `FOCALLEN` and `XPIXSZ` headers are present, arcseconds are shown too — more meaningful at a glance. Larger = softer focus. Primary verdict driver. |
| **ΔFWHM** | How much worse/better than your reference. |
| **HFR (px)** | Half-Flux Radius — PixInsight's HFR equivalent. |
| **Eccentricity** | 0 = round stars (pure defocus). →1 = elongated (tracking/wind). |
| **Stars** | Detected star count. Drops sharply if the frame is heavily defocused or cloudy. |
| **Sky (ADU)** | Median sky background level. Useful for tracking light pollution changes across a session. |
| **SNR** | Median peak-signal / sky-noise. Lower = noisier frame. |

## Verdict thresholds

| Verdict | Condition |
|---------|-----------|
| 🟢 **KEEP** | ΔFWHM ≤ +10% |
| 🟡 **MARGINAL** | ΔFWHM ≤ +25% |
| 🔴 **REJECT** | ΔFWHM > +25% |

## Setup

Run once from the repo root (`my_astro_utils\`):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then activate at the start of each terminal session before running any script:

```powershell
# From the repo root:
.venv\Scripts\Activate.ps1

# Then cd into the tool:
cd focus_check
```

You'll see `(astro_utils)` in your prompt when the venv is active.

> **PowerShell tip:** if you get an "execution policy" error on `Activate.ps1`, run this once:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

## Usage

### CLI — quick one-off

```powershell
# Single target
python analyze.py -r "REF.fits" -t "target.fits"

# Multiple targets
python analyze.py -r "REF.fits" -t "frame1.fits" "frame2.fits" "frame3.fits"

# Glob pattern (always quote on Windows)
python analyze.py -r "REF.fits" -t "C:/data/session/*.fits"

# With CSV export
python analyze.py -r "REF.fits" -t "C:/data/session/*.fits" --csv results.csv
```

### Config file — save a session for re-running

Place your YAML files in `sessions/` and pass just the filename — no folder prefix needed:

```powershell
python analyze.py -c my_session.yaml

# Override CSV output path without editing the file
python analyze.py -c my_session.yaml --csv custom.csv

# Example with an existing session
python analyze.py -c cygnus_loop_focus_check_20-06-2026.yaml
```

> Configs outside `sessions/` are rejected. This keeps all your session configs in one predictable place.

### Config file format

```yaml
reference: "C:/path/to/good_reference.fits"

targets:
  - "C:/path/to/target1.fits"
  - "C:/path/to/target2.fits"
  - "C:/path/to/session/*.fits"   # glob works too

output:
  csv: "C:/path/to/results.csv"   # optional

# Optional: set a watch directory (see Watch mode below)
# watch_dir: "C:/AstroData/tonight/Panel8/"
```

### Watch mode — real-time session monitoring

Start this **before** imaging begins. The script polls the directory and auto-verdicts each new sub as it lands.

```powershell
# CLI
python analyze.py -r "REF.fits" -w "C:/AstroData/tonight/Panel8/"

# Via config (set watch_dir in the YAML, targets list is optional)
python analyze.py -c my_session.yaml
```

Each new frame prints a compact one-liner:
```
02:14:33  Panel 8_2026-06-21_02-14-30_OIII_300.00s_0001.fits   FWHM 2.71px / 4.3" (+0.7%)  HFR 1.98px  stars 150  SNR 105.2  KEEP
```

Press **Ctrl+C** at any point to stop watching and print the full comparison table. If a CSV path is set, it is updated after every new frame.

## Example output

With `FOCALLEN`/`XPIXSZ` headers present, FWHM shows both pixels and arcseconds:

```
                       focus_check — frame quality analysis
╭──────────────────────────────────────┬───────────────┬────────┬─────────┬───────┬───────┬───────────┬───────┬───────────╮
│ File                                 │ FWHM (px / ") │  ΔFWHM │ HFR(px) │  Ecc. │ Stars │ Sky (ADU) │   SNR │  Verdict  │
├──────────────────────────────────────┼───────────────┼────────┼─────────┼───────┼───────┼───────────┼───────┼───────────┤
│ REF  Panel8_good_ref.fits            │   2.73 / 4.3" │      — │    2.00 │ 0.366 │   150 │    3977.3 │ 102.4 │ REFERENCE │
│ Panel8_tonight_0001.fits             │   2.75 / 4.3" │  +0.7% │    2.01 │ 0.355 │   149 │    3990.1 │ 101.8 │   KEEP    │
│ Panel8_defocused_0002.fits           │  6.41 / 10.1" │ +135.% │    3.61 │ 0.268 │   150 │    4114.7 │  17.2 │  REJECT   │
╰──────────────────────────────────────┴───────────────┴────────┴─────────┴───────┴───────┴───────────┴───────┴───────────╯

Verdict thresholds — KEEP: ΔFWHM ≤ +10%   MARGINAL: ≤ +25%   REJECT: > +25%
```

If `FOCALLEN`/`XPIXSZ` are absent from the headers, the column falls back to pixels only.

## Notes

- Files don't need to be copied here — supply full paths wherever they live on disk.
- The reference and targets can be from different nights or panels, as long as they cover the same target and filter.
- A single FITS file with only the reference (no targets) is valid — it prints the baseline metrics.
