"""
astro_utils.py — one entry point for every workflow in this repo

Workflows are auto-discovered: any subfolder containing analyze.py is picked up.
Running one through the launcher is equivalent to running it directly — every
argument after the workflow name is forwarded verbatim.

Usage — run one workflow (args after the name go straight to it):
    python astro_utils.py -m analyze_stars -c my_session.yaml
    python astro_utils.py -m analyze_focus -r REF.fits -t "C:/data/*.fits"

Usage — run every workflow with the same arguments:
    python astro_utils.py -a -t "C:/data/session/*.fits"

Usage — drive everything from a master config in sessions/:
    python astro_utils.py -c my_night.yaml

Usage — inspect without running:
    python astro_utils.py --list
    python astro_utils.py -a --dry-run -t "C:/data/*.fits"
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

# Ensure workflows.py is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

import workflows as wf_registry
from workflows import Workflow

try:
    import argcomplete
except ImportError:
    argcomplete = None  # type: ignore[assignment]

console = Console()

_SCRIPT_DIR   = Path(__file__).parent
_SESSIONS_DIR = _SCRIPT_DIR / "sessions"

# Tokens that put a workflow into an endless watch loop.
_WATCH_FLAGS = ("-w", "--watch")

Job = tuple[Workflow, list[str]]


# ---------------------------------------------------------------------------
# Argument splitting
# ---------------------------------------------------------------------------

def _split_argv(argv: list[str]) -> tuple[list[str], str | None, list[str]]:
    """
    Split argv at the mode selector: everything after `-m NAME` or `-a` belongs
    to the workflow, not to us.

    This is what lets `-c` mean "master config" here and "session config" in the
    child without ambiguity.  Returns (launcher_argv, workflow_name, passthrough).
    """
    for i, tok in enumerate(argv):
        if tok in ("-m", "--module"):
            name = argv[i + 1] if i + 1 < len(argv) else None
            return argv[:i], name, argv[i + 2:]
        if tok.startswith("--module="):
            return argv[:i], tok.split("=", 1)[1], argv[i + 1:]
        if tok.startswith("-m") and len(tok) > 2 and not tok.startswith("--"):
            return argv[:i], tok[2:], argv[i + 1:]
        if tok in ("-a", "--all"):
            return [*argv[:i], "--all"], None, argv[i + 1:]
    return argv, None, []


def _as_arg_list(value, context: str) -> list[str]:
    """Accept `args` as a list (canonical) or a string (split shell-style)."""
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(v) for v in value]
    console.print(f"[red]{context}: 'args' must be a list of strings.[/red]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Master config
# ---------------------------------------------------------------------------

_KNOWN_TOP_KEYS = {"workflow", "workflows", "args", "config", "allow_watch"}
_KNOWN_ENTRY_KEYS = {"name", "workflow", "args", "config", "enabled"}


def _load_master_config(config_path: str) -> dict:
    """Bare filename resolves into sessions/; an explicit path is used as given."""
    p = Path(config_path)
    if p.is_absolute() or p.parent != Path("."):
        path = p
    else:
        _SESSIONS_DIR.mkdir(exist_ok=True)
        path = _SESSIONS_DIR / p.name

    if not path.exists():
        console.print(
            f"[red]Master config not found:[/red] {path}\n"
            f"[dim]Place launcher YAML files in: {_SESSIONS_DIR.resolve()}[/dim]"
        )
        sys.exit(1)

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        console.print(f"[red]Master config must be a YAML mapping:[/red] {path}")
        sys.exit(1)

    for key in set(cfg) - _KNOWN_TOP_KEYS:
        console.print(f"[yellow]Ignoring unknown key in master config:[/yellow] {key}")
    return cfg


def _entry_args(entry: dict, context: str) -> list[str]:
    """Resolve one entry's args, where `config: X` is sugar for `-c X`."""
    if "config" in entry and "args" in entry:
        console.print(f"[red]{context}: use either 'config' or 'args', not both.[/red]")
        sys.exit(1)
    if entry.get("config"):
        return ["-c", str(entry["config"])]
    return _as_arg_list(entry.get("args"), context)


def _jobs_from_config(
    cfg: dict,
    available: list[Workflow],
    extra_args: list[str],
) -> tuple[list[Job], bool]:
    """Turn a master config into a job list. Returns (jobs, allow_watch)."""
    allow_watch = bool(cfg.get("allow_watch", False))
    shared_args = _entry_args(cfg, "master config")

    entries: list[dict] = []

    if "workflows" in cfg:
        raw = cfg.get("workflows")
        if not isinstance(raw, list):
            console.print("[red]Master config: 'workflows' must be a list.[/red]")
            sys.exit(1)
        for item in raw:
            if isinstance(item, str):
                entries.append({"name": item})
            elif isinstance(item, dict):
                entries.append(item)
            else:
                console.print("[red]Master config: each workflow entry must be a name or a mapping.[/red]")
                sys.exit(1)

    elif "workflow" in cfg:
        selector = str(cfg.get("workflow") or "").strip()
        if not selector:
            console.print("[red]Master config: 'workflow' is empty.[/red]")
            sys.exit(1)
        if selector.lower() in ("all", "*"):
            entries = [{"name": w.name} for w in available if w.enabled]
        else:
            entries = [{"name": selector}]

    else:
        console.print(
            "[red]Master config needs a 'workflow:' or 'workflows:' key.[/red]\n"
            f"[dim]See {_SCRIPT_DIR / 'example_master.yaml'}[/dim]"
        )
        sys.exit(1)

    jobs: list[Job] = []
    for entry in entries:
        for key in set(entry) - _KNOWN_ENTRY_KEYS:
            console.print(f"[yellow]Ignoring unknown key in workflow entry:[/yellow] {key}")

        name = entry.get("name") or entry.get("workflow")
        if not name:
            console.print("[red]Master config: a workflow entry is missing 'name'.[/red]")
            sys.exit(1)
        if not entry.get("enabled", True):
            continue

        workflow = wf_registry.resolve(str(name), available)
        own_args = _entry_args(entry, f"Workflow '{name}'")
        # A per-entry args list replaces the shared one; merging two arg lists is unpredictable.
        args = own_args or shared_args
        jobs.append((workflow, [*args, *extra_args]))

    return jobs, allow_watch


# ---------------------------------------------------------------------------
# Watch guard
# ---------------------------------------------------------------------------

def _args_request_watch(args: list[str]) -> str | None:
    """Return the offending token if these args put the workflow into watch mode."""
    for tok in args:
        if tok in _WATCH_FLAGS or tok.startswith("--watch="):
            return tok
        if tok.startswith("-w") and len(tok) > 2 and not tok.startswith("--"):
            return tok
    return None


def _validate_queue(jobs: list[Job], allow_watch: bool) -> None:
    """
    Watch mode never returns on its own, so one watcher starves every job behind
    it.  Refuse before anything starts; a lone job has no queue to starve.
    """
    if len(jobs) <= 1:
        return

    offenders = [(w, tok) for w, args in jobs if (tok := _args_request_watch(args))]
    if not offenders:
        return

    listed = "\n".join(f"  [cyan]{w.name}[/cyan]  ({tok})" for w, tok in offenders)

    if not allow_watch:
        console.print(
            f"\n[red]Watch mode is not allowed when running multiple workflows.[/red]\n"
            f"[dim]Watch mode runs until Ctrl+C, so these would block everything queued behind them:[/dim]\n"
            f"{listed}\n\n"
            f"[dim]Run the watcher on its own:[/dim]  "
            f"[cyan]python astro_utils.py -m {offenders[0][0].name} {offenders[0][1]} DIR[/cyan]\n"
            f"[dim]Or override:[/dim]                 [cyan]--allow-watch[/cyan] "
            f"[dim](or 'allow_watch: true' in the master config)[/dim]"
        )
        sys.exit(1)

    console.print(
        f"\n[yellow]Watch mode enabled inside a multi-workflow run.[/yellow]\n"
        f"[dim]The queue will block on these until you press Ctrl+C:[/dim]\n"
        f"{listed}\n"
        f"[dim]Ctrl+C moves on to the next workflow instead of aborting the run.[/dim]"
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_STATUS_STYLE = {
    "ok":          "[green]ok[/green]",
    "failed":      "[red]failed[/red]",
    "interrupted": "[yellow]interrupted[/yellow]",
    "skipped":     "[dim]skipped[/dim]",
}


def _print_workflow_list(available: list[Workflow]) -> None:
    if not available:
        console.print(
            f"[yellow]No workflows found in:[/yellow] {_SCRIPT_DIR.resolve()}\n"
            f"[dim]A workflow is any subfolder containing analyze.py.[/dim]"
        )
        return

    table = Table(
        title="[bold]astro_utils — available workflows[/bold]",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    table.add_column("Workflow", style="bold cyan", no_wrap=True)
    table.add_column("Aliases",  style="dim", no_wrap=True)
    table.add_column("Folder",   style="white", no_wrap=True)
    table.add_column("Description", style="white", max_width=60)

    for w in available:
        name = w.name if w.enabled else f"{w.name} [dim](disabled)[/dim]"
        table.add_row(name, ", ".join(w.aliases) or "—", f"{w.directory.name}/", w.description or "—")

    console.print(table)


def _print_summary(results: list[tuple[str, str, int | None, float]]) -> None:
    table = Table(
        title="[bold]astro_utils — run summary[/bold]",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
        title_style="bold white",
    )
    table.add_column("Workflow", style="bold white", no_wrap=True)
    table.add_column("Status",   justify="center")
    table.add_column("Exit",     justify="right")
    table.add_column("Duration", justify="right")

    for name, status, code, elapsed in results:
        table.add_row(
            name,
            _STATUS_STYLE.get(status, status),
            "—" if code is None else str(code),
            "—" if status == "skipped" else f"{elapsed:.1f}s",
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _execute(jobs: list[Job], allow_watch: bool) -> int:
    results: list[tuple[str, str, int | None, float]] = []
    aborted = False

    for index, (workflow, args) in enumerate(jobs):
        if aborted:
            results.append((workflow.name, "skipped", None, 0.0))
            continue

        console.rule(f"[bold cyan]{workflow.name}[/bold cyan]")
        console.print(f"[dim]{wf_registry.format_command(workflow, args)}[/dim]\n")

        started = time.monotonic()
        status, code = wf_registry.run(workflow, args)
        elapsed = time.monotonic() - started
        results.append((workflow.name, status, code, elapsed))

        # Ctrl+C is how a watcher exits normally, so it only aborts the queue
        # when the user did not explicitly ask for watch mode.
        if status == "interrupted" and not allow_watch:
            aborted = True
        if aborted and index < len(jobs) - 1:
            console.print("\n[yellow]Interrupted — skipping remaining workflows.[/yellow]")

    if len(results) > 1 or any(r[1] != "ok" for r in results):
        _print_summary(results)

    if aborted or any(r[1] == "failed" for r in results):
        return 1
    return 0


def _print_dry_run(jobs: list[Job]) -> None:
    console.print("\n[bold cyan]Planned commands[/bold cyan] [dim](nothing was run)[/dim]")
    for workflow, args in jobs:
        console.print(f"  {wf_registry.format_command(workflow, args)}")
    console.print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    launcher_argv, module_name, passthrough = _split_argv(sys.argv[1:])

    parser = argparse.ArgumentParser(
        prog="astro_utils",
        description=(
            "Run any workflow in this repo from one place.\n"
            "Workflows are auto-discovered: any subfolder containing analyze.py is picked up.\n\n"
            "Everything after -m NAME (or after -a) is forwarded to the workflow untouched,\n"
            "so -c after -m is the workflow's session config, not the launcher's."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # List what's available
  python astro_utils.py --list

  # One workflow — args after the name go straight to it
  python astro_utils.py -m analyze_stars -c my_session.yaml
  python astro_utils.py -m analyze_focus -r REF.fits -t "C:/data/*.fits"

  # Every workflow, same arguments
  python astro_utils.py -a -t "C:/data/session/*.fits"

  # Master config (lives in sessions/ next to this script)
  python astro_utils.py -c my_night.yaml
  python astro_utils.py -c my_night.yaml --csv override.csv

  # Preview without running
  python astro_utils.py -a --dry-run -t "C:/data/*.fits"

notes:
  Launcher flags must come before -m / -a.
  Watch mode (-w) is refused when more than one workflow is queued; pass
  --allow-watch to override.  A watch_dir set inside a workflow's own session
  YAML cannot be detected by the launcher.
""",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--module", "-m",
        metavar="NAME",
        help="Workflow to run.  Everything after NAME is forwarded to it verbatim.",
    )
    mode_group.add_argument(
        "--all", "-a",
        action="store_true",
        help="Run every discovered workflow.  Trailing args are forwarded to each of them.",
    )
    config_arg = mode_group.add_argument(
        "--config", "-c",
        metavar="FILE",
        help="Master YAML config (filename only — lives in sessions/ next to this script).",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List discovered workflows and exit.",
    )
    parser.add_argument(
        "--allow-watch",
        action="store_true",
        help="Permit watch mode (-w) even when multiple workflows are queued.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands that would run, then exit without running them.",
    )

    if argcomplete is not None:
        config_arg.completer = _sessions_completer  # type: ignore[attr-defined]
        argcomplete.autocomplete(parser)

    args, unknown = parser.parse_known_args(launcher_argv)

    # With -c there is no mode selector to split on, so leftovers are the
    # workflow overrides.  Anywhere else they're a typo.
    if unknown:
        if args.config and module_name is None and not args.all:
            passthrough = [*unknown, *passthrough]
        else:
            parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    available = wf_registry.discover(_SCRIPT_DIR)

    if args.list:
        _print_workflow_list(available)
        return

    if module_name is not None:
        args.module = module_name
    elif args.module:
        # `-m` was consumed by argparse, which means no name followed it.
        parser.error("argument --module/-m: expected a workflow name")

    # No mode selected at all — show what's on offer instead of failing.
    if not args.module and not args.all and not args.config:
        _print_workflow_list(available)
        console.print(
            r"[dim]Run one with:[/dim]  [cyan]python astro_utils.py -m <workflow> \[args...][/cyan]" "\n"
            r"[dim]Run them all:[/dim]  [cyan]python astro_utils.py -a \[args...][/cyan]"
        )
        return

    if not available:
        console.print(
            f"[red]No workflows found in:[/red] {_SCRIPT_DIR.resolve()}\n"
            f"[dim]A workflow is any subfolder containing analyze.py.[/dim]"
        )
        sys.exit(1)

    allow_watch = args.allow_watch

    if args.config:
        cfg = _load_master_config(args.config)
        jobs, cfg_allow_watch = _jobs_from_config(cfg, available, passthrough)
        allow_watch = allow_watch or cfg_allow_watch
    elif args.all:
        jobs = [(w, list(passthrough)) for w in available if w.enabled]
    else:
        jobs = [(wf_registry.resolve(args.module, available), list(passthrough))]

    if not jobs:
        console.print("[yellow]Nothing to run — every workflow is disabled or filtered out.[/yellow]")
        return

    if args.dry_run:
        _print_dry_run(jobs)
        _validate_queue(jobs, allow_watch)
        return

    _validate_queue(jobs, allow_watch)
    sys.exit(_execute(jobs, allow_watch))


def _sessions_completer(prefix, **kwargs):  # noqa: ANN001
    """argcomplete completer — lists master YAML files in sessions/."""
    _SESSIONS_DIR.mkdir(exist_ok=True)
    return [
        f.name
        for f in sorted(_SESSIONS_DIR.glob("*.yaml")) + sorted(_SESSIONS_DIR.glob("*.yml"))
        if f.name.startswith(prefix)
    ]


if __name__ == "__main__":
    main()
