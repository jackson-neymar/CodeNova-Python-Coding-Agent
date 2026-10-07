"""End-to-end dependency migration orchestration."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

from codenova.migration.docs import document_links, fetch_official_documents
from codenova.migration.models import (
    MigrationChange,
    MigrationIssue,
    MigrationMetrics,
    MigrationOptions,
    MigrationResult,
    RiskLevel,
    SourceLocation,
    ValidationRun,
)
from codenova.migration.report import write_artifacts
from codenova.migration.semantic import build_semantic_prompt
from codenova.migration.rules import (
    PydanticV2RuleEngine,
    ensure_dependency_manifest,
    find_pydantic_v1_residuals,
    update_dependency_manifest,
)
from codenova.migration.scanner import PythonFileInfo, scan_project
from codenova.migration.validation import discover_validation_commands, run_validation_command
from codenova.runtime import ExecutionJournal


ProgressCallback = Callable[[str], None]
SemanticHandler = Callable[[str], Mapping[str, Any] | None]


def _major(version: str) -> int | None:
    match = re.search(r"\d+", version)
    return int(match.group()) if match else None


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")[:100]


def _checked_id(value: str) -> str:
    safe = _safe_id(value)
    if not safe:
        raise MigrationError("migration run id must contain a letter or number")
    return safe


def _change_from_dict(raw: dict) -> MigrationChange:
    location = SourceLocation(**raw["location"])
    return MigrationChange(
        raw["rule_id"], raw["description"], location, raw["before"], raw["after"],
        RiskLevel(raw.get("risk", "low")), bool(raw.get("deterministic", True)),
    )


def _issue_from_dict(raw: dict) -> MigrationIssue:
    return MigrationIssue(
        raw["issue_type"], raw["message"], SourceLocation(**raw["location"]),
        RiskLevel(raw.get("risk", "medium")), raw.get("migration_rule", ""),
        tuple(raw.get("affected_files", ())),
    )


def _attach_affected_files(
    issues: list[MigrationIssue], graph: list,
) -> list[MigrationIssue]:
    consumers: dict[str, set[str]] = {}
    for node in graph:
        if not node.referenced_by:
            continue
        for reference in node.referenced_by:
            reference_file = reference.removeprefix("file:").split(":", 1)[0]
            consumers.setdefault(node.file, set()).add(reference_file)
    attached: list[MigrationIssue] = []
    for issue in issues:
        affected = set(issue.affected_files)
        affected.update(consumers.get(issue.location.file, ()))
        affected.discard(issue.location.file)
        attached.append(MigrationIssue(
            issue.issue_type, issue.message, issue.location, issue.risk,
            issue.migration_rule, tuple(sorted(affected)),
        ))
    return attached


class MigrationError(RuntimeError):
    pass


@dataclass
class _RunPaths:
    root: Path
    journal: Path
    backups: Path


class MigrationEngine:
    """Hybrid migration engine: deterministic first, semantic tasks second."""

    def __init__(
        self,
        options: MigrationOptions,
        progress: ProgressCallback | None = None,
        semantic_handler: SemanticHandler | None = None,
    ) -> None:
        self.options = options
        self.root = options.project_root.resolve()
        self.progress = progress or (lambda message: None)
        self.semantic_handler = semantic_handler
        self._planned_contents: dict[str, tuple[str, str]] = {}
        self.run_id = self._resolve_run_id()
        artifact_root = self.root / ".codenova" / "migrations" / self.run_id
        self.paths = _RunPaths(artifact_root, artifact_root / "journal.jsonl", artifact_root / "backups")
        self.journal = ExecutionJournal(self.paths.journal)

    def _resolve_run_id(self) -> str:
        if self.options.resume:
            if self.options.run_id:
                return _checked_id(self.options.run_id)
            latest = self.root / ".codenova" / "migrations" / "latest"
            if latest.is_file():
                value = latest.read_text(encoding="utf-8").strip()
                if value:
                    return _checked_id(value)
            raise MigrationError("--resume requires a run id when no latest migration exists")
        if self.options.run_id:
            return _checked_id(self.options.run_id)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:6]
        nonce = time.time_ns() % 1_000_000
        return _checked_id(f"{self.options.package}-{self.options.from_version}-to-{self.options.to_version}-{timestamp}-{nonce:06d}-{digest}")

    def _validate_options(self) -> None:
        if not self.root.is_dir():
            raise MigrationError(f"Project directory does not exist: {self.root}")
        if not self.options.package.strip():
            raise MigrationError("package must not be empty")
        if not self.options.from_version.strip() or not self.options.to_version.strip():
            raise MigrationError("from/to versions must not be empty")
        if self.options.package.lower() == "pydantic":
            if _major(self.options.from_version) != 1 or _major(self.options.to_version) != 2:
                raise MigrationError("the built-in Pydantic codemod supports only v1 -> v2 migrations")

    def _checkpoint(
        self,
        stage: str,
        changes: list[MigrationChange],
        issues: list[MigrationIssue],
        completed_files: set[str],
    ) -> None:
        self.journal.write_checkpoint({
            "run_id": self.run_id,
            "package": self.options.package,
            "from_version": self.options.from_version,
            "to_version": self.options.to_version,
            "dry_run": self.options.dry_run,
            "stage": stage,
            "completed_files": sorted(completed_files),
            "changes": [change.to_dict() for change in changes],
            "issues": [issue.to_dict() for issue in issues],
        }, trace_id=self.run_id)

    def _restore(self) -> tuple[list[MigrationChange], list[MigrationIssue], set[str]]:
        if not self.options.resume:
            return [], [], set()
        state = self.journal.load_checkpoint()
        if not state:
            return [], [], set()
        expected = (self.options.package, self.options.from_version, self.options.to_version)
        actual = (state.get("package"), state.get("from_version"), state.get("to_version"))
        if actual != expected:
            raise MigrationError(f"checkpoint targets {actual}, not {expected}")
        if state.get("dry_run"):
            raise MigrationError("a dry-run checkpoint cannot be resumed as an applying migration; start a new run")
        return (
            [_change_from_dict(raw) for raw in state.get("changes", [])],
            [_issue_from_dict(raw) for raw in state.get("issues", [])],
            set(state.get("completed_files", [])),
        )

    def _backup_and_write(self, relative: str, content: str) -> None:
        destination = (self.root / relative).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise MigrationError(f"refusing to write outside project: {destination}") from exc
        backup = self.paths.backups / relative
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        temp = destination.with_name(f".{destination.name}.codenova-{os.getpid()}.tmp")
        try:
            temp.write_text(content, encoding="utf-8")
            os.replace(temp, destination)
        finally:
            if temp.exists():
                temp.unlink()

    def _invoke_semantic_handler(
        self,
        pending: list[MigrationIssue],
        graph: list,
        changes: list[MigrationChange],
        candidate_files: list[Path],
        validation_runs: list[ValidationRun] | None = None,
    ) -> dict[str, int | float]:
        if self.semantic_handler is None or not pending:
            return {}
        snapshots: dict[str, str] = {}
        for path in candidate_files:
            if not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.resolve().relative_to(self.root).as_posix()
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            snapshots[relative] = source
            backup = self.paths.backups / relative
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)

        prompt = build_semantic_prompt(
            self.options, pending, graph, self.paths.root,
            validation_runs=validation_runs,
        )
        self.journal.append(
            "migration_semantic_agent_started", trace_id=self.run_id,
            payload={"issues": len(pending), "files": len(snapshots)},
        )
        agent_started = time.perf_counter()
        raw_stats = self.semantic_handler(prompt)
        wall_duration = time.perf_counter() - agent_started
        stats: dict[str, int | float] = {"duration_seconds": wall_duration}
        if isinstance(raw_stats, Mapping):
            for key in ("input_tokens", "output_tokens", "turns", "tool_calls"):
                value = raw_stats.get(key, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    stats[key] = max(int(value), 0)
        changed_files: list[str] = []
        for relative, before in snapshots.items():
            path = self.root / relative
            try:
                after = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                changed_files.append(relative)
                changes.append(MigrationChange(
                    "semantic.agent", "LLM semantic repair deleted or made this file unreadable.",
                    SourceLocation(relative),
                    f"sha256:{hashlib.sha256(before.encode()).hexdigest()}", "<missing>",
                    RiskLevel.HIGH, False,
                ))
                continue
            if after == before:
                continue
            changed_files.append(relative)
            changes.append(MigrationChange(
                "semantic.agent", "LLM semantic repair changed this file.",
                SourceLocation(relative),
                f"sha256:{hashlib.sha256(before.encode()).hexdigest()}",
                f"sha256:{hashlib.sha256(after.encode()).hexdigest()}",
                RiskLevel.HIGH, False,
            ))
        after_scan = scan_project(self.root)
        after_candidates = [info.path for info in after_scan.files] + after_scan.manifests
        created: list[str] = []
        for path in after_candidates:
            try:
                relative = path.resolve().relative_to(self.root).as_posix()
                if relative in snapshots or not path.is_file() or path.is_symlink():
                    continue
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            created.append(relative)
            changed_files.append(relative)
            changes.append(MigrationChange(
                "semantic.agent", "LLM semantic repair created this file.",
                SourceLocation(relative), "<absent>",
                f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
                RiskLevel.HIGH, False,
            ))
        if created:
            created_path = self.paths.root / "created-files.json"
            existing: list[str] = []
            if created_path.is_file():
                try:
                    raw = json.loads(created_path.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        existing = [str(item) for item in raw]
                except (OSError, json.JSONDecodeError):
                    pass
            created_path.write_text(
                json.dumps(sorted(set(existing) | set(created)), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        self.journal.append(
            "migration_semantic_agent_completed", trace_id=self.run_id,
            payload={"changed_files": changed_files, "usage": stats},
        )
        return stats

    def _write_patch(self) -> Path:
        """Write a git-compatible unified diff for review or application."""
        pairs = dict(self._planned_contents)
        if self.paths.backups.is_dir():
            for backup in self.paths.backups.rglob("*"):
                if not backup.is_file():
                    continue
                relative = backup.relative_to(self.paths.backups).as_posix()
                try:
                    before = backup.read_text(encoding="utf-8")
                    destination = self.root / relative
                    after = destination.read_text(encoding="utf-8") if destination.is_file() else ""
                except (OSError, UnicodeDecodeError):
                    continue
                pairs[relative] = (before, after)
        created_path = self.paths.root / "created-files.json"
        if created_path.is_file():
            try:
                created = json.loads(created_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                created = []
            if isinstance(created, list):
                for value in created:
                    relative = str(value)
                    destination = self.root / relative
                    if destination.is_file():
                        try:
                            pairs[relative] = ("", destination.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError):
                            pass
        chunks: list[str] = []
        for relative, (before, after) in sorted(pairs.items()):
            if before == after:
                continue
            chunks.extend(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{relative}", tofile=f"b/{relative}",
            ))
        patch_path = self.paths.root / "changes.patch"
        patch_path.write_text("".join(chunks), encoding="utf-8")
        return patch_path

    def run(self) -> MigrationResult:
        run_started = time.perf_counter()
        stage_durations: dict[str, float] = {}
        agent_totals = {
            "duration_seconds": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "turns": 0,
            "tool_calls": 0,
        }

        def record_agent_stats(stats: Mapping[str, int | float]) -> None:
            agent_totals["duration_seconds"] += float(stats.get("duration_seconds", 0.0))
            for key in ("input_tokens", "output_tokens", "turns", "tool_calls"):
                agent_totals[key] += int(stats.get(key, 0))

        self._validate_options()
        self.paths.root.mkdir(parents=True, exist_ok=True)
        latest = self.root / ".codenova" / "migrations" / "latest"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(self.run_id + "\n", encoding="utf-8")
        changes, issues, completed_files = self._restore()
        seen_changes = {(change.rule_id, change.location.file, change.location.line, change.before) for change in changes}
        seen_issues = {(issue.issue_type, issue.location.file, issue.location.line, issue.message) for issue in issues}

        self.progress("Scanning project and building the migration dependency graph")
        stage_started = time.perf_counter()
        self.journal.append("migration_scan_started", trace_id=self.run_id)
        scan = scan_project(self.root)
        for issue in scan.issues:
            key = (issue.issue_type, issue.location.file, issue.location.line, issue.message)
            if key not in seen_issues:
                issues.append(issue)
                seen_issues.add(key)
        self.journal.append(
            "migration_scan_completed", trace_id=self.run_id,
            payload={"python_files": len(scan.files), "graph_nodes": len(scan.graph), "manifests": len(scan.manifests)},
        )
        stage_durations["scan"] = time.perf_counter() - stage_started

        stage_started = time.perf_counter()
        documentation = (
            fetch_official_documents(self.options.package, self.root / ".codenova" / "migration-docs")
            if self.options.fetch_docs else document_links(self.options.package)
        )
        stage_durations["documentation"] = time.perf_counter() - stage_started

        pydantic_migration = (
            self.options.package.lower() == "pydantic"
            and _major(self.options.from_version) == 1
            and _major(self.options.to_version) == 2
        )
        rule_engine = PydanticV2RuleEngine() if pydantic_migration else None
        if rule_engine is None:
            generic_issue = MigrationIssue(
                "semantic_migration", f"No deterministic API rule pack is registered for {self.options.package}; only dependency constraints can be updated.",
                SourceLocation("."), RiskLevel.HIGH, "generic.semantic-agent",
            )
            key = (generic_issue.issue_type, ".", 1, generic_issue.message)
            if key not in seen_issues:
                issues.append(generic_issue)
                seen_issues.add(key)

        self.progress("Applying deterministic AST-guided codemods")
        stage_started = time.perf_counter()
        if rule_engine:
            # Foundational model files first, then their consumers. Stable sorting
            # keeps checkpoints reproducible when the dependency graph is partial.
            model_files = {node.file for node in scan.graph if node.kind == "pydantic_model"}
            model_modules: dict[str, set[str]] = {}
            for node in scan.graph:
                if node.kind != "pydantic_model":
                    continue
                module = node.file.removesuffix(".py").replace("/", ".")
                if module.endswith(".__init__"):
                    module = module.removesuffix(".__init__")
                model_modules.setdefault(module, set()).add(node.symbol)
            ordered_files = sorted(scan.files, key=lambda info: (info.relative_path not in model_files, info.relative_path))
            for info in ordered_files:
                if info.relative_path in completed_files:
                    continue
                imported_models = {
                    local_name
                    for local_name, qualified in info.imports.items()
                    if any(
                        qualified == f"{module}.{symbol}"
                        for module, symbols in model_modules.items()
                        for symbol in symbols
                    )
                }
                transformed = rule_engine.transform(info, imported_models)
                for change in transformed.changes:
                    key = (change.rule_id, change.location.file, change.location.line, change.before)
                    if key not in seen_changes:
                        changes.append(change)
                        seen_changes.add(key)
                for issue in transformed.issues:
                    key = (issue.issue_type, issue.location.file, issue.location.line, issue.message)
                    if key not in seen_issues:
                        issues.append(issue)
                        seen_issues.add(key)
                if transformed.source != info.source and not self.options.dry_run:
                    self._backup_and_write(info.relative_path, transformed.source)
                if transformed.source != info.source:
                    self._planned_contents[info.relative_path] = (info.source, transformed.source)
                completed_files.add(info.relative_path)
                self.journal.append(
                    "migration_file_completed", trace_id=self.run_id,
                    payload={"file": info.relative_path, "changed": transformed.source != info.source, "dry_run": self.options.dry_run},
                )
                self._checkpoint("codemod", changes, issues, completed_files)

                # Scan the proposed content, including during dry-run, for APIs
                # that need semantic handling.
                try:
                    proposed = PythonFileInfo(
                        info.path, info.relative_path,
                        ast.parse(transformed.source, filename=info.relative_path), transformed.source,
                    )
                    proposed.imports = info.imports
                    proposed.definitions = info.definitions
                    proposed.model_classes = info.model_classes
                    proposed.settings_classes = info.settings_classes
                    for issue in find_pydantic_v1_residuals(proposed):
                        key = (issue.issue_type, issue.location.file, issue.location.line, issue.message)
                        if key not in seen_issues:
                            issues.append(issue)
                            seen_issues.add(key)
                except SyntaxError as exc:
                    issues.append(MigrationIssue(
                        "codemod_syntax_error", f"Proposed transform is invalid Python: {exc}",
                        SourceLocation(info.relative_path, exc.lineno or 1), RiskLevel.HIGH, "codemod.integrity",
                    ))
        stage_durations["codemod"] = time.perf_counter() - stage_started

        self.progress("Updating dependency manifests")
        stage_started = time.perf_counter()
        manifest_change_count = 0
        dependency_found = False
        base_settings_required = any(
            change.rule_id in {"pydantic.base-settings-import", "pydantic.settings-config"}
            for change in changes
        )
        settings_dependency_found = not base_settings_required
        for manifest in scan.manifests:
            relative = manifest.relative_to(self.root).as_posix()
            if manifest.name in {"uv.lock", "poetry.lock", "Pipfile.lock"}:
                continue
            transformed = update_dependency_manifest(
                manifest, self.root, self.options.package, self.options.from_version, self.options.to_version,
            )
            dependency_found = dependency_found or transformed.matched
            manifest_source = transformed.source
            manifest_changes = list(transformed.changes)
            if not settings_dependency_found:
                companion = ensure_dependency_manifest(
                    manifest, self.root, "pydantic-settings", ">=2,<3",
                    source=manifest_source,
                )
                if companion.matched:
                    settings_dependency_found = True
                    manifest_source = companion.source
                    manifest_changes.extend(companion.changes)
            for constraint in transformed.version_mismatches:
                issue = MigrationIssue(
                    "version_mismatch",
                    f"Declared {self.options.package} constraint {constraint!r} does not match --from {self.options.from_version}; it was not modified.",
                    SourceLocation(relative), RiskLevel.HIGH, "dependency.version-guard",
                )
                key = (issue.issue_type, relative, 1, issue.message)
                if key not in seen_issues:
                    issues.append(issue)
                    seen_issues.add(key)
            for change in manifest_changes:
                key = (change.rule_id, change.location.file, change.location.line, change.before)
                if key not in seen_changes:
                    changes.append(change)
                    seen_changes.add(key)
                    manifest_change_count += 1
            original_manifest = manifest.read_text(encoding="utf-8")
            if manifest_source != original_manifest and not self.options.dry_run:
                self._backup_and_write(relative, manifest_source)
            if manifest_source != original_manifest:
                self._planned_contents[relative] = (
                    original_manifest, manifest_source,
                )
        if not dependency_found:
            issue = MigrationIssue(
                "dependency_not_found", f"No editable dependency constraint for {self.options.package} was found.",
                SourceLocation("."), RiskLevel.HIGH, "dependency.constraint",
            )
            key = (issue.issue_type, ".", 1, issue.message)
            if key not in seen_issues:
                issues.append(issue)
                seen_issues.add(key)
        if not settings_dependency_found:
            issue = MigrationIssue(
                "settings_dependency_not_added",
                "BaseSettings was migrated, but no supported manifest could declare pydantic-settings>=2,<3.",
                SourceLocation("."), RiskLevel.HIGH, "dependency.pydantic-settings",
            )
            key = (issue.issue_type, ".", 1, issue.message)
            if key not in seen_issues:
                issues.append(issue)
                seen_issues.add(key)
        lockfiles = [path.name for path in scan.manifests if path.name in {"uv.lock", "poetry.lock", "Pipfile.lock"}]
        if lockfiles and manifest_change_count:
            issue = MigrationIssue(
                "lockfile_refresh", f"Regenerate lock file(s) with the project's package manager: {', '.join(lockfiles)}.",
                SourceLocation(lockfiles[0]), RiskLevel.MEDIUM, "dependency.lockfile",
            )
            key = (issue.issue_type, issue.location.file, 1, issue.message)
            if key not in seen_issues:
                issues.append(issue)
                seen_issues.add(key)
        self._checkpoint("manifest", changes, issues, completed_files)
        stage_durations["manifest"] = time.perf_counter() - stage_started

        semantic_iterations = 0
        pending_semantic = [issue for issue in issues if issue.risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}]
        pre_agent_open_issues = len(pending_semantic)
        issue_counts_before_agent = Counter(str(issue.risk) for issue in pending_semantic)
        if self.semantic_handler and pending_semantic and self.options.max_agent_iterations > 0 and not self.options.dry_run:
            self.progress(f"Routing {len(pending_semantic)} semantic task(s) to the LLM agent")
            record_agent_stats(self._invoke_semantic_handler(
                pending_semantic, scan.graph, changes,
                [info.path for info in scan.files] + scan.manifests,
            ))
            semantic_iterations += 1
            # Replace pre-agent Pydantic findings with a fresh audit so resolved
            # tasks do not remain falsely listed as blockers.
            refreshed = scan_project(self.root)
            retained = [
                issue for issue in issues
                if not issue.migration_rule.startswith("pydantic.")
                and issue.issue_type != "deprecated_api"
            ]
            refreshed_seen = {
                (issue.issue_type, issue.location.file, issue.location.line, issue.message)
                for issue in retained
            }
            for info in refreshed.files:
                audit = rule_engine.transform(info) if rule_engine else None
                audit_issues = list(audit.issues) if audit else []
                residual_info = info
                if audit and audit.source != info.source:
                    self._backup_and_write(info.relative_path, audit.source)
                    for change in audit.changes:
                        key = (change.rule_id, change.location.file, change.location.line, change.before)
                        if key not in seen_changes:
                            changes.append(change)
                            seen_changes.add(key)
                    try:
                        residual_info = PythonFileInfo(
                            info.path, info.relative_path,
                            ast.parse(audit.source, filename=info.relative_path), audit.source,
                            info.imports, info.definitions, info.model_classes,
                            info.settings_classes,
                        )
                    except SyntaxError as exc:
                        audit_issues.append(MigrationIssue(
                            "codemod_syntax_error", f"Post-agent transform is invalid Python: {exc}",
                            SourceLocation(info.relative_path, exc.lineno or 1),
                            RiskLevel.HIGH, "codemod.integrity",
                        ))
                if pydantic_migration:
                    audit_issues.extend(find_pydantic_v1_residuals(residual_info))
                for issue in audit_issues:
                    key = (issue.issue_type, issue.location.file, issue.location.line, issue.message)
                    if key not in refreshed_seen:
                        retained.append(issue)
                        refreshed_seen.add(key)
            issues = retained
            seen_issues = refreshed_seen
            scan = refreshed
            self._checkpoint("semantic-agent", changes, issues, completed_files)

        validations = []
        last_validation_batch = []
        if self.options.run_validation and not self.options.dry_run:
            commands = self.options.validation_commands or discover_validation_commands(self.root)
            while True:
                self.progress(f"Running {len(commands)} validation command(s)")
                last_validation_batch = []
                for command in commands:
                    run = run_validation_command(command, self.root, self.options.timeout_seconds)
                    validations.append(run)
                    last_validation_batch.append(run)
                    for issue in run.issues:
                        key = (issue.issue_type, issue.location.file, issue.location.line, issue.message)
                        if key not in seen_issues:
                            issues.append(issue)
                            seen_issues.add(key)
                    self.journal.append(
                        "migration_validation_completed", trace_id=self.run_id,
                        payload={"command": list(command), "returncode": run.returncode, "passed": run.passed, "duration_seconds": run.duration_seconds},
                    )
                failed_issues = [issue for run in last_validation_batch if not run.passed for issue in run.issues]
                if (
                    not failed_issues or self.semantic_handler is None
                    or semantic_iterations >= self.options.max_agent_iterations
                ):
                    break
                self.progress(f"Routing {len(failed_issues)} validation failure(s) back to the LLM agent")
                refreshed = scan_project(self.root)
                record_agent_stats(self._invoke_semantic_handler(
                    failed_issues, refreshed.graph, changes,
                    [info.path for info in refreshed.files] + refreshed.manifests,
                    validation_runs=[run for run in last_validation_batch if not run.passed],
                ))
                semantic_iterations += 1
                self._checkpoint("validation-repair", changes, issues, completed_files)

        status = "dry-run" if self.options.dry_run else "completed"
        if last_validation_batch and not all(run.passed for run in last_validation_batch):
            status = "validation-failed"
        elif last_validation_batch:
            # Earlier failures are feedback-loop history, not open blockers,
            # once the final validation batch has passed.
            issues = [
                issue for issue in issues
                if issue.issue_type not in {
                    "validation_error", "test_failure", "validation_timeout", "validation_unavailable",
                    "validation_failed",
                }
            ]
        changed_lockfiles = {
            change.location.file
            for change in changes
            if change.rule_id == "semantic.agent"
            and Path(change.location.file).name in {"uv.lock", "poetry.lock", "Pipfile.lock"}
        }
        if changed_lockfiles:
            issues = [
                issue for issue in issues
                if not (issue.issue_type == "lockfile_refresh" and issue.location.file in changed_lockfiles)
            ]
        if status == "completed" and any(issue.risk == RiskLevel.HIGH for issue in issues):
            status = "needs-review"
        issues = _attach_affected_files(issues, scan.graph)
        final_non_validation_issues = [
            issue for issue in issues
            if issue.issue_type not in {
                "validation_error", "test_failure", "validation_timeout",
                "validation_unavailable", "validation_failed",
            }
        ]
        post_agent_open_issues = len(final_non_validation_issues)
        semantic_issues_resolved = max(pre_agent_open_issues - post_agent_open_issues, 0)
        deterministic_changes = sum(change.deterministic for change in changes)
        semantic_changes = len(changes) - deterministic_changes
        detected_targets = deterministic_changes + pre_agent_open_issues
        automatically_cleared = deterministic_changes + semantic_issues_resolved
        validation_passed = sum(run.passed for run in validations)
        cost_configured = bool(
            self.options.input_cost_per_million or self.options.output_cost_per_million
        )
        estimated_cost = None
        if cost_configured:
            estimated_cost = (
                agent_totals["input_tokens"] * self.options.input_cost_per_million
                + agent_totals["output_tokens"] * self.options.output_cost_per_million
            ) / 1_000_000
        metrics = MigrationMetrics(
            duration_seconds=round(time.perf_counter() - run_started, 6),
            stage_durations={
                **{key: round(value, 6) for key, value in stage_durations.items()},
                "semantic_agent": round(float(agent_totals["duration_seconds"]), 6),
                "validation": round(sum(run.duration_seconds for run in validations), 6),
            },
            deterministic_changes=deterministic_changes,
            semantic_changes=semantic_changes,
            changed_files=len({change.location.file for change in changes}),
            pre_agent_open_issues=pre_agent_open_issues,
            post_agent_open_issues=post_agent_open_issues,
            semantic_issues_resolved=semantic_issues_resolved,
            semantic_issue_clearance_percent=round(
                semantic_issues_resolved / pre_agent_open_issues * 100, 2
            ) if pre_agent_open_issues else 100.0,
            detected_migration_targets=detected_targets,
            automatically_cleared_targets=automatically_cleared,
            automatic_clearance_percent=round(
                automatically_cleared / detected_targets * 100, 2
            ) if detected_targets else 100.0,
            issue_counts_before_agent=dict(issue_counts_before_agent),
            issue_counts_final=dict(Counter(str(issue.risk) for issue in issues)),
            validation_runs=len(validations),
            validation_passed=validation_passed,
            validation_pass_rate_percent=round(
                validation_passed / len(validations) * 100, 2
            ) if validations else 0.0,
            final_validation_passed=(
                all(run.passed for run in last_validation_batch)
                if last_validation_batch else None
            ),
            agent_enabled=self.semantic_handler is not None,
            agent_provider=self.options.agent_provider,
            agent_model=self.options.agent_model,
            agent_iterations=semantic_iterations,
            agent_turns=int(agent_totals["turns"]),
            agent_tool_calls=int(agent_totals["tool_calls"]),
            agent_input_tokens=int(agent_totals["input_tokens"]),
            agent_output_tokens=int(agent_totals["output_tokens"]),
            estimated_cost_usd=(round(estimated_cost, 6) if estimated_cost is not None else None),
        )
        result = MigrationResult(
            self.run_id, status, self.root, changes, issues, scan.graph, validations,
            [path.relative_to(self.root).as_posix() for path in scan.manifests], len(scan.files), None,
            documentation, metrics,
        )
        self.progress("Writing migration report and resumable handoff artifacts")
        patch_path = self._write_patch()
        result.report_path = write_artifacts(self.options, result, self.paths.root)
        # Rewrite result.json now that report_path is known.
        (self.paths.root / "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        self.journal.append(
            "migration_completed", trace_id=self.run_id,
            payload={"status": status, "changes": len(changes), "issues": len(issues), "report": str(result.report_path), "patch": str(patch_path)},
        )
        self._checkpoint("complete", changes, issues, completed_files)
        return result


__all__ = ["MigrationEngine", "MigrationError"]
