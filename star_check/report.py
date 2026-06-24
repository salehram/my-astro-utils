"""
report.py — console table and CSV export for star_check results

Verdict thresholds (eccentricity is absolute; star count is relative to reference
when one is provided):

  Eccentricity (primary)
    green  / KEEP     :  ecc ≤ 0.25  (round stars)
    yellow / MARGINAL :  ecc ≤ 0.45  (mildly elongated)
    red    / REJECT   :  ecc >  0.45 (clearly trailing)

  Sky gradient (secondary)
    green  :  gradient ≤ 0.10  (uniform sky)
    yellow :  gradient ≤ 0.30  (mild gradient)
    red    :  gradient >  0.30 (cloud cover likely)

  Star count (secondary — only when a reference frame is supplied)
    green  :  ≥ −10% of reference
    yellow :  ≥ −25% of reference
    red    :   < −25% of reference

  Satellite / plane trails → any detected trail = immediate REJECT
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

# ── Eccentricity thresholds (absolute — ecc is already 0–1) ───────────────
_ECC_KEEP   = 0.25
_ECC_REJECT = 0.45

# ── Relative thresholds (shared with focus_check) ────────────────────────
_KEEP_THRESH     = 0.10   # +10%  → green
_MARGINAL_THRESH = 0.25   # +25%  → yellow / red above

# ── Sky gradient thresholds ──────────────────────────────────────────────────
_GRAD_KEEP   = 0.10
_GRAD_REJECT = 0.30

# ── Star-count drop thresholds (relative to reference) ───────────────────────
_STAR_WARN = 0.10   # −10% → yellow
_STAR_BAD  = 0.25   # −25% → red

# ── Field-tilt (edge − center eccentricity) thresholds ───────────────────────
_TILT_WARN = 0.08
_TILT_BAD  = 0.18


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt(val: object, decimals: int = 2) -> str:
    """Format a numeric value; return an em-dash for None / NaN."""
    if val is None:
        return "—"
    try:
        if math.isnan(float(val)):  # type: ignore[arg-type]
            return "—"
    except (TypeError, ValueError):
        return str(val)
    return f"{float(val):.{decimals}f}"  # type: ignore[arg-type]


def _is_nan(val: object) -> bool:
    try:
        return math.isnan(float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def _ecc_style(ecc: float) -> str:
    """Colour for eccentricity using absolute thresholds."""
    if _is_nan(ecc):
        return "dim"
    if ecc <= _ECC_KEEP:
        return "green"
    if ecc <= _ECC_REJECT:
        return "yellow"
    return "red"


def _higher_is_bad_style(val: float, ref: float) -> str:
    """Rich colour for metrics where higher → worse (relative to reference)."""
    if _is_nan(val) or _is_nan(ref) or ref == 0:
        return "dim"
    ratio = (val - ref) / ref
    if ratio <= _KEEP_THRESH:
        return "green"
    if ratio <= _MARGINAL_THRESH:
        return "yellow"
    return "red"


def _lower_is_bad_style(val: float, ref: float) -> str:
    """Rich colour for metrics where lower → worse (SNR, star count)."""
    if _is_nan(val) or _is_nan(ref) or ref == 0:
        return "dim"
    ratio = (val - ref) / ref
    if ratio >= -_KEEP_THRESH:
        return "green"
    if ratio >= -_MARGINAL_THRESH:
        return "yellow"
    return "red"


def _delta_ecc_str(ecc: float, ref_ecc: float) -> str:
    """Absolute eccentricity delta vs reference, e.g. '+0.052'."""
    if _is_nan(ecc) or _is_nan(ref_ecc):
        return "—"
    diff = ecc - ref_ecc
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.3f}"


def _gradient_style(grad: float) -> str:
    if _is_nan(grad):
        return "dim"
    if grad <= _GRAD_KEEP:
        return "green"
    if grad <= _GRAD_REJECT:
        return "yellow"
    return "red"


def _star_style(count: int, ref_count: int | None) -> str:
    if ref_count is None or ref_count == 0:
        return "white"
    ratio = (count - ref_count) / ref_count
    if ratio >= -_STAR_WARN:
        return "green"
    if ratio >= -_STAR_BAD:
        return "yellow"
    return "red"


def _tilt_style(delta: float) -> str:
    if _is_nan(delta):
        return "dim"
    if abs(delta) <= _TILT_WARN:
        return "green"
    if abs(delta) <= _TILT_BAD:
        return "yellow"
    return "red"


def _angle_str(angle: float, ecc: float) -> str:
    """Show trailing angle only when stars are actually elongated."""
    if _is_nan(angle) or _is_nan(ecc):
        return "—"
    if ecc < 0.10:
        return "—"     # stars too round for angle to be meaningful
    return f"{angle:.0f}°"


def _consistency_str(consistency: float, ecc: float) -> str:
    """Short label for angle consistency — blank when stars are round."""
    if _is_nan(consistency) or _is_nan(ecc) or ecc < 0.10:
        return "—"
    if consistency < 0.30:
        label = "atmo"
    elif consistency < 0.70:
        label = "mixed"
    else:
        label = "track"
    return f"{consistency:.2f} ({label})"


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def _verdict(frame: dict, reference: dict | None) -> tuple[str, str]:
    """
    Return (label, rich_style).

    Priority order:
      1. Trails                     → REJECT
      2. Eccentricity > 0.45        → REJECT
      3. Sky gradient > 0.30        → REJECT
      4. Star count < −25% of ref   → REJECT
      5. Eccentricity > 0.25        → MARGINAL
      6. Sky gradient > 0.10        → MARGINAL
      7. Star count < −10% of ref   → MARGINAL
      8. Otherwise                  → KEEP
    """
    nan = float("nan")
    ecc      = frame.get("eccentricity",  nan)
    gradient = frame.get("sky_gradient",  nan)
    trails   = frame.get("trail_count",   0) or 0
    stars    = frame.get("star_count",    0)

    ref_stars = (reference or {}).get("star_count", 0) or 0

    # Hard rejects
    if trails > 0:
        return "REJECT", "bold red"
    if not _is_nan(ecc) and ecc > _ECC_REJECT:
        return "REJECT", "bold red"
    if not _is_nan(gradient) and gradient > _GRAD_REJECT:
        return "REJECT", "bold red"
    if ref_stars > 0 and stars < (1.0 - _STAR_BAD) * ref_stars:
        return "REJECT", "bold red"

    # Marginal
    if not _is_nan(ecc) and ecc > _ECC_KEEP:
        return "MARGINAL", "bold yellow"
    if not _is_nan(gradient) and gradient > _GRAD_KEEP:
        return "MARGINAL", "bold yellow"
    if ref_stars > 0 and stars < (1.0 - _STAR_WARN) * ref_stars:
        return "MARGINAL", "bold yellow"

    return "KEEP", "bold green"


# ---------------------------------------------------------------------------
# Console table
# ---------------------------------------------------------------------------

def print_report(reference: dict | None, targets: list[dict]) -> None:
    """Print a rich-formatted star-quality comparison table to stdout."""
    ref_ecc   = (reference or {}).get("eccentricity",     float("nan"))
    ref_elong = (reference or {}).get("elongation_ratio",  float("nan"))
    ref_angle = (reference or {}).get("trailing_angle_deg", float("nan"))
    ref_cons  = (reference or {}).get("angle_consistency",  float("nan"))
    ref_tilt  = (reference or {}).get("ecc_field_delta",    float("nan"))
    ref_grad  = (reference or {}).get("sky_gradient",       float("nan"))
    ref_stars = (reference or {}).get("star_count",          0)
    ref_snr   = (reference or {}).get("snr",                float("nan"))
    ref_tc    = (reference or {}).get("trail_count",          0) or 0
    ref_name  = (reference or {}).get("filename", "")

    table = Table(
        title="[bold]star_check — frame star quality analysis[/bold]",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    table.add_column("File",        style="white", no_wrap=False, max_width=42)
    table.add_column("Ecc.",        justify="right")
    table.add_column("Δecc",        justify="right")
    table.add_column("Elong.",      justify="right")
    table.add_column("Trail °",     justify="right")
    table.add_column("Angle cons.", justify="right")
    table.add_column("Field tilt",  justify="right")
    table.add_column("Sky grad.",   justify="right")
    table.add_column("Stars",       justify="right")
    table.add_column("SNR",         justify="right")
    table.add_column("Trails",      justify="center")
    table.add_column("Verdict",     justify="center")

    # ── Reference row (if provided) ──────────────────────────────────────
    if reference is not None:
        ref_ecc   = reference.get("eccentricity",     float("nan"))
        ref_elong = reference.get("elongation_ratio",  float("nan"))
        ref_angle = reference.get("trailing_angle_deg", float("nan"))
        ref_cons  = reference.get("angle_consistency",  float("nan"))
        ref_tilt  = reference.get("ecc_field_delta",    float("nan"))
        ref_grad  = reference.get("sky_gradient",       float("nan"))
        ref_snr   = reference.get("snr",                float("nan"))
        ref_tc    = reference.get("trail_count",         0) or 0
        ref_name  = reference.get("filename", "")

        table.add_row(
            f"[bold blue]REF[/bold blue]  {ref_name}",
            _fmt(ref_ecc,   3),
            _fmt(ref_elong, 3),
            _angle_str(ref_angle, ref_ecc),
            _consistency_str(ref_cons, ref_ecc),
            _fmt(ref_tilt,  3),
            _fmt(ref_grad,  3),
            str(ref_stars or 0),
            _fmt(ref_snr,   1),
            "0" if ref_tc == 0 else f"[bold red]{ref_tc}[/bold red]",
            "[bold blue]REFERENCE[/bold blue]",
        )

    # ── Target rows ──────────────────────────────────────────────────────
    for frame in targets:
        if frame.get("error"):
            table.add_row(
                frame.get("filename", ""),
                "—", "—", "—", "—", "—", "—", "—", "—", "—", "—",
                "[bold red]ERROR[/bold red]",
            )
            continue

        ecc      = frame.get("eccentricity",     float("nan"))
        elong    = frame.get("elongation_ratio",  float("nan"))
        angle    = frame.get("trailing_angle_deg", float("nan"))
        cons     = frame.get("angle_consistency",  float("nan"))
        tilt     = frame.get("ecc_field_delta",    float("nan"))
        gradient = frame.get("sky_gradient",       float("nan"))
        stars    = frame.get("star_count",          0)
        snr      = frame.get("snr",                float("nan"))
        trails   = frame.get("trail_count",         0) or 0

        v_label, v_style = _verdict(frame, reference)
        es    = _ecc_style(ecc)
        de_s  = _higher_is_bad_style(ecc, ref_ecc)   # Δecc colour: relative to ref
        gs    = _gradient_style(gradient)
        ss    = _star_style(stars, ref_stars if reference else None)
        sns   = _lower_is_bad_style(snr, ref_snr) if reference else "white"
        ts_s  = _tilt_style(tilt)
        trl_s = "bold red" if trails > 0 else "green"

        table.add_row(
            frame.get("filename", ""),
            f"[{es}]{_fmt(ecc, 3)}[/{es}]",
            f"[{de_s}]{_delta_ecc_str(ecc, ref_ecc)}[/{de_s}]",
            _fmt(elong, 3),
            _angle_str(angle, ecc),
            _consistency_str(cons, ecc),
            f"[{ts_s}]{_fmt(tilt, 3)}[/{ts_s}]",
            f"[{gs}]{_fmt(gradient, 3)}[/{gs}]",
            f"[{ss}]{stars}[/{ss}]",
            f"[{sns}]{_fmt(snr, 1)}[/{sns}]",
            f"[{trl_s}]{trails}[/{trl_s}]",
            f"[{v_style}]{v_label}[/{v_style}]",
        )

    console.print(table)
    console.print()
    console.print(
        "[dim]Verdict thresholds — "
        f"[green]KEEP[/green]: ecc ≤ {_ECC_KEEP} | grad ≤ {_GRAD_KEEP} | "
        f"no trails | stars ≥ −10% of ref   "
        f"[yellow]MARGINAL[/yellow]: ecc ≤ {_ECC_REJECT} | grad ≤ {_GRAD_REJECT} | "
        f"no trails | stars ≥ −25% of ref   "
        f"[red]REJECT[/red]: ecc > {_ECC_REJECT} | grad > {_GRAD_REJECT} | "
        f"trail detected | stars < −25% of ref[/dim]"
    )
    console.print()
    console.print(
        "[dim]Trail °    : dominant elongation axis (0° = horizontal, 90° = vertical)[/dim]\n"
        "[dim]Angle cons.: trailing-direction coherence — "
        "< 0.30 = random (atmospheric), 0.30–0.70 = directional wind, "
        "> 0.70 = consistent = likely tracking / mount fault[/dim]\n"
        "[dim]Field tilt : edge_ecc − center_ecc — "
        "> 0.08 = mild tilt/curvature, > 0.18 = significant[/dim]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Watch-mode one-liner
# ---------------------------------------------------------------------------

def print_watch_line(frame: dict, reference: dict | None = None) -> None:
    """Compact single-line verdict for --watch mode."""
    from datetime import datetime
    ts   = datetime.now().strftime("%H:%M:%S")
    name = frame.get("filename", "")

    if frame.get("error"):
        console.print(
            f"[dim]{ts}[/dim]  [red]ERROR[/red]  {name}  [dim]{frame['error']}[/dim]"
        )
        return

    ecc      = frame.get("eccentricity",  float("nan"))
    gradient = frame.get("sky_gradient",  float("nan"))
    stars    = frame.get("star_count",    0)
    snr      = frame.get("snr",          float("nan"))
    trails   = frame.get("trail_count",  0) or 0
    angle    = frame.get("trailing_angle_deg", float("nan"))

    v_label, v_style = _verdict(frame, reference)
    es = _ecc_style(ecc)
    gs = _gradient_style(gradient)

    trail_str  = f"  [bold red]TRAILS={trails}[/bold red]" if trails > 0 else ""
    angle_str  = f"  angle {_angle_str(angle, ecc)}" if not _is_nan(ecc) and ecc >= 0.10 else ""

    console.print(
        f"[dim]{ts}[/dim]  "
        f"{name:<52}  "
        f"ecc [{es}]{_fmt(ecc, 3)}[/{es}]{angle_str}  "
        f"grad [{gs}]{_fmt(gradient, 3)}[/{gs}]  "
        f"stars {stars}  "
        f"SNR {_fmt(snr, 1)}"
        f"{trail_str}  "
        f"[{v_style}]{v_label}[/{v_style}]"
    )


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "role", "filename", "path",
    "eccentricity", "delta_ecc", "elongation_ratio",
    "trailing_angle_deg", "angle_consistency",
    "center_ecc", "edge_ecc", "ecc_field_delta",
    "star_count", "sky_adu", "sky_rms", "sky_gradient",
    "snr", "trail_count",
    "verdict",
    "filter", "exptime", "gain", "ccd_temp",
    "error",
]


def _safe_val(val: object) -> str:
    """Return empty string for None / NaN, otherwise the string representation."""
    if val is None:
        return ""
    try:
        if math.isnan(float(val)):  # type: ignore[arg-type]
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


def _csv_row(frame: dict, role: str, reference: dict | None) -> dict:
    if frame.get("error"):
        v_label = "ERROR"
    else:
        v_label, _ = _verdict(frame, reference)

    ref_ecc = (reference or {}).get("eccentricity", float("nan"))
    ecc     = frame.get("eccentricity", float("nan"))
    try:
        delta_ecc = "" if (_is_nan(ecc) or _is_nan(ref_ecc)) else f"{ecc - ref_ecc:+.4f}"
    except (TypeError, ValueError):
        delta_ecc = ""

    return {
        "role":               role,
        "filename":           frame.get("filename", ""),
        "path":               frame.get("path", ""),
        "eccentricity":       _safe_val(frame.get("eccentricity")),
        "delta_ecc":          delta_ecc,
        "elongation_ratio":   _safe_val(frame.get("elongation_ratio")),
        "trailing_angle_deg": _safe_val(frame.get("trailing_angle_deg")),
        "angle_consistency":  _safe_val(frame.get("angle_consistency")),
        "center_ecc":         _safe_val(frame.get("center_ecc")),
        "edge_ecc":           _safe_val(frame.get("edge_ecc")),
        "ecc_field_delta":    _safe_val(frame.get("ecc_field_delta")),
        "star_count":         _safe_val(frame.get("star_count")),
        "sky_adu":            _safe_val(frame.get("sky_adu")),
        "sky_rms":            _safe_val(frame.get("sky_rms")),
        "sky_gradient":       _safe_val(frame.get("sky_gradient")),
        "snr":                _safe_val(frame.get("snr")),
        "trail_count":        _safe_val(frame.get("trail_count")),
        "verdict":            v_label,
        "filter":             frame.get("filter") or "",
        "exptime":            _safe_val(frame.get("exptime")),
        "gain":               _safe_val(frame.get("gain")),
        "ccd_temp":           _safe_val(frame.get("ccd_temp")),
        "error":              frame.get("error") or "",
    }


def export_csv(
    reference: dict | None,
    targets: list[dict],
    csv_path: str,
) -> None:
    """Write results to a CSV file.  Reference row written first if given."""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    if reference is not None:
        rows.append(_csv_row(reference, "reference", None))
    for frame in targets:
        rows.append(_csv_row(frame, "target", reference))

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    console.print(f"[dim]CSV saved → {path}[/dim]")
