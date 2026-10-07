"""Domain models for repository dependency migrations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class SourceLocation:
    file: str
    line: int = 1
    column: int = 0
    symbol: str = ""


@dataclass(frozen=True)
class MigrationChange:
    rule_id: str
    description: str
    location: SourceLocation
    before: str
    after: str
    risk: RiskLevel = RiskLevel.LOW
    deterministic: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationIssue:
    issue_type: str
    message: str
    location: SourceLocation
    risk: RiskLevel = RiskLevel.MEDIUM
    migration_rule: str = ""
    affected_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    kind: str
    file: str
    symbol: str
    depends_on: tuple[str, ...] = ()
    referenced_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationRun:
    command: tuple[str, ...]
    returncode: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    issues: list[MigrationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["passed"] = self.passed
        return result


@dataclass
class MigrationOptions:
    project_root: Path
    package: str
    from_version: str
    to_version: str
    dry_run: bool = False
    resume: bool = False
    fetch_docs: bool = True
    run_validation: bool = True
    validation_commands: list[tuple[str, ...]] = field(default_factory=list)
    timeout_seconds: int = 300
    report_path: Path | None = None
    run_id: str = ""
    max_agent_iterations: int = 2
    agent_provider: str = ""
    agent_model: str = ""
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


@dataclass
class MigrationMetrics:
    """Comparable measurements emitted by every migration run.

    The counters deliberately distinguish deterministic codemods from semantic
    agent edits.  This lets the benchmark collector compare rules-only and
    hybrid runs without scraping the human-readable report.
    """

    duration_seconds: float = 0.0
    stage_durations: dict[str, float] = field(default_factory=dict)
    deterministic_changes: int = 0
    semantic_changes: int = 0
    changed_files: int = 0
    pre_agent_open_issues: int = 0
    post_agent_open_issues: int = 0
    semantic_issues_resolved: int = 0
    semantic_issue_clearance_percent: float = 0.0
    detected_migration_targets: int = 0
    automatically_cleared_targets: int = 0
    automatic_clearance_percent: float = 0.0
    issue_counts_before_agent: dict[str, int] = field(default_factory=dict)
    issue_counts_final: dict[str, int] = field(default_factory=dict)
    validation_runs: int = 0
    validation_passed: int = 0
    validation_pass_rate_percent: float = 0.0
    final_validation_passed: bool | None = None
    agent_enabled: bool = False
    agent_provider: str = ""
    agent_model: str = ""
    agent_iterations: int = 0
    agent_turns: int = 0
    agent_tool_calls: int = 0
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    estimated_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MigrationResult:
    run_id: str
    status: str
    project_root: Path
    changes: list[MigrationChange] = field(default_factory=list)
    issues: list[MigrationIssue] = field(default_factory=list)
    graph: list[GraphNode] = field(default_factory=list)
    validations: list[ValidationRun] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    scanned_files: int = 0
    report_path: Path | None = None
    documentation: list[dict[str, str]] = field(default_factory=list)
    metrics: MigrationMetrics = field(default_factory=MigrationMetrics)

    @property
    def succeeded(self) -> bool:
        return (
            self.status == "completed"
            and not any(issue.risk == RiskLevel.HIGH for issue in self.issues)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "project_root": str(self.project_root),
            "changes": [change.to_dict() for change in self.changes],
            "issues": [issue.to_dict() for issue in self.issues],
            "graph": [node.to_dict() for node in self.graph],
            "validations": [run.to_dict() for run in self.validations],
            "manifests": self.manifests,
            "scanned_files": self.scanned_files,
            "report_path": str(self.report_path) if self.report_path else None,
            "documentation": self.documentation,
            "metrics": self.metrics.to_dict(),
            "succeeded": self.succeeded,
        }
