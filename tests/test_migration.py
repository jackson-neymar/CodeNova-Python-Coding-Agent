from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from codenova.migration.engine import MigrationEngine, MigrationError
from codenova.migration.cli import main as migration_cli
from codenova.migration.benchmark import build_summary, load_benchmark_run, render_markdown
from codenova.migration.models import MigrationOptions, RiskLevel
from codenova.migration.rules import PydanticV2RuleEngine, find_pydantic_v1_residuals
from codenova.migration.rules import update_dependency_manifest
from codenova.migration.scanner import scan_project
from codenova.migration.validation import parse_validation_issues


MODEL_SOURCE = '''from pydantic import BaseModel, Field, parse_obj_as, validator


class User(BaseModel):
    name: str = Field(regex="^[a-z]+$", min_items=1)

    class Config:
        orm_mode = True
        allow_mutation = False

    @validator("name", pre=True)
    def clean_name(cls, value):
        return value.strip()

    def payload(self):
        return self.dict()


users = parse_obj_as(list[User], [{"name": "alice"}])
'''


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\ndependencies = ["pydantic>=1.10,<2"]\n',
        encoding="utf-8",
    )
    (tmp_path / "models.py").write_text(MODEL_SOURCE, encoding="utf-8")
    (tmp_path / "api.py").write_text("from models import User\n\ndef build() -> User:\n    return User.parse_obj({'name': 'bob'})\n", encoding="utf-8")
    return tmp_path


def test_pydantic_rule_engine_preserves_valid_python(tmp_path: Path) -> None:
    root = _project(tmp_path)
    info = next(info for info in scan_project(root).files if info.relative_path == "models.py")

    transformed = PydanticV2RuleEngine().transform(info)

    ast.parse(transformed.source)
    assert "from pydantic import ConfigDict" in transformed.source
    assert "field_validator" in transformed.source
    assert "TypeAdapter" in transformed.source
    assert 'Field(pattern="^[a-z]+$", min_length=1)' in transformed.source
    assert "model_config = ConfigDict(from_attributes=True, frozen=True)" in transformed.source
    assert '@field_validator("name", mode="before")' in transformed.source
    assert "self.model_dump()" in transformed.source
    assert "TypeAdapter(list[User]).validate_python" in transformed.source
    namespace: dict[str, object] = {}
    exec(compile(transformed.source, "models.py", "exec"), namespace)
    assert namespace["users"]


def test_dry_run_is_non_destructive_and_writes_report(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = MigrationEngine(MigrationOptions(
        root, "pydantic", "1.10", "2.8", dry_run=True,
        fetch_docs=False, run_validation=False, run_id="dry-run",
    )).run()

    assert (root / "models.py").read_text(encoding="utf-8") == MODEL_SOURCE
    assert "pydantic>=1.10,<2" in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert result.status == "dry-run"
    assert result.report_path and result.report_path.is_file()
    assert result.changes
    assert (root / ".codenova/migrations/dry-run/semantic-tasks.json").is_file()
    patch = (root / ".codenova/migrations/dry-run/changes.patch").read_text(encoding="utf-8")
    assert "model_config = ConfigDict" in patch
    assert "pydantic>=2.8,<3" in patch


def test_apply_validate_graph_and_rollback(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = MigrationEngine(MigrationOptions(
        root, "pydantic", "1.10", "2.8", fetch_docs=False,
        validation_commands=[(sys.executable, "-c", "import ast; ast.parse(open('models.py').read())")],
        run_id="apply",
    )).run()

    assert result.status == "completed"
    assert result.validations[0].passed
    assert "pydantic>=2.8" in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "model_config = ConfigDict" in (root / "models.py").read_text(encoding="utf-8")
    assert "User.model_validate" in (root / "api.py").read_text(encoding="utf-8")
    model_node = next(node for node in result.graph if node.node_id == "models.py:User")
    assert "api.py" in model_node.referenced_by
    assert (root / ".codenova/migrations/apply/backups/models.py").read_text(encoding="utf-8") == MODEL_SOURCE
    result_json = json.loads((root / ".codenova/migrations/apply/result.json").read_text(encoding="utf-8"))
    assert result_json["report_path"]


def test_resume_rejects_dry_run_checkpoint(tmp_path: Path) -> None:
    root = _project(tmp_path)
    MigrationEngine(MigrationOptions(
        root, "pydantic", "1", "2", dry_run=True,
        fetch_docs=False, run_validation=False, run_id="plan",
    )).run()

    try:
        MigrationEngine(MigrationOptions(
            root, "pydantic", "1", "2", resume=True,
            fetch_docs=False, run_validation=False, run_id="plan",
        )).run()
    except MigrationError as exc:
        assert "dry-run checkpoint" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("dry-run resume should fail")


def test_pre_root_validator_is_migrated_but_post_variant_is_queued(tmp_path: Path) -> None:
    root = _project(tmp_path)
    source = "from pydantic import BaseModel, root_validator\nclass M(BaseModel):\n @root_validator(pre=True)\n def check(cls, values): return values\n"
    path = root / "models.py"
    path.write_text(source, encoding="utf-8")
    info = next(info for info in scan_project(root).files if info.relative_path == "models.py")

    transformed = PydanticV2RuleEngine().transform(info)

    assert "root_validator" not in transformed.source
    assert '@model_validator(mode="before")' in transformed.source
    assert not transformed.issues
    namespace: dict[str, object] = {}
    exec(compile(transformed.source, "models.py", "exec"), namespace)
    namespace["M"]()

    path.write_text(
        "from pydantic import BaseModel, root_validator\n"
        "class M(BaseModel):\n @root_validator\n def check(cls, values): return values\n",
        encoding="utf-8",
    )
    post = PydanticV2RuleEngine().transform(scan_project(root).files[1])
    assert "root_validator" in post.source
    assert any(issue.issue_type == "semantic_validator" for issue in post.issues)


def test_validation_output_becomes_structured_tasks(tmp_path: Path) -> None:
    issues = parse_validation_issues("models.py:12:5: error: incompatible type\n", tmp_path)
    assert issues[0].issue_type == "validation_error"
    assert issues[0].location.file == "models.py"
    assert issues[0].location.line == 12


def test_semantic_handler_edits_are_backed_up_and_recorded(tmp_path: Path) -> None:
    root = _project(tmp_path)
    original = "from pydantic import BaseModel, root_validator\nclass M(BaseModel):\n @root_validator(pre=True)\n def check(cls, values): return values\n"
    (root / "models.py").write_text(original, encoding="utf-8")

    def semantic_handler(prompt: str) -> None:
        assert "semantic-input.json" in prompt
        (root / "models.py").write_text(
            original.replace("root_validator", "model_validator").replace("pre=True", 'mode="before"'),
            encoding="utf-8",
        )
        (root / "migration_helper.py").write_text("MIGRATED = True\n", encoding="utf-8")

    result = MigrationEngine(
        MigrationOptions(
            root, "pydantic", "1", "2", fetch_docs=False,
            run_validation=False, run_id="semantic",
        ),
        semantic_handler=semantic_handler,
    ).run()

    assert any(change.rule_id == "semantic.agent" for change in result.changes)
    assert not any(issue.issue_type == "semantic_validator" for issue in result.issues)
    assert (root / ".codenova/migrations/semantic/backups/models.py").read_text(encoding="utf-8") == original
    assert (root / "migration_helper.py").is_file()
    assert migration_cli(["--project", str(root), "--rollback", "semantic"]) == 0
    assert not (root / "migration_helper.py").exists()
    assert (root / "models.py").read_text(encoding="utf-8") == original


def test_cli_apply_and_per_file_rollback(tmp_path: Path) -> None:
    root = _project(tmp_path)
    exit_code = migration_cli([
        "--package", "pydantic", "--from", "1.10", "--to", "2.8",
        "--project", str(root), "--offline", "--no-validate", "--run-id", "cli",
    ])
    assert exit_code == 0
    assert "model_config" in (root / "models.py").read_text(encoding="utf-8")

    exit_code = migration_cli([
        "--project", str(root), "--rollback", "cli", "--file", "models.py",
    ])
    assert exit_code == 0
    assert (root / "models.py").read_text(encoding="utf-8") == MODEL_SOURCE
    assert "pydantic>=2.8,<3" in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_scanner_resolves_relative_imports_and_basemodel_alias(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(
        "from pydantic import BaseModel as BM\nclass Parent(BM): pass\nclass Child(Parent): pass\n",
        encoding="utf-8",
    )
    (package / "api.py").write_text("from .models import Child\nvalue = Child.parse_obj({})\n", encoding="utf-8")

    scan = scan_project(tmp_path)

    model = next(node for node in scan.graph if node.node_id == "pkg/models.py:Child")
    assert "pkg/api.py" in model.referenced_by


def test_scanner_skips_arbitrarily_named_virtualenv(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    environment = tmp_path / ".benchmark-python"
    (environment / "lib/site-packages").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (environment / "lib/site-packages/vendor.py").write_text("VALUE = 2\n", encoding="utf-8")

    scan = scan_project(tmp_path)

    assert [info.relative_path for info in scan.files] == ["app.py"]


def test_scanner_handles_import_shadowed_by_non_model_definition(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text(
        "from source import helper\ndef helper(): pass\n",
        encoding="utf-8",
    )

    scan = scan_project(tmp_path)

    consumer = next(node for node in scan.graph if node.node_id == "consumer.py")
    assert "source.py:helper" in consumer.depends_on
    assert any(node.node_id == "consumer.py:helper" for node in scan.graph)


def test_poetry_constraint_and_from_version_guard(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[tool.poetry.dependencies]\npydantic = "^1.10"\n', encoding="utf-8")
    transformed = update_dependency_manifest(manifest, tmp_path, "pydantic", "1.10", "2.8")
    assert 'pydantic = ">=2.8,<3"' in transformed.source

    manifest.write_text('[tool.poetry.dependencies]\npydantic = "^3.0"\n', encoding="utf-8")
    guarded = update_dependency_manifest(manifest, tmp_path, "pydantic", "1.10", "2.8")
    assert guarded.source == manifest.read_text(encoding="utf-8")
    assert guarded.version_mismatches == ("^3.0",)


def test_poetry_dependency_subtable_constraint(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[tool.poetry.dependencies.pydantic]\nextras = ["dotenv"]\nversion = "^1.10.9"\n\n'
        '[tool.poetry.dependencies.requests]\nversion = "^2.0"\n',
        encoding="utf-8",
    )

    transformed = update_dependency_manifest(manifest, tmp_path, "pydantic", "1.10", "2.8")

    assert 'version = ">=2.8,<3"' in transformed.source
    assert 'version = "^2.0"' in transformed.source
    assert len(transformed.changes) == 1


def test_v1_validator_signature_is_queued_instead_of_rewritten(tmp_path: Path) -> None:
    source = '''from pydantic import BaseModel, validator

class M(BaseModel):
    x: int
    y: int

    @validator("y")
    def check_y(cls, value, values):
        return value + values.get("x", 0)
'''
    (tmp_path / "models.py").write_text(source, encoding="utf-8")
    info = scan_project(tmp_path).files[0]

    transformed = PydanticV2RuleEngine().transform(info)

    assert transformed.source == source
    assert any(issue.issue_type == "semantic_validator" for issue in transformed.issues)


def test_added_import_preserves_shebang_and_encoding_cookie(tmp_path: Path) -> None:
    source = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pydantic import BaseModel

class M(BaseModel):
    class Config:
        orm_mode = True
'''
    (tmp_path / "models.py").write_text(source, encoding="utf-8")
    info = scan_project(tmp_path).files[0]

    transformed = PydanticV2RuleEngine().transform(info)

    lines = transformed.source.splitlines()
    assert lines[:3] == [
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
        "from pydantic import ConfigDict",
    ]


def test_optional_defaults_and_generic_base_order_preserve_v1_behavior(tmp_path: Path) -> None:
    source = '''from typing import Generic, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")

class Parent(BaseModel):
    pass

class M(Generic[T], Parent):
    old_optional: Optional[str]
    union_optional: str | None
    required: str
'''
    (tmp_path / "models.py").write_text(source, encoding="utf-8")
    info = scan_project(tmp_path).files[0]

    transformed = PydanticV2RuleEngine().transform(info)

    assert "class M(Parent, Generic[T]):" in transformed.source
    assert "old_optional: Optional[str] = None" in transformed.source
    assert "union_optional: str | None = None" in transformed.source
    assert "required: str\n" in transformed.source


def test_base_settings_moves_package_config_and_dependency(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "settings-app"\nversion = "0.1"\n'
        'dependencies = ["pydantic>=1.10,<2"]\n',
        encoding="utf-8",
    )
    (tmp_path / "settings.py").write_text(
        "from pydantic import BaseSettings, Field\n\n"
        "class Settings(BaseSettings):\n"
        "    token: str = Field(default='x')\n"
        "    class Config:\n"
        "        '''Environment configuration.'''\n"
        "        env_file = '.env'\n",
        encoding="utf-8",
    )

    result = MigrationEngine(MigrationOptions(
        tmp_path, "pydantic", "1.10", "2.8", fetch_docs=False,
        run_validation=False, run_id="base-settings",
    )).run()

    migrated = (tmp_path / "settings.py").read_text(encoding="utf-8")
    manifest = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "from pydantic_settings import BaseSettings" in migrated
    assert "SettingsConfigDict(env_file='.env')" in migrated
    assert "pydantic-settings>=2,<3" in manifest
    assert result.status == "completed"
    assert result.succeeded


def test_local_from_orm_is_migrated_only_with_ready_config(tmp_path: Path) -> None:
    source = '''from pydantic import BaseModel

class User(BaseModel):
    name: str
    class Config:
        """ORM compatibility."""
        orm_mode = True

value = User.from_orm(type("Record", (), {"name": "Ada"})())
'''
    (tmp_path / "models.py").write_text(source, encoding="utf-8")
    transformed = PydanticV2RuleEngine().transform(scan_project(tmp_path).files[0])

    assert "User.model_validate(type" in transformed.source
    assert "from_attributes=True" in transformed.source
    assert not any(issue.issue_type == "semantic_from_orm" for issue in transformed.issues)
    namespace: dict[str, object] = {"__name__": "models"}
    exec(compile(transformed.source, "models.py", "exec"), namespace)
    assert namespace["value"].name == "Ada"


def test_high_risk_issue_sets_needs_review_and_cli_failure(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="blocked"\nversion="0.1"\ndependencies=["pydantic>=1,<2"]\n',
        encoding="utf-8",
    )
    (tmp_path / "models.py").write_text(
        "from pydantic import BaseModel, root_validator\n"
        "class M(BaseModel):\n"
        " @root_validator\n"
        " def check(cls, values): return values\n",
        encoding="utf-8",
    )

    result = MigrationEngine(MigrationOptions(
        tmp_path, "pydantic", "1", "2", fetch_docs=False,
        run_validation=False, run_id="blocked",
    )).run()

    assert result.status == "needs-review"
    assert not result.succeeded
    assert migration_cli([
        "--package", "pydantic", "--from", "1", "--to", "2",
        "--project", str(tmp_path), "--offline", "--no-validate",
        "--run-id", "blocked-cli",
    ]) == 1


def test_validation_feedback_retries_and_reaches_completion(tmp_path: Path) -> None:
    root = _project(tmp_path)
    prompts: list[str] = []

    def semantic_handler(prompt: str) -> None:
        prompts.append(prompt)
        payload = json.loads(
            (root / ".codenova/migrations/retry/semantic-input.json").read_text(encoding="utf-8")
        )
        assert payload["validation_feedback"][0]["returncode"] == 1
        path = root / "models.py"
        path.write_text(path.read_text(encoding="utf-8") + "\nVALIDATION_FIXED = True\n", encoding="utf-8")

    result = MigrationEngine(
        MigrationOptions(
            root, "pydantic", "1.10", "2.8", fetch_docs=False,
            validation_commands=[
                (sys.executable, "-c", "import models; assert models.VALIDATION_FIXED"),
            ],
            run_id="retry", max_agent_iterations=2,
        ),
        semantic_handler=semantic_handler,
    ).run()

    assert len(prompts) == 1
    assert [run.passed for run in result.validations] == [False, True]
    assert result.status == "completed"
    assert result.succeeded
    assert any(change.rule_id == "semantic.agent" for change in result.changes)


def test_validation_feedback_stops_at_agent_iteration_limit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    attempts = 0

    def semantic_handler(prompt: str) -> None:
        nonlocal attempts
        attempts += 1
        assert "validation_feedback" in prompt

    result = MigrationEngine(
        MigrationOptions(
            root, "pydantic", "1.10", "2.8", fetch_docs=False,
            validation_commands=[(sys.executable, "-c", "raise SystemExit(7)")],
            run_id="bounded-retry", max_agent_iterations=2,
        ),
        semantic_handler=semantic_handler,
    ).run()

    assert attempts == 2
    assert len(result.validations) == 3
    assert result.status == "validation-failed"
    assert not result.succeeded


def test_residual_scan_ignores_literals_but_keeps_code_usages(tmp_path: Path) -> None:
    (tmp_path / "legacy.py").write_text(
        "from pydantic.generics import GenericModel\n"
        "from pydantic import BaseModel\n"
        "\n"
        "class Model(BaseModel):\n"
        "    class Config:\n"
        "        json_encoders = {}\n"
        "\n"
        "def load(model):\n"
        "    return model.parse_file('data.json')\n"
        "\n"
        "class Box(GenericModel):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "prose.py").write_text(
        "MESSAGES = ['parse_file APIs', 'GenericModel', 'json_encoders']\n"
        "# parse_file GenericModel json_encoders\n"
        "NOTE = f'call parse_file'\n"
        "def _parse_file():\n"
        "    return None\n",
        encoding="utf-8",
    )
    infos = {info.relative_path: info for info in scan_project(tmp_path).files}

    legacy = find_pydantic_v1_residuals(infos["legacy.py"])
    assert {issue.message for issue in legacy} == {
        "parse_file APIs were removed in Pydantic v2",
        "GenericModel should be replaced with BaseModel and Generic",
        "json_encoders should be reviewed for serializer decorators",
    }
    assert all(
        issue.risk in {RiskLevel.HIGH, RiskLevel.MEDIUM} for issue in legacy
    )

    assert find_pydantic_v1_residuals(infos["prose.py"]) == []


def test_rule_engine_does_not_rewrite_same_named_local_apis(tmp_path: Path) -> None:
    source = "def validator(*args, **kwargs): return lambda fn: fn\ndef Field(**kwargs): return kwargs\n@validator('x', pre=True)\ndef f(): return Field(regex='x')\n"
    path = tmp_path / "local_api.py"
    path.write_text(source, encoding="utf-8")
    info = scan_project(tmp_path).files[0]
    transformed = PydanticV2RuleEngine().transform(info)
    assert transformed.source == source
    assert transformed.changes == ()


def test_migration_emits_agent_usage_and_clearance_metrics(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "blocked.py").write_text(
        "from pydantic import BaseModel, root_validator\n"
        "class M(BaseModel):\n"
        " @root_validator\n"
        " def check(cls, values): return values\n",
        encoding="utf-8",
    )

    def semantic_handler(prompt: str) -> dict[str, int | float]:
        assert "semantic-repair stage" in prompt
        return {"input_tokens": 1200, "output_tokens": 300, "turns": 2, "tool_calls": 4}

    result = MigrationEngine(
        MigrationOptions(
            root, "pydantic", "1.10", "2.8", fetch_docs=False,
            run_validation=False, run_id="measured", max_agent_iterations=1,
            agent_provider="test", agent_model="test-model",
            input_cost_per_million=1.0, output_cost_per_million=2.0,
        ),
        semantic_handler=semantic_handler,
    ).run()

    metrics = result.metrics
    assert metrics.agent_enabled
    assert metrics.agent_iterations == 1
    assert metrics.agent_input_tokens == 1200
    assert metrics.agent_output_tokens == 300
    assert metrics.agent_turns == 2
    assert metrics.agent_tool_calls == 4
    assert metrics.estimated_cost_usd == 0.0018
    assert metrics.pre_agent_open_issues >= 1
    assert metrics.post_agent_open_issues >= 1
    assert metrics.duration_seconds > 0
    stored = json.loads(
        (root / ".codenova/migrations/measured/result.json").read_text(encoding="utf-8")
    )
    assert stored["metrics"]["agent_model"] == "test-model"
    assert "automatic_clearance_percent" in stored["metrics"]


def test_benchmark_aggregates_variants_and_renders_improvement(tmp_path: Path) -> None:
    def write_result(name: str, succeeded: bool, issues: int, cleared: int) -> Path:
        path = tmp_path / name / "result.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "status": "completed" if succeeded else "needs-review",
            "succeeded": succeeded,
            "project_root": "/projects/sample",
            "scanned_files": 10,
            "changes": [],
            "issues": [
                {
                    "risk": "medium" if succeeded else "high",
                    "location": {"file": "models.py"},
                }
                for _ in range(issues)
            ],
            "validations": [{"passed": succeeded, "duration_seconds": 1.0}],
            "metrics": {
                "deterministic_changes": 5,
                "semantic_changes": 2 if name == "hybrid" else 0,
                "changed_files": 3,
                "detected_migration_targets": 10,
                "automatically_cleared_targets": cleared,
                "automatic_clearance_percent": cleared * 10,
                "validation_runs": 1,
                "validation_passed": int(succeeded),
                "validation_pass_rate_percent": 100.0 if succeeded else 0.0,
                "duration_seconds": 2.0,
                "agent_input_tokens": 100 if name == "hybrid" else 0,
                "agent_output_tokens": 20 if name == "hybrid" else 0,
            },
        }), encoding="utf-8")
        return path

    rules = load_benchmark_run(
        write_result("rules", False, 5, 5), case="sample", variant="rules"
    )
    hybrid = load_benchmark_run(
        write_result("hybrid", True, 1, 9), case="sample", variant="hybrid"
    )
    summary = build_summary([rules, hybrid], "rules")

    assert summary["variants"]["hybrid"]["success_rate_percent"] == 100.0
    comparison = summary["comparisons"]["hybrid"]
    assert comparison["success_rate_delta_points"] == 100.0
    assert comparison["open_issue_reduction_percent"] == 80.0
    report = render_markdown(summary, "Test benchmark")
    assert "自动清除率" in report
    assert "+80.00%" in report
