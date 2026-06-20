"""
analyze.py — focus_check entry point

Analyze one reference FITS file against N target frames and report
star quality metrics (FWHM, HFR, eccentricity, SNR, star count, sky bg).

Usage — CLI:
    python analyze.py --reference REF.fits --targets A.fits B.fits
    python analyze.py -r REF.fits -t "C:/data/session/*.fits" --csv out.csv

Usage — config file (must be in the sessions/ folder):
    python analyze.py --config my_session.yaml
    python analyze.py -c my_session.yaml --csv override_output.csv
"""

from __future__ import annotations

import argparse
import glob
import sys
import time
from pathlib import Path

# Ensure sibling modules (metrics, report) are importable regardless of cwd
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from rich.console import Console

from metrics import analyze_frame
from report import export_csv, print_report, print_watch_line

console = Console()

# Directories relative to this script file (created on demand)
_SCRIPT_DIR  = Path(__file__).parent
_SESSIONS_DIR = _SCRIPT_DIR / "sessions"
_RESULTS_DIR  = _SCRIPT_DIR / "results"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_csv_path(csv_out: str) -> str:
    """
    Resolve the final CSV output path.

    Rules:
      - Absolute path              → used as-is
      - Relative path with dirs    → used as-is (relative to cwd)
      - Bare filename (no dirs)    → routed into results/ next to this script
    """
    p = Path(csv_out)
    if p.is_absolute() or p.parent != Path("."):
        return str(p)
    _RESULTS_DIR.mkdir(exist_ok=True)
    return str(_RESULTS_DIR / p)

def _expand_paths(raw: list[str]) -> list[str]:
    """
    Expand glob patterns in a list of path strings.
    Keeps non-matching entries as-is so they surface as proper errors later.
    """
    expanded: list[str] = []
    for p in raw:
        matched = sorted(glob.glob(p, recursive=True))
        if matched:
            expanded.extend(matched)
        else:
            expanded.append(p)
    return expanded


def _load_config(config_path: str) -> dict:
    p = Path(config_path)

    # Reject paths that reference a directory — all configs must live in sessions/
    if p.parent != Path("."):
        console.print(
            f"[red]Config files must be placed in the sessions/ folder.[/red]\n"
            f"[dim]Pass just the filename, e.g.:  -c {p.name}[/dim]"
        )
        sys.exit(1)

    # Resolve bare filename → sessions/ directory
    path = _SESSIONS_DIR / p.name
    _SESSIONS_DIR.mkdir(exist_ok=True)

    if not path.exists():
        console.print(
            f"[red]Config file not found:[/red] {path}\n"
            f"[dim]Place your YAML files in: {_SESSIONS_DIR.resolve()}[/dim]"
        )
        sys.exit(1)

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg or {}


def _parse_config(cfg: dict) -> tuple[str, list[str], str | None, str | None]:
    """
    Extract (reference_path, target_paths, csv_out, watch_dir) from a YAML config dict.
    """
    reference = cfg.get("reference")
    if not reference:
        console.print("[red]Config must include a 'reference' key.[/red]")
        sys.exit(1)

    targets_raw = cfg.get("targets", [])
    if isinstance(targets_raw, str):
        targets_raw = [targets_raw]
    targets = _expand_paths([str(t) for t in targets_raw])

    output_cfg = cfg.get("output", {})
    csv_out = output_cfg.get("csv") if isinstance(output_cfg, dict) else None

    watch_dir = cfg.get("watch_dir") or None

    return str(reference), targets, csv_out, watch_dir


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="focus_check",
        description=(
            "Compare FITS frames against a reference to evaluate focus quality.\n"
            "Reports FWHM, HFR, eccentricity, star count, sky background, and SNR."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # CLI — individual files
  python analyze.py -r REF.fits -t night1.fits night2.fits night3.fits

  # CLI — glob pattern (quote on Windows to prevent shell expansion)
  python analyze.py -r REF.fits -t "C:/data/session/*.fits" --csv results.csv

  # Config file — just the filename, it lives in sessions/ automatically
  python analyze.py -c my_session.yaml

  # Config file with CSV path override
  python analyze.py -c my_session.yaml --csv custom_output.csv
""",
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--reference", "-r",
        metavar="FILE",
        help="Reference FITS file (your known-good focus frame).",
    )
    input_group.add_argument(
        "--config", "-c",
        metavar="FILE",
        help="Path to a YAML config file (reference + targets + output settings).",
    )

    parser.add_argument(
        "--targets", "-t",
        metavar="FILE",
        nargs="+",
        help=(
            "Target FITS files to evaluate. "
            "Supports glob patterns (quote on Windows: '*.fits')."
        ),
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help="Export full results table to this CSV file.",
    )
    parser.add_argument(
        "--watch", "-w",
        metavar="DIR",
        help=(
            "Watch a directory for new FITS files and auto-verdict each one "
            "as it arrives. Prints a full summary table on Ctrl+C."
        ),
    )

    args = parser.parse_args()

    # --- Resolve inputs ---
    watch_dir: str | None = None
    if args.config:
        cfg = _load_config(args.config)
        reference_path, target_paths, csv_out, watch_dir = _parse_config(cfg)
        if args.csv:
            csv_out = args.csv
        if args.watch:
            watch_dir = args.watch  # CLI flag overrides config
    else:
        if not args.targets and not args.watch:
            parser.error("--targets / -t or --watch / -w is required when not using --config")
        reference_path = args.reference
        target_paths   = _expand_paths(args.targets) if args.targets else []
        csv_out        = args.csv
        watch_dir      = args.watch

    # --- Dispatch to watch mode ---
    if watch_dir:
        _watch_mode(reference_path, watch_dir, _resolve_csv_path(csv_out) if csv_out else None)
        return

    # --- Analyze reference ---
    console.print(f"\n[cyan]Analyzing reference:[/cyan] {Path(reference_path).name}")
    ref_result = analyze_frame(reference_path)
    if ref_result.get("error"):
        console.print(
            f"[red]Failed to read reference file:[/red] {ref_result['error']}"
        )
        sys.exit(1)

    _print_meta(ref_result)

    # --- Analyze targets ---
    if not target_paths:
        console.print("[yellow]No target files specified — nothing to compare.[/yellow]")
        sys.exit(0)

    console.print(f"\n[cyan]Analyzing {len(target_paths)} target frame(s)...[/cyan]")
    target_results: list[dict] = []
    for path in target_paths:
        result = analyze_frame(path)
        status = "[red]ERROR[/red]" if result.get("error") else "[green]OK[/green]"
        console.print(f"  {status}  {Path(path).name}")
        if result.get("error"):
            console.print(f"         [dim]{result['error']}[/dim]")
        target_results.append(result)

    # --- Report ---
    console.print()
    print_report(ref_result, target_results)

    # --- CSV ---
    if csv_out:
        export_csv(ref_result, target_results, _resolve_csv_path(csv_out))


def _watch_mode(reference_path: str, watch_dir: str, csv_out: str | None) -> None:
    """
    Poll watch_dir for new FITS files and auto-verdict each as it arrives.
    Prints a compact one-liner per frame. On Ctrl+C, prints a full summary table.
    """
    watch_path = Path(watch_dir)
    if not watch_path.is_dir():
        console.print(f"[red]Watch directory not found:[/red] {watch_dir}")
        sys.exit(1)

    console.print(f"\n[cyan]Analyzing reference:[/cyan] {Path(reference_path).name}")
    ref_result = analyze_frame(reference_path)
    if ref_result.get("error"):
        console.print(f"[red]Failed to read reference file:[/red] {ref_result['error']}")
        sys.exit(1)
    _print_meta(ref_result)

    _FITS_PATTERNS = ("*.fits", "*.fit", "*.FITS", "*.FIT")
    seen: set[str] = set()
    for pattern in _FITS_PATTERNS:
        for f in watch_path.glob(pattern):
            seen.add(str(f))

    console.print(f"\n[cyan]Watching:[/cyan] {watch_path.resolve()}")
    console.print(
        f"[dim]{len(seen)} existing file(s) pre-loaded — "
        f"waiting for new subs... (Ctrl+C for summary)[/dim]\n"
    )

    results: list[dict] = []
    try:
        while True:
            current: set[str] = set()
            for pattern in _FITS_PATTERNS:
                current.update(str(f) for f in watch_path.glob(pattern))

            new_files = sorted(current - seen)
            for filepath in new_files:
                p = Path(filepath)
                # Mark seen immediately — prevents infinite retry on corrupt/truncated files.
                # If still being written, remove from seen so it's retried next cycle.
                seen.add(filepath)
                try:
                    size1 = p.stat().st_size
                    time.sleep(2)
                    size2 = p.stat().st_size
                    if size1 != size2:
                        seen.discard(filepath)  # still writing — retry next cycle
                        continue
                except OSError:
                    continue  # file vanished — leave it in seen, skip permanently
                result = analyze_frame(filepath)
                results.append(result)
                print_watch_line(result, ref_result)

                if csv_out:
                    export_csv(ref_result, results, csv_out)

            time.sleep(5)

    except KeyboardInterrupt:
        console.print("\n[dim]Watch stopped.[/dim]")
        if results:
            console.print()
            print_report(ref_result, results)
            if csv_out:
                export_csv(ref_result, results, csv_out)
        else:
            console.print("[dim]No new frames were captured during this watch session.[/dim]")


def _print_meta(frame: dict) -> None:
    """Print FITS header metadata for the reference frame."""
    parts = []
    if frame.get("filter"):
        parts.append(f"filter={frame['filter']}")
    if frame.get("exptime") is not None:
        parts.append(f"exp={frame['exptime']}s")
    if frame.get("ccd_temp") is not None:
        parts.append(f"temp={frame['ccd_temp']}°C")
    if frame.get("gain") is not None:
        parts.append(f"gain={frame['gain']}")
    if parts:
        console.print(f"         [dim]{' | '.join(parts)}[/dim]")


if __name__ == "__main__":
    main()
