"""Aggregate migration artifacts into reproducible benchmark summaries."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class BenchmarkRun:
    case: str
    variant: str
    result_path: str
    metrics_source: str
    status: str
    succeeded: bool
    scanned_files: int
    deterministic_changes: int
    semantic_changes: int
    changed_files: int
    open_issues: int
    high_risk_issues: int
    medium_risk_issues: int
    detected_targets: int
    cleared_targets: int
    automatic_clearance_percent: float
    validation_runs: int
    validation_passed: int
    validation_pass_rate_percent: float
    duration_seconds: float
    agent_iterations: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _result_file(path: Path) -> Path:
    if path.is_file():
        return path
    candidate = path / "result.json"
    if candidate.is_file():
        return candidate
    raise ValueError(f"result.json not found at {path}")


def load_benchmark_run(path: Path, *, case: str = "", variant: str = "") -> BenchmarkRun:
    result_path = _result_file(path).resolve()
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid migration result {result_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"migration result must be an object: {result_path}")

    changes = raw.get("changes", []) if isinstance(raw.get("changes"), list) else []
    issues = raw.get("issues", []) if isinstance(raw.get("issues"), list) else []
    validations = raw.get("validations", []) if isinstance(raw.get("validations"), list) else []
    metrics = raw.get("metrics", {}) if isinstance(raw.get("metrics"), dict) else {}
    deterministic = int(_number(metrics.get("deterministic_changes"), sum(
        1 for change in changes if isinstance(change, dict) and change.get("deterministic", True)
    )))
    semantic = int(_number(metrics.get("semantic_changes"), max(len(changes) - deterministic, 0)))
    validation_passed = int(_number(metrics.get("validation_passed"), sum(
        1 for run in validations if isinstance(run, dict) and run.get("passed") is True
    )))
    validation_runs = int(_number(metrics.get("validation_runs"), len(validations)))
    detected_targets = int(_number(
        metrics.get("detected_migration_targets"), deterministic + len(issues)
    ))
    cleared_targets = int(_number(metrics.get("automatically_cleared_targets"), deterministic))
    project_root = Path(str(raw.get("project_root", "project")))
    risk_counts = defaultdict(int)
    for issue in issues:
        if isinstance(issue, dict):
            risk_counts[str(issue.get("risk", "medium"))] += 1
    status = str(raw.get("status", "unknown"))
    conservatively_successful = (
        status == "completed"
        and risk_counts["high"] == 0
        and (not validations or validation_passed == validation_runs)
    )

    return BenchmarkRun(
        case=case or project_root.name or "project",
        variant=variant or "unknown",
        result_path=str(result_path),
        metrics_source="native" if metrics else "legacy-derived",
        status=status,
        succeeded=bool(raw.get("succeeded", conservatively_successful)) and conservatively_successful,
        scanned_files=int(_number(raw.get("scanned_files"))),
        deterministic_changes=deterministic,
        semantic_changes=semantic,
        changed_files=int(_number(metrics.get("changed_files"), len({
            str(change.get("location", {}).get("file", ""))
            for change in changes if isinstance(change, dict)
        }))),
        open_issues=len(issues),
        high_risk_issues=risk_counts["high"],
        medium_risk_issues=risk_counts["medium"],
        detected_targets=detected_targets,
        cleared_targets=cleared_targets,
        automatic_clearance_percent=_number(
            metrics.get("automatic_clearance_percent"),
            cleared_targets / detected_targets * 100 if detected_targets else 100.0,
        ),
        validation_runs=validation_runs,
        validation_passed=validation_passed,
        validation_pass_rate_percent=_number(
            metrics.get("validation_pass_rate_percent"),
            validation_passed / validation_runs * 100 if validation_runs else 0.0,
        ),
        duration_seconds=_number(metrics.get("duration_seconds"), sum(
            _number(run.get("duration_seconds"))
            for run in validations if isinstance(run, dict)
        )),
        agent_iterations=int(_number(metrics.get("agent_iterations"))),
        input_tokens=int(_number(metrics.get("agent_input_tokens"))),
        output_tokens=int(_number(metrics.get("agent_output_tokens"))),
        estimated_cost_usd=(
            _number(metrics["estimated_cost_usd"])
            if metrics.get("estimated_cost_usd") is not None else None
        ),
    )


def _aggregate(runs: Iterable[BenchmarkRun]) -> dict[str, Any]:
    items = list(runs)
    projects = len(items)
    successes = sum(run.succeeded for run in items)
    detected = sum(run.detected_targets for run in items)
    cleared = sum(run.cleared_targets for run in items)
    validations = sum(run.validation_runs for run in items)
    validation_passed = sum(run.validation_passed for run in items)
    costs = [run.estimated_cost_usd for run in items if run.estimated_cost_usd is not None]
    return {
        "projects": projects,
        "successful_projects": successes,
        "success_rate_percent": round(successes / projects * 100, 2) if projects else 0.0,
        "scanned_files": sum(run.scanned_files for run in items),
        "deterministic_changes": sum(run.deterministic_changes for run in items),
        "semantic_changes": sum(run.semantic_changes for run in items),
        "changed_files": sum(run.changed_files for run in items),
        "open_issues": sum(run.open_issues for run in items),
        "high_risk_issues": sum(run.high_risk_issues for run in items),
        "detected_targets": detected,
        "cleared_targets": cleared,
        "automatic_clearance_percent": round(cleared / detected * 100, 2) if detected else 100.0,
        "validation_runs": validations,
        "validation_passed": validation_passed,
        "validation_pass_rate_percent": round(
            validation_passed / validations * 100, 2
        ) if validations else 0.0,
        "duration_seconds": round(sum(run.duration_seconds for run in items), 3),
        "input_tokens": sum(run.input_tokens for run in items),
        "output_tokens": sum(run.output_tokens for run in items),
        "estimated_cost_usd": round(sum(costs), 6) if costs else None,
    }


def _percent_change(current: float, baseline: float, *, reduction: bool = False) -> float | None:
    if baseline == 0:
        return None
    delta = (current - baseline) / baseline * 100
    return round(-delta if reduction else delta, 2)


def build_summary(runs: list[BenchmarkRun], baseline: str) -> dict[str, Any]:
    grouped: dict[str, list[BenchmarkRun]] = defaultdict(list)
    for run in runs:
        grouped[run.variant].append(run)
    variants = {name: _aggregate(items) for name, items in sorted(grouped.items())}
    comparisons: dict[str, dict[str, float | None]] = {}
    base = variants.get(baseline)
    if base:
        for name, current in variants.items():
            if name == baseline:
                continue
            comparisons[name] = {
                "success_rate_delta_points": round(
                    current["success_rate_percent"] - base["success_rate_percent"], 2
                ),
                "automatic_clearance_delta_points": round(
                    current["automatic_clearance_percent"]
                    - base["automatic_clearance_percent"], 2
                ),
                "open_issue_reduction_percent": _percent_change(
                    current["open_issues"], base["open_issues"], reduction=True
                ),
                "high_risk_issue_reduction_percent": _percent_change(
                    current["high_risk_issues"], base["high_risk_issues"], reduction=True
                ),
                "validation_pass_rate_delta_points": round(
                    current["validation_pass_rate_percent"]
                    - base["validation_pass_rate_percent"], 2
                ),
                "duration_change_percent": _percent_change(
                    current["duration_seconds"], base["duration_seconds"]
                ),
            }
    return {
        "benchmark": "codenova_migration_comparison_v1",
        "generated_on": date.today().isoformat(),
        "baseline": baseline,
        "runs": [asdict(run) for run in runs],
        "variants": variants,
        "comparisons": comparisons,
    }


def render_markdown(summary: dict[str, Any], title: str) -> str:
    lines = [
        f"# {title}", "", f"生成日期：{summary['generated_on']}", "",
        "## 汇总", "",
        "| 方案 | 项目 | 成功率 | 自动清除率 | 修改（规则/Agent） | 遗留问题（高风险） | 验证通过率 | 耗时 | Token（入/出） | 成本 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in summary["variants"].items():
        cost = values["estimated_cost_usd"]
        lines.append(
            f"| {name} | {values['projects']} | {values['success_rate_percent']:.2f}% | "
            f"{values['automatic_clearance_percent']:.2f}% | "
            f"{values['deterministic_changes']}/{values['semantic_changes']} | "
            f"{values['open_issues']} ({values['high_risk_issues']}) | "
            f"{values['validation_pass_rate_percent']:.2f}% | {values['duration_seconds']:.2f}s | "
            f"{values['input_tokens']}/{values['output_tokens']} | "
            f"{'$' + format(cost, '.6f') if cost is not None else '—'} |"
        )
    lines.extend(["", f"## 相对基线：{summary['baseline']}", ""])
    if summary["comparisons"]:
        lines.extend([
            "| 方案 | 成功率提升 | 自动清除率提升 | 遗留问题减少 | 高风险问题减少 | 验证通过率提升 | 耗时变化 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for name, values in summary["comparisons"].items():
            def cell(key: str, suffix: str = "%") -> str:
                value = values[key]
                return "—" if value is None else f"{value:+.2f}{suffix}"
            lines.append(
                f"| {name} | {cell('success_rate_delta_points', 'pp')} | "
                f"{cell('automatic_clearance_delta_points', 'pp')} | "
                f"{cell('open_issue_reduction_percent')} | "
                f"{cell('high_risk_issue_reduction_percent')} | "
                f"{cell('validation_pass_rate_delta_points', 'pp')} | "
                f"{cell('duration_change_percent')} |"
            )
    else:
        lines.append("没有同时包含基线和对照方案，暂时无法计算提升比例。")

    lines.extend(["", "## 逐项目结果", "",
                  "| 项目 | 方案 | 指标来源 | 状态 | 成功 | 文件 | 修改 | 问题 | 自动清除率 | 验证 | 耗时 |",
                  "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for run in summary["runs"]:
        lines.append(
            f"| {run['case']} | {run['variant']} | {run['metrics_source']} | {run['status']} | "
            f"{'是' if run['succeeded'] else '否'} | {run['scanned_files']} | "
            f"{run['deterministic_changes'] + run['semantic_changes']} | {run['open_issues']} | "
            f"{run['automatic_clearance_percent']:.2f}% | "
            f"{run['validation_passed']}/{run['validation_runs']} | {run['duration_seconds']:.2f}s |"
        )
    if any(run["metrics_source"] == "legacy-derived" for run in summary["runs"]):
        lines.extend([
            "",
            "> 注：标记为 `legacy-derived` 的旧产物没有原生 metrics；自动清除率按“修改数 ÷（修改数＋遗留问题数）”保守推导。",
        ])
    lines.append("")
    return "\n".join(lines)


def _parse_run_spec(value: str) -> tuple[str, str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not raw_path:
        raise ValueError("--run must use VARIANT:CASE=PATH")
    variant, case_separator, case = label.partition(":")
    if not variant:
        raise ValueError("--run variant must not be empty")
    return variant, case if case_separator else "", Path(raw_path)


def _manifest_runs(path: Path) -> list[tuple[str, str, Path]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid benchmark manifest {path}: {exc}") from exc
    entries = raw.get("runs", []) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("benchmark manifest must contain a runs list")
    parsed: list[tuple[str, str, Path]] = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not entry.get("variant") or not entry.get("result"):
            raise ValueError(f"benchmark manifest run #{index} needs variant and result")
        result = Path(str(entry["result"]))
        if not result.is_absolute():
            result = path.parent / result
        parsed.append((str(entry["variant"]), str(entry.get("case", "")), result))
    return parsed


def write_summary(summary: dict[str, Any], output_dir: Path, title: str) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "summary.json"
    csv_path = output_dir / "runs.csv"
    markdown_path = output_dir / "report.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = summary["runs"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    markdown_path.write_text(render_markdown(summary, title), encoding="utf-8")
    return json_path, csv_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codenova benchmark",
        description="Aggregate migration result.json artifacts into JSON, CSV, and Markdown comparisons.",
    )
    parser.add_argument(
        "--run", action="append", default=[], metavar="VARIANT:CASE=PATH",
        help="Add one migration result; CASE may be omitted and inferred from project_root",
    )
    parser.add_argument("--manifest", type=Path, help="JSON file containing a runs list")
    parser.add_argument("--baseline", default="rules", help="Variant used as the comparison baseline")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("benchmarks/results/migration-comparison"),
    )
    parser.add_argument("--title", default="CodeNova 依赖迁移量化实验")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    specs: list[tuple[str, str, Path]] = []
    try:
        specs.extend(_parse_run_spec(value) for value in args.run)
        if args.manifest:
            specs.extend(_manifest_runs(args.manifest))
        if not specs:
            parser.error("provide at least one --run or --manifest")
        runs = [
            load_benchmark_run(path, case=case, variant=variant)
            for variant, case, path in specs
        ]
        if len({(run.variant, run.case) for run in runs}) != len(runs):
            raise ValueError("duplicate variant/case pair")
        paths = write_summary(build_summary(runs, args.baseline), args.output_dir, args.title)
    except ValueError as exc:
        parser.error(str(exc))
    print("Benchmark artifacts: " + ", ".join(str(path) for path in paths))
    return 0


__all__ = [
    "BenchmarkRun", "build_parser", "build_summary", "load_benchmark_run",
    "main", "render_markdown", "write_summary",
]
