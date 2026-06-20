"""
report.py — console table and CSV export for focus_check results

Color coding (relative to reference FWHM):
  green   ≤ +10%     → KEEP
  yellow  ≤ +25%     → MARGINAL
  red     >  +25%    → REJECT

Star count and SNR use inverted thresholds (lower = worse).
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

# Verdict thresholds (fraction relative to reference)
_KEEP_THRESH     = 0.10   # +10%  → green  / KEEP
_MARGINAL_THRESH = 0.25   # +25%  → yellow / MARGINAL
_STAR_DROP_WARN  = 0.10   # -10%  star count drop → yellow
_STAR_DROP_BAD   = 0.30   # -30%  star count drop → red


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt(val: object, decimals: int = 2) -> str:
    """Format a numeric value; return an em-dash for None/NaN."""
    if val is None:
        return "—"
    try:
        if math.isnan(float(val)):  # type: ignore[arg-type]
            return "—"
    except (TypeError, ValueError):
        return str(val)
    return f"{float(val):.{decimals}f}"  # type: ignore[arg-type]


def _is_nan(val: object) -> bool:
    """Return True for None, NaN, or anything that can't be parsed as a number."""
    try:
        return math.isnan(float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def _delta_str(val: float, ref: float) -> str:
    """Percentage delta relative to reference, e.g. '+12.3%'."""
    try:
        if math.isnan(val) or math.isnan(ref) or ref == 0:
            return "—"
    except TypeError:
        return "—"
    pct = (val - ref) / ref * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def _higher_is_bad_style(val: float, ref: float) -> str:
    """Rich colour for metrics where higher → worse (FWHM, HFR, ecc, sky)."""
    try:
        if math.isnan(val) or math.isnan(ref) or ref == 0:
            return "dim"
    except TypeError:
        return "dim"
    ratio = (val - ref) / ref
    if ratio <= _KEEP_THRESH:
        return "green"
    if ratio <= _MARGINAL_THRESH:
        return "yellow"
    return "red"


def _lower_is_bad_style(val: float, ref: float) -> str:
    """Rich colour for metrics where lower → worse (SNR, star count)."""
    try:
        if math.isnan(val) or math.isnan(ref) or ref == 0:
            return "dim"
    except TypeError:
        return "dim"
    ratio = (val - ref) / ref
    if ratio >= -_KEEP_THRESH:
        return "green"
    if ratio >= -_MARGINAL_THRESH:
        return "yellow"
    return "red"


def _star_style(count: int, ref_count: int) -> str:
    if ref_count == 0:
        return "dim"
    ratio = (count - ref_count) / ref_count
    if ratio >= -_STAR_DROP_WARN:
        return "green"
    if ratio >= -_STAR_DROP_BAD:
        return "yellow"
    return "red"


def _verdict(fwhm: float, ref_fwhm: float) -> tuple[str, str]:
    """Return (label, rich_style) for the verdict column."""
    try:
        if math.isnan(fwhm) or math.isnan(ref_fwhm) or ref_fwhm == 0:
            return "UNKNOWN", "dim"
    except TypeError:
        return "UNKNOWN", "dim"
    ratio = (fwhm - ref_fwhm) / ref_fwhm
    if ratio <= _KEEP_THRESH:
        return "KEEP",     "bold green"
    if ratio <= _MARGINAL_THRESH:
        return "MARGINAL", "bold yellow"
    return "REJECT",   "bold red"


# ---------------------------------------------------------------------------
# Console table
# ---------------------------------------------------------------------------

def print_report(reference: dict, targets: list[dict]) -> None:
    """Print a rich-formatted comparison table to stdout."""
    ref_fwhm        = reference.get("fwhm_px",      float("nan"))
    ref_fwhm_arcsec = reference.get("fwhm_arcsec",  float("nan"))
    ref_hfr         = reference.get("hfr_px",        float("nan"))
    ref_ecc         = reference.get("eccentricity",  float("nan"))
    ref_stars       = reference.get("star_count",    0)
    ref_sky         = reference.get("sky_adu",       float("nan"))
    ref_snr         = reference.get("snr",           float("nan"))

    # Show arcsec alongside pixels when plate scale headers were present
    has_arcsec = (
        reference.get("plate_scale") is not None
        and not _is_nan(ref_fwhm_arcsec)
    )

    def _fwhm_cell(px: float, arcsec: float) -> str:
        if has_arcsec and not _is_nan(arcsec):
            return f'{_fmt(px)} / {_fmt(arcsec, 1)}"'
        return _fmt(px)

    fwhm_col = 'FWHM (px / ")' if has_arcsec else "FWHM (px)"

    table = Table(
        title="[bold]focus_check — frame quality analysis[/bold]",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    table.add_column("File",      style="white", no_wrap=False, max_width=42)
    table.add_column(fwhm_col,    justify="right")
    table.add_column("ΔFWHM",     justify="right")
    table.add_column("HFR (px)",  justify="right")
    table.add_column("Ecc.",      justify="right")
    table.add_column("Stars",     justify="right")
    table.add_column("Sky (ADU)", justify="right")
    table.add_column("SNR",       justify="right")
    table.add_column("Verdict",   justify="center")

    # Reference row (always blue, no verdict delta)
    table.add_row(
        f"[bold blue]REF[/bold blue]  {reference.get('filename', '')}",
        _fwhm_cell(ref_fwhm, ref_fwhm_arcsec),
        "—",
        _fmt(ref_hfr),
        _fmt(ref_ecc, 3),
        str(ref_stars),
        _fmt(ref_sky, 1),
        _fmt(ref_snr, 1),
        "[bold blue]REFERENCE[/bold blue]",
    )

    for frame in targets:
        if frame.get("error"):
            table.add_row(
                frame.get("filename", ""),
                "—", "—", "—", "—", "—", "—", "—",
                f"[bold red]ERROR[/bold red]",
            )
            continue

        fwhm        = frame.get("fwhm_px",      float("nan"))
        fwhm_arcsec = frame.get("fwhm_arcsec",  float("nan"))
        hfr         = frame.get("hfr_px",       float("nan"))
        ecc         = frame.get("eccentricity",  float("nan"))
        stars       = frame.get("star_count",   0)
        sky         = frame.get("sky_adu",      float("nan"))
        snr         = frame.get("snr",          float("nan"))

        v_label, v_style = _verdict(fwhm, ref_fwhm)
        fs  = _higher_is_bad_style(fwhm, ref_fwhm)
        hs  = _higher_is_bad_style(hfr,  ref_hfr)
        es  = _higher_is_bad_style(ecc,  ref_ecc)
        ss  = _star_style(stars, ref_stars)
        sks = _higher_is_bad_style(sky,  ref_sky)
        ns  = _lower_is_bad_style(snr,   ref_snr)

        table.add_row(
            frame.get("filename", ""),
            f"[{fs}]{_fwhm_cell(fwhm, fwhm_arcsec)}[/{fs}]",
            f"[{fs}]{_delta_str(fwhm, ref_fwhm)}[/{fs}]",
            f"[{hs}]{_fmt(hfr)}[/{hs}]",
            f"[{es}]{_fmt(ecc, 3)}[/{es}]",
            f"[{ss}]{stars}[/{ss}]",
            f"[{sks}]{_fmt(sky, 1)}[/{sks}]",
            f"[{ns}]{_fmt(snr, 1)}[/{ns}]",
            f"[{v_style}]{v_label}[/{v_style}]",
        )

    console.print(table)
    console.print()
    console.print(
        "[dim]Verdict thresholds — "
        "[green]KEEP[/green]: ΔFWHM ≤ +10%   "
        "[yellow]MARGINAL[/yellow]: ≤ +25%   "
        "[red]REJECT[/red]: > +25%[/dim]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Watch-mode one-liner
# ---------------------------------------------------------------------------

def print_watch_line(frame: dict, reference: dict) -> None:
    """Compact single-line verdict for --watch mode."""
    from datetime import datetime
    ts       = datetime.now().strftime("%H:%M:%S")
    name     = frame.get("filename", "")
    ref_fwhm = reference.get("fwhm_px", float("nan"))

    if frame.get("error"):
        console.print(
            f"[dim]{ts}[/dim]  [red]ERROR[/red]  {name}  [dim]{frame['error']}[/dim]"
        )
        return

    fwhm        = frame.get("fwhm_px",     float("nan"))
    fwhm_arcsec = frame.get("fwhm_arcsec", float("nan"))
    hfr         = frame.get("hfr_px",      float("nan"))
    stars       = frame.get("star_count",  0)
    snr         = frame.get("snr",         float("nan"))

    v_label, v_style = _verdict(fwhm, ref_fwhm)
    fs    = _higher_is_bad_style(fwhm, ref_fwhm)
    delta = _delta_str(fwhm, ref_fwhm)

    fwhm_str = _fmt(fwhm) + "px"
    if not _is_nan(fwhm_arcsec):
        fwhm_str += f' / {_fmt(fwhm_arcsec, 1)}"'

    console.print(
        f"[dim]{ts}[/dim]  "
        f"{name:<52}  "
        f"FWHM [{fs}]{fwhm_str}[/{fs}] ([{fs}]{delta}[/{fs}])  "
        f"HFR {_fmt(hfr)}px  "
        f"stars {stars}  "
        f"SNR {_fmt(snr, 1)}  "
        f"[{v_style}]{v_label}[/{v_style}]"
    )


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "role", "filename", "path",
    "fwhm_px", "fwhm_arcsec", "delta_fwhm_pct",
    "hfr_px", "eccentricity",
    "star_count", "sky_adu", "sky_rms", "snr",
    "verdict", "filter", "exptime", "gain", "ccd_temp",
    "error",
]


def _safe_nan(val: object) -> str:
    if val is None:
        return ""
    try:
        if math.isnan(float(val)):  # type: ignore[arg-type]
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


def _csv_row(frame: dict, role: str, ref_fwhm: float) -> dict:
    fwhm = frame.get("fwhm_px", float("nan"))
    try:
        delta_pct = (
            f"{(fwhm - ref_fwhm) / ref_fwhm * 100:.1f}"
            if not math.isnan(fwhm) and not math.isnan(ref_fwhm) and ref_fwhm != 0
            else ""
        )
    except TypeError:
        delta_pct = ""

    if role == "reference":
        v_label = "REFERENCE"
    elif frame.get("error"):
        v_label = "ERROR"
    else:
        v_label, _ = _verdict(fwhm, ref_fwhm)

    return {
        "role":           role,
        "filename":       frame.get("filename", ""),
        "path":           frame.get("path", ""),
        "fwhm_px":        _safe_nan(fwhm),
        "fwhm_arcsec":    _safe_nan(frame.get("fwhm_arcsec")),
        "delta_fwhm_pct": delta_pct,
        "hfr_px":         _safe_nan(frame.get("hfr_px")),
        "eccentricity":   _safe_nan(frame.get("eccentricity")),
        "star_count":     _safe_nan(frame.get("star_count")),
        "sky_adu":        _safe_nan(frame.get("sky_adu")),
        "sky_rms":        _safe_nan(frame.get("sky_rms")),
        "snr":            _safe_nan(frame.get("snr")),
        "verdict":        v_label,
        "filter":         frame.get("filter") or "",
        "exptime":        _safe_nan(frame.get("exptime")),
        "gain":           _safe_nan(frame.get("gain")),
        "ccd_temp":       _safe_nan(frame.get("ccd_temp")),
        "error":          frame.get("error") or "",
    }


def export_csv(reference: dict, targets: list[dict], output_path: str | Path) -> None:
    """Write all results to a CSV file."""
    output_path = Path(output_path)
    ref_fwhm = reference.get("fwhm_px", float("nan"))

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerow(_csv_row(reference, "reference", ref_fwhm))
        for frame in targets:
            writer.writerow(_csv_row(frame, "target", ref_fwhm))

    console.print(f"[dim]CSV saved → {output_path.resolve()}[/dim]")
