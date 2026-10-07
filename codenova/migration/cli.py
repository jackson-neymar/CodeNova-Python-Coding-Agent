"""Command-line interface for dependency migrations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from codenova.migration.engine import MigrationEngine, MigrationError
from codenova.migration.models import MigrationOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codenova migrate",
        description="Plan, apply, validate, and report a Python dependency upgrade.",
    )
    parser.add_argument("--package", help="Dependency name, for example pydantic")
    parser.add_argument("--from", dest="from_version", help="Current version or major line")
    parser.add_argument("--to", dest="to_version", help="Target version")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project root (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Plan and report edits without modifying project sources")
    parser.add_argument(
        "--resume", nargs="?", const="", metavar="RUN_ID",
        help="Resume a checkpointed migration (defaults to the latest run)",
    )
    parser.add_argument("--run-id", default="", help="Explicit id for a new run")
    parser.add_argument("--offline", action="store_true", help="Do not retrieve official migration documentation")
    parser.add_argument("--no-validate", action="store_true", help="Skip tests, lint, and type checks")
    parser.add_argument(
        "--check", action="append", default=[], metavar="COMMAND",
        help="Validation command; repeat to run multiple commands",
    )
    parser.add_argument("--timeout", type=int, default=300, help="Timeout per validation command in seconds")
    parser.add_argument("--report", type=Path, default=None, help="Write Markdown report at this path")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result")
    parser.add_argument("--agent", action="store_true", help="Use the configured CodeNova LLM agent for semantic repairs")
    parser.add_argument("--agent-iterations", type=int, default=2, help="Maximum semantic/validation repair turns")
    parser.add_argument(
        "--input-cost-per-million", type=float, default=0.0, metavar="USD",
        help="Provider input-token price in USD per million tokens (for experiment cost estimates)",
    )
    parser.add_argument(
        "--output-cost-per-million", type=float, default=0.0, metavar="USD",
        help="Provider output-token price in USD per million tokens (for experiment cost estimates)",
    )
    parser.add_argument("--rollback", metavar="RUN_ID", help="Restore files backed up by a migration run")
    parser.add_argument("--file", action="append", default=[], help="With --rollback, restore only this relative file")
    return parser


def _rollback(project: Path, run_id: str, selected: list[str]) -> int:
    root = project.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", run_id):
        print("Error: invalid migration run id", file=sys.stderr)
        return 2
    backup_root = root / ".codenova" / "migrations" / run_id / "backups"
    artifact_root = root / ".codenova" / "migrations" / run_id
    created_path = artifact_root / "created-files.json"
    created_files: list[str] = []
    if created_path.is_file():
        try:
            raw = json.loads(created_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                created_files = [str(item) for item in raw]
        except (OSError, json.JSONDecodeError):
            print("Error: invalid created-files rollback metadata", file=sys.stderr)
            return 2
    if not backup_root.is_dir() and not created_files:
        print(f"Error: no backups found for migration {run_id!r}", file=sys.stderr)
        return 2
    files = [path for path in backup_root.rglob("*") if path.is_file()] if backup_root.is_dir() else []
    selected_set: set[str] = set()
    if selected:
        selected_set = {Path(value).as_posix().lstrip("/") for value in selected}
        files = [path for path in files if path.relative_to(backup_root).as_posix() in selected_set]
        available = {path.relative_to(backup_root).as_posix() for path in files} | set(created_files)
        missing = selected_set - available
        if missing:
            print(f"Error: no backup for: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    created_to_remove = set(created_files) & (selected_set if selected else set(created_files))
    if not files and not created_to_remove:
        print("No files selected for rollback.", file=sys.stderr)
        return 2
    for backup in files:
        relative = backup.relative_to(backup_root)
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError:
            print(f"Error: unsafe backup path: {relative}", file=sys.stderr)
            return 2
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, destination)
        print(f"restored {relative.as_posix()}")
    for relative_value in sorted(created_to_remove):
        destination = (root / relative_value).resolve()
        try:
            destination.relative_to(root)
        except ValueError:
            print(f"Error: unsafe created-file path: {relative_value}", file=sys.stderr)
            return 2
        if destination.is_file() or destination.is_symlink():
            destination.unlink()
            print(f"removed created file {relative_value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.rollback:
        return _rollback(args.project, args.rollback, args.file)
    missing = [flag for flag, value in (("--package", args.package), ("--from", args.from_version), ("--to", args.to_version)) if not value]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.agent_iterations < 0:
        parser.error("--agent-iterations must not be negative")
    if args.input_cost_per_million < 0 or args.output_cost_per_million < 0:
        parser.error("token prices must not be negative")
    commands: list[tuple[str, ...]] = []
    for value in args.check:
        command = tuple(shlex.split(value))
        if not command:
            parser.error("--check command must not be empty")
        commands.append(command)

    options = MigrationOptions(
        project_root=args.project,
        package=args.package,
        from_version=args.from_version,
        to_version=args.to_version,
        dry_run=args.dry_run,
        resume=args.resume is not None,
        fetch_docs=not args.offline,
        run_validation=not args.no_validate,
        validation_commands=commands,
        timeout_seconds=args.timeout,
        report_path=args.report,
        run_id=(args.resume or args.run_id),
        max_agent_iterations=args.agent_iterations,
        input_cost_per_million=args.input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
    )
    semantic_handler = None
    if args.agent:
        # Validate the provider configuration before deterministic edits begin.
        from codenova.__main__ import _run_prompt
        from codenova.config import ConfigError, load_config
        from codenova.hooks import HookConfigError, HookEngine, load_hooks
        from codenova.permissions import PermissionMode

        previous_cwd = Path.cwd()
        try:
            os.chdir(args.project.resolve())
            config = load_config()
            hooks = load_hooks(config.raw_hooks)
        except (ConfigError, HookConfigError) as exc:
            print(f"Agent configuration error: {exc}", file=sys.stderr)
            return 2
        finally:
            os.chdir(previous_cwd)
        hook_engine = HookEngine(hooks) if hooks else None
        permission_mode = PermissionMode(config.permission_mode)
        provider = config.providers[0]
        options.agent_provider = provider.name
        options.agent_model = provider.model

        def semantic_handler(prompt: str) -> dict[str, int | float]:
            old_cwd = Path.cwd()
            try:
                os.chdir(args.project.resolve())
                return asyncio.run(
                    _run_prompt(config, permission_mode, hook_engine, prompt, "text")
                )
            finally:
                os.chdir(old_cwd)

    try:
        result = MigrationEngine(
            options,
            progress=(lambda message: print(f"[migrate] {message}", file=sys.stderr)),
            semantic_handler=semantic_handler,
        ).run()
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Migration interrupted. Re-run with --resume to continue.", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"Migration {result.status}: {len(result.changes)} change(s), "
            f"{len(result.issues)} issue(s), report: {result.report_path}"
        )
    return 0 if result.status in {"completed", "dry-run"} else 1


__all__ = ["build_parser", "main"]
