# my_astro_utils

Lightweight Python utilities for astrophotography data quality checks.
Designed to run on-the-go from the command line — no PixInsight required.

## Tools

| Tool | Purpose |
|------|---------|
| [`focus_check`](focus_check/) | Compare FITS frames against a reference to evaluate focus quality (FWHM, HFR, eccentricity, SNR, star count, sky background) |

## Setup

```powershell
# Create the virtual environment (once)
python -m venv .venv

# Install dependencies (once)
.venv\Scripts\pip install -r requirements.txt
```

## Usage

Activate the virtual environment at the start of each terminal session, then `cd` into a tool's folder and run it:

```powershell
.venv\Scripts\Activate.ps1
cd focus_check
python analyze.py --help
```

Each tool lives in its own subdirectory with its own `README.md` and `example_config.yaml`.
