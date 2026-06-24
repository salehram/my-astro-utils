# star_check

Analyze FITS frames for **star roundness, trailing direction, sky gradient,
field tilt, and linear artifact detection** — everything that matters for
sub rejection based on star shape and sky conditions, complementing
`focus_check` (which covers FWHM / HFR sharpness).

---

## Metrics

| Metric | Description |
|---|---|
| **Eccentricity** | 0 = perfectly round, → 1 = elongated/trailing. **Primary verdict driver.** |
| **Elongation ratio** | b/a axis ratio: 1 = round, → 0 = elongated. Visual complement to eccentricity. |
| **Trail °** | Dominant major-axis direction \[0°, 180°\]. 0° = horizontal (RA if camera aligned), 90° = vertical (Dec). Only shown when ecc ≥ 0.10. |
| **Angle consistency** | How uniformly stars trail in the same direction (circular coherence). < 0.30 = random (atmospheric turbulence); > 0.70 = consistent → likely a single-axis tracking or mount fault. |
| **Field tilt** | edge\_ecc − center\_ecc. Positive = edge stars more elongated than center, indicating possible sensor tilt or field curvature. |
| **Sky gradient** | (bg\_max − bg\_min) / bg\_median. Near 0 = flat sky; > 0.30 = strong gradient, likely cloud cover or bright moon encroaching. |
| **Star count** | Number of detected stars. A drop relative to the reference suggests cloud cover or transparency change. |
| **SNR** | Median (peak / sky\_rms). Transparency proxy — drops with cloud cover or high sky background. |
| **Trails** | Count of detected linear artifacts: satellite trails, aircraft, meteors. Any detection triggers an immediate REJECT. |

---

## Verdict thresholds

| Verdict | Condition |
|---|---|
| **KEEP** | ecc ≤ 0.25 · gradient ≤ 0.10 · no trails · stars ≥ −10% of ref |
| **MARGINAL** | ecc ≤ 0.45 · gradient ≤ 0.30 · no trails · stars ≥ −25% of ref |
| **REJECT** | ecc > 0.45 · gradient > 0.30 · any trail detected · stars < −25% of ref |

Star count comparisons are only made when a reference frame is supplied.
All other thresholds are absolute.

---

## Usage

```bash
# Batch analysis — no reference needed
python analyze.py -t "C:/data/session/*.fits" --csv results.csv

# With an optional reference for relative star-count comparison
python analyze.py -r best.fits -t "C:/data/session/*.fits"

# Config file (filename only — auto-located in star_check/sessions/)
python analyze.py -c my_session.yaml

# Config with CSV output override
python analyze.py -c my_session.yaml --csv custom_output.csv

# Real-time watch mode (Ctrl+C prints full summary table)
python analyze.py --watch "C:/data/incoming/"
python analyze.py -r REF.fits --watch "C:/data/incoming/"
python analyze.py -c my_session.yaml --watch "C:/data/incoming/"
```

---

## Config file

Place YAML session files in `star_check/sessions/`.
See `example_config.yaml` for the full template with all options.

```yaml
# reference is optional
reference: "C:/data/ref.fits"

targets:
  - "C:/data/session/*.fits"

output:
  csv: "my_session_stars.csv"   # bare name → saved in star_check/results/
```

---

## Diagnosing trailing from the output

| Trail ° | Angle cons. | Likely cause |
|---|---|---|
| ~0° or ~180° | High (> 0.70) | RA-axis tracking error |
| ~90° | High (> 0.70) | Dec-axis backlash or wind in Dec direction |
| Any angle | High (> 0.70) | Consistent mechanical problem (cable snag, motor slip) |
| Variable | Low (< 0.30) | Atmospheric turbulence, guiding bouncing randomly |
| Variable | Medium | Directional wind shake |

---

## Combining with focus_check

Run both scripts for a complete quality pipeline:

```bash
python focus_check/analyze.py -c focus_check/sessions/session.yaml  # FWHM, HFR, sharpness
python star_check/analyze.py  -c star_check/sessions/session.yaml   # roundness, trailing, sky
```

Both CSV outputs share a `filename` column and can be joined in any
spreadsheet application or with pandas for a complete per-frame quality table.

---

## Files

```
star_check/
  analyze.py          — CLI entry point
  metrics.py          — per-frame FITS metric computation
  report.py           — console table + CSV export
  example_config.yaml — session config template
  README.md           — this file
  sessions/           — place your YAML session configs here
  results/            — CSV outputs land here (bare filenames)
```
