"""Human-readable and machine-readable migration artifacts."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from pathlib import Path

from codenova.migration.models import MigrationOptions, MigrationResult


def render_report(options: MigrationOptions, result: MigrationResult) -> str:
    risk_counts = Counter(str(change.risk) for change in result.changes)
    risk_counts.update(str(issue.risk) for issue in result.issues)
    validation_passed = sum(run.passed for run in result.validations)
    lines = [
        f"# Migration report: {options.package} {options.from_version} → {options.to_version}",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Status: **{result.status}**",
        f"- Successful: **{str(result.succeeded).lower()}**",
        f"- Project: `{result.project_root}`",
        f"- Python files scanned: {result.scanned_files}",
        f"- Deterministic changes: {result.metrics.deterministic_changes}",
        f"- Semantic-agent changes: {result.metrics.semantic_changes}",
        f"- Open issues: {len(result.issues)}",
        f"- Validation: {validation_passed}/{len(result.validations)} commands passed",
        f"- Risk items: low={risk_counts['low']}, medium={risk_counts['medium']}, high={risk_counts['high']}",
        f"- Total duration: {result.metrics.duration_seconds:.2f}s",
        "",
        "## Quantitative metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Detected migration targets | {result.metrics.detected_migration_targets} |",
        f"| Automatically cleared targets | {result.metrics.automatically_cleared_targets} |",
        f"| Automatic clearance | {result.metrics.automatic_clearance_percent:.2f}% |",
        f"| Pre-agent open issues | {result.metrics.pre_agent_open_issues} |",
        f"| Post-agent open issues | {result.metrics.post_agent_open_issues} |",
        f"| Semantic issue clearance | {result.metrics.semantic_issue_clearance_percent:.2f}% |",
        f"| Changed files | {result.metrics.changed_files} |",
        f"| Agent iterations | {result.metrics.agent_iterations} |",
        f"| Agent input tokens | {result.metrics.agent_input_tokens} |",
        f"| Agent output tokens | {result.metrics.agent_output_tokens} |",
        f"| Validation pass rate | {result.metrics.validation_pass_rate_percent:.2f}% |",
        (
            f"| Estimated API cost | ${result.metrics.estimated_cost_usd:.6f} |"
            if result.metrics.estimated_cost_usd is not None
            else "| Estimated API cost | not configured |"
        ),
        "",
        "## Documentation",
        "",
    ]
    if result.documentation:
        for document in result.documentation:
            status = document.get("status", "unknown")
            lines.append(f"- [{document['title']}]({document['url']}) — {status}")
    else:
        lines.append("- No built-in official documentation source is registered for this package.")

    lines.extend(["", "## Changed call sites", ""])
    if result.changes:
        for change in result.changes:
            location = change.location
            lines.append(
                f"- `{location.file}:{location.line}` [{change.risk}] `{change.rule_id}` — {change.description}"
            )
    else:
        lines.append("- No deterministic edits were applicable.")

    lines.extend(["", "## Semantic review / blockers", ""])
    if result.issues:
        for issue in result.issues:
            location = issue.location
            lines.append(
                f"- `{location.file}:{location.line}` [{issue.risk}] `{issue.issue_type}` — {issue.message}"
            )
    else:
        lines.append("- None detected.")

    lines.extend(["", "## Validation", ""])
    if result.validations:
        for run in result.validations:
            state = "PASS" if run.passed else "FAIL"
            command = shlex.join(run.command)
            lines.append(f"### {state}: `{command}` ({run.duration_seconds:.2f}s)")
            lines.append("")
            output = (run.stdout + "\n" + run.stderr).strip()
            if output:
                lines.extend(["```text", output[-6000:], "```", ""])
    elif options.dry_run:
        lines.append("Validation was intentionally skipped for dry-run because files were not modified.")
    elif not options.run_validation:
        lines.append("Validation was disabled.")
    else:
        lines.append("No supported validation commands were discovered.")

    lines.extend(["", "## Dependency graph", ""])
    interesting = [node for node in result.graph if node.kind == "pydantic_model" or node.depends_on or node.referenced_by]
    if interesting:
        for node in interesting:
            inbound = ", ".join(f"`{item}`" for item in node.referenced_by) or "—"
            outbound = ", ".join(f"`{item}`" for item in node.depends_on) or "—"
            lines.append(f"- `{node.node_id}` ({node.kind})")
            lines.append(f"  - depends on: {outbound}")
            lines.append(f"  - referenced by: {inbound}")
    else:
        lines.append("- No cross-file symbol dependencies were resolved.")

    lines.extend([
        "",
        "## Recovery and handoff",
        "",
        f"Resume an interrupted run with `codenova migrate --package {options.package} --from {options.from_version} --to {options.to_version} --resume {result.run_id}`.",
        "Backups, the hash-chained execution journal, checkpoint, JSON result, and semantic task queue are stored under `.codenova/migrations/<run-id>/`.",
        "The reviewable unified diff is stored as `.codenova/migrations/<run-id>/changes.patch`.",
        "",
    ])
    return "\n".join(lines)


def write_artifacts(
    options: MigrationOptions,
    result: MigrationResult,
    artifact_dir: Path,
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_path = options.report_path or (artifact_dir / "report.md")
    if not report_path.is_absolute():
        report_path = options.project_root / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(options, result), encoding="utf-8")
    (artifact_dir / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    semantic_tasks = [issue.to_dict() for issue in result.issues if str(issue.risk) in {"medium", "high"}]
    (artifact_dir / "semantic-tasks.json").write_text(
        json.dumps(semantic_tasks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


__all__ = ["render_report", "write_artifacts"]
