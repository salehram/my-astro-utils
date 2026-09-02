"""
workflows.py — workflow discovery and dispatch for astro_utils.py

A "workflow" is any immediate subdirectory of the repo root that contains an
entry script (``analyze.py`` by default).  Dropping in a new folder is all it
takes to register a new tool — no launcher changes required.

An optional ``workflow.yaml`` inside the folder supplies friendly metadata:

    name:        analyze_stars                  # default: folder name
    aliases:     [star_check, stars]            # default: []
    description: Star roundness, trailing...    # default: first line of README.md
    entry:       analyze.py                     # default: analyze.py
    order:       20                             # default: 100; controls --all order
    enabled:     true                           # default: true; false = skipped by --all

Workflows are launched as subprocesses rather than imported.  This is not an
implementation detail to be optimized away — see DISPATCH NOTES below.
"""

# DISPATCH NOTES — why subprocess, not import
#
# Every workflow folder ships its own `metrics.py` and `report.py` and imports
# them by bare name after a `sys.path.insert`.  Importing two workflows into one
# interpreter means the second one silently receives the first one's
# `sys.modules["metrics"]` — wrong code, no error.  On top of that, each
# `main()` takes no argv parameter (it reads `sys.argv` directly) and calls
# `sys.exit(1)` on error.  In-process dispatch would therefore require
# monkeypatching argv, trapping SystemExit, and juggling sys.modules/sys.path.
#
# A subprocess sidesteps all of it and makes the launcher's core promise literal:
# the workflow behaves exactly as if it had been invoked directly.

from __future__ import annotations

import difflib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

_ROOT = Path(__file__).parent

_DEFAULT_ENTRY = "analyze.py"
_MANIFEST_NAME = "workflow.yaml"
_DEFAULT_ORDER = 100

# Folders that are never workflows, even if they somehow contain an entry script.
_SKIP_DIRS = {"sessions", "results", "__pycache__", "venv", "node_modules"}


@dataclass
class Workflow:
    name: str
    directory: Path
    entry_path: Path
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    order: int = _DEFAULT_ORDER
    enabled: bool = True

    @property
    def display_entry(self) -> str:
        """Entry script path relative to the repo root, for echoing commands."""
        try:
            return str(self.entry_path.relative_to(_ROOT))
        except ValueError:
            return str(self.entry_path)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def normalize(name: str) -> str:
    """Fold case and treat '-' and '_' as equivalent, so `Analyze-Stars` == `analyze_stars`."""
    return name.strip().lower().replace("-", "_")


def _first_readme_line(directory: Path) -> str:
    """First non-empty, non-heading line of README.md — used as a fallback description."""
    readme = directory / "README.md"
    if not readme.exists():
        return ""
    try:
        with readme.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    return stripped
    except OSError:
        return ""
    return ""


def _load_manifest(directory: Path) -> dict:
    manifest = directory / _MANIFEST_NAME
    if not manifest.exists():
        return {}
    try:
        with manifest.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        console.print(f"[yellow]Ignoring malformed {manifest}:[/yellow] {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _build_workflow(directory: Path) -> Workflow | None:
    cfg = _load_manifest(directory)

    entry_name = str(cfg.get("entry") or _DEFAULT_ENTRY)
    entry_path = directory / entry_name
    if not entry_path.is_file():
        return None

    aliases_raw = cfg.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [aliases_raw]

    order_raw = cfg.get("order", _DEFAULT_ORDER)
    try:
        order = int(order_raw)
    except (TypeError, ValueError):
        order = _DEFAULT_ORDER

    return Workflow(
        name=str(cfg.get("name") or directory.name),
        directory=directory,
        entry_path=entry_path,
        description=str(cfg.get("description") or _first_readme_line(directory)),
        aliases=[str(a) for a in aliases_raw],
        order=order,
        enabled=bool(cfg.get("enabled", True)),
    )


def _check_duplicates(workflows: list[Workflow]) -> None:
    """A name/alias claimed by two folders makes `-m` ambiguous — fail loudly at startup."""
    owners: dict[str, tuple[str, Path]] = {}
    for wf in workflows:
        for label in [wf.name, *wf.aliases]:
            key = normalize(label)
            if key in owners:
                prev_label, prev_dir = owners[key]
                console.print(
                    f"[red]Duplicate workflow name '{label}'.[/red]\n"
                    f"[dim]Claimed by both {prev_dir.name}/ (as '{prev_label}') "
                    f"and {wf.directory.name}/.[/dim]\n"
                    f"[dim]Fix the 'name' or 'aliases' in one of their {_MANIFEST_NAME} files.[/dim]"
                )
                sys.exit(1)
            owners[key] = (label, wf.directory)


def discover(root: Path | None = None) -> list[Workflow]:
    """Find every workflow folder under `root`, sorted by manifest order then name."""
    base = root or _ROOT
    found: list[Workflow] = []

    for directory in sorted(p for p in base.iterdir() if p.is_dir()):
        if directory.name.startswith((".", "_")) or directory.name in _SKIP_DIRS:
            continue
        wf = _build_workflow(directory)
        if wf is not None:
            found.append(wf)

    _check_duplicates(found)
    found.sort(key=lambda w: (w.order, w.name))
    return found


def resolve(name: str, workflows: list[Workflow]) -> Workflow:
    """Look up a workflow by name or alias, exiting with a suggestion when it misses."""
    key = normalize(name)
    for wf in workflows:
        if key == normalize(wf.name) or any(key == normalize(a) for a in wf.aliases):
            return wf

    known = [wf.name for wf in workflows]
    suggestions = difflib.get_close_matches(key, [normalize(k) for k in known], n=1)
    hint = ""
    if suggestions:
        match = next(k for k in known if normalize(k) == suggestions[0])
        hint = f"[dim]Did you mean:[/dim] [cyan]{match}[/cyan]\n"

    console.print(
        f"[red]Unknown workflow:[/red] {name}\n"
        f"{hint}"
        f"[dim]Available:[/dim] {', '.join(known) if known else '(none found)'}"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def build_command(workflow: Workflow, args: list[str]) -> list[str]:
    """The exact argv used to launch a workflow. `sys.executable` keeps the active venv."""
    return [sys.executable, str(workflow.entry_path), *args]


def format_command(workflow: Workflow, args: list[str]) -> str:
    """Render the command as a copy-pasteable line for direct invocation."""
    parts = ["python", workflow.display_entry, *args]
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def run(workflow: Workflow, args: list[str]) -> tuple[str, int]:
    """
    Launch a workflow and wait for it.  Returns (status, exit_code) where status
    is "ok", "failed", or "interrupted".

    stdio is deliberately inherited (no capture) so the child keeps its rich
    colors and interactive session picker; cwd is deliberately untouched because
    workflows resolve FITS globs against it.
    """
    proc = subprocess.Popen(build_command(workflow, args))
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        # Ctrl+C already reached the child via the console process group;
        # wait again so it can flush its own summary table before we report.
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            code = proc.wait()
        return "interrupted", code
    return ("ok" if code == 0 else "failed"), code
