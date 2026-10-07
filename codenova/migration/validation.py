"""Validation command discovery and structured diagnostic parsing."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from codenova.migration.models import MigrationIssue, RiskLevel, SourceLocation, ValidationRun


DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>[^:\n]+\.py):(?P<line>\d+)(?::(?P<column>\d+))?:\s*(?:(?P<level>error|warning):\s*)?(?P<message>.+)$",
    re.MULTILINE,
)
PYTEST_RE = re.compile(r"^(?P<file>[^:\n]+\.py):(?P<line>\d+):\s+in\s+", re.MULTILINE)


def discover_validation_commands(root: Path) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []
    python = sys.executable
    if (root / "tests").is_dir() or any(root.glob("test_*.py")):
        commands.append((python, "-m", "pytest", "-q"))

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").is_file() else ""
    if "[tool.ruff" in pyproject and shutil.which("ruff"):
        commands.append((shutil.which("ruff") or "ruff", "check", "."))
    if ("[tool.mypy" in pyproject or (root / "mypy.ini").is_file()) and shutil.which("mypy"):
        commands.append((shutil.which("mypy") or "mypy", "."))
    if ("[tool.pyright" in pyproject or (root / "pyrightconfig.json").is_file()) and shutil.which("pyright"):
        commands.append((shutil.which("pyright") or "pyright",))
    return commands


def parse_validation_issues(output: str, root: Path) -> list[MigrationIssue]:
    issues: list[MigrationIssue] = []
    seen: set[tuple[str, int, str]] = set()
    for match in DIAGNOSTIC_RE.finditer(output):
        raw_path = match.group("file")
        try:
            file = Path(raw_path).resolve().relative_to(root.resolve()).as_posix() if Path(raw_path).is_absolute() else raw_path
        except ValueError:
            file = raw_path
        line = int(match.group("line"))
        message = match.group("message").strip()
        key = (file, line, message)
        if key in seen:
            continue
        seen.add(key)
        issues.append(MigrationIssue(
            "validation_error", message,
            SourceLocation(file, line, int(match.group("column") or 0)),
            RiskLevel.HIGH if match.group("level") == "error" else RiskLevel.MEDIUM,
            "validation.feedback",
        ))
    for match in PYTEST_RE.finditer(output):
        key = (match.group("file"), int(match.group("line")), "pytest traceback")
        if key not in seen:
            seen.add(key)
            issues.append(MigrationIssue(
                "test_failure", "pytest traceback points to this location",
                SourceLocation(key[0], key[1]), RiskLevel.HIGH, "validation.pytest",
            ))
    return issues


def run_validation_command(
    command: tuple[str, ...], root: Path, timeout_seconds: int,
) -> ValidationRun:
    started = time.monotonic()
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        issues = parse_validation_issues(output, root)
        if completed.returncode != 0 and not issues:
            issues.append(MigrationIssue(
                "validation_failed",
                f"Validation command exited with status {completed.returncode}; inspect captured output.",
                SourceLocation("."), RiskLevel.HIGH, "validation.command",
            ))
        return ValidationRun(
            command,
            completed.returncode,
            time.monotonic() - started,
            completed.stdout[-50_000:],
            completed.stderr[-50_000:],
            False,
            issues,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return ValidationRun(
            command, 124, time.monotonic() - started, stdout[-50_000:], stderr[-50_000:], True,
            [MigrationIssue(
                "validation_timeout", f"Command exceeded {timeout_seconds}s timeout.",
                SourceLocation("."), RiskLevel.HIGH, "validation.timeout",
            )],
        )
    except OSError as exc:
        return ValidationRun(
            command, 127, time.monotonic() - started, "", str(exc), False,
            [MigrationIssue(
                "validation_unavailable", f"Could not start validation command: {exc}",
                SourceLocation("."), RiskLevel.HIGH, "validation.command",
            )],
        )


__all__ = ["discover_validation_commands", "parse_validation_issues", "run_validation_command"]
