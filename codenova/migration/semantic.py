"""Structured handoff from deterministic rules to an LLM coding agent."""

from __future__ import annotations

import json
from pathlib import Path

from codenova.migration.models import (
    GraphNode,
    MigrationIssue,
    MigrationOptions,
    ValidationRun,
)


def build_semantic_prompt(
    options: MigrationOptions,
    issues: list[MigrationIssue],
    graph: list[GraphNode],
    artifact_dir: Path,
    validation_runs: list[ValidationRun] | None = None,
) -> str:
    affected = {issue.location.file for issue in issues if issue.location.file != "."}
    related_nodes = [
        node.to_dict()
        for node in graph
        if node.file in affected or any(ref.removeprefix("file:").split(":", 1)[0] in affected for ref in node.referenced_by)
    ]
    payload = {
        "package": options.package,
        "from_version": options.from_version,
        "to_version": options.to_version,
        "issues": [issue.to_dict() for issue in issues],
        "dependency_graph": related_nodes,
        "validation_feedback": [
            {
                "command": list(run.command),
                "returncode": run.returncode,
                "timed_out": run.timed_out,
                "stdout": run.stdout[-12_000:],
                "stderr": run.stderr[-12_000:],
            }
            for run in (validation_runs or [])
        ],
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    task_path = artifact_dir / "semantic-input.json"
    task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt = f"""You are the semantic-repair stage of a dependency migration.

Target: {options.package} {options.from_version} -> {options.to_version}
Project root: {options.project_root.resolve()}
Structured tasks: {task_path}

Read the structured task file and the affected project files. Resolve only migration-related
semantic issues that deterministic codemods could not safely handle. Preserve business behavior,
keep changes scoped, and use the official migration guide. Run the relevant tests/type checks after
editing. Do not change CodeNova's migration journal, checkpoint, backups, or report artifacts.
When validation_feedback is present, diagnose its exact command output and fix only failures caused
by this migration. Do not hide failures by deleting tests, weakening assertions, or disabling checks.

The task payload is included below for portability:
```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```
"""
    (artifact_dir / "semantic-prompt.md").write_text(prompt, encoding="utf-8")
    return prompt


__all__ = ["build_semantic_prompt"]
