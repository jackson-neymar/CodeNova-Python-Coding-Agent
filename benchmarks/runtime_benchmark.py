"""Reproducible microbenchmarks for the durable, resource-aware runtime.

Run from the repository root:

    .venv/bin/python benchmarks/runtime_benchmark.py

The workload is intentionally synthetic so it is deterministic and free of API
costs.  Results are emitted as JSON and label their scope explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codenova.runtime import ExecutionJournal, ResourceAwareScheduler, RuntimeEventType
from codenova.tools.base import Tool, ToolResult


class _Params(BaseModel):
    file_path: str = ""


class _BenchTool(Tool):
    description = "Synthetic benchmark tool"
    params_model = _Params

    def __init__(self, name: str, category: str, concurrency_safe: bool = True) -> None:
        self.name = name
        self.category = category  # type: ignore[assignment]
        self.is_concurrency_safe = concurrency_safe

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok")


async def _measure_parallel_io(width: int, delay_ms: int, repeats: int) -> dict:
    read = _BenchTool("ReadFile", "read")
    sequential_samples: list[float] = []
    scheduled_samples: list[float] = []
    observed_parallelism: list[int] = []

    async def io_task() -> int:
        await asyncio.sleep(delay_ms / 1000)
        return 1

    for _ in range(repeats):
        start = time.perf_counter()
        for _index in range(width):
            await io_task()
        sequential_samples.append(time.perf_counter() - start)

        scheduler: ResourceAwareScheduler[int] = ResourceAwareScheduler(".")
        start = time.perf_counter()
        for index in range(width):
            scheduler.submit(
                read,
                {"file_path": f"fixture-{index}.txt"},
                io_task,
            )
        await scheduler.collect()
        scheduled_samples.append(time.perf_counter() - start)
        observed_parallelism.append(scheduler.stats.max_parallelism)

    sequential = statistics.median(sequential_samples)
    scheduled = statistics.median(scheduled_samples)
    return {
        "workload": "synthetic_io",
        "width": width,
        "delay_ms_per_call": delay_ms,
        "repeats": repeats,
        "sequential_median_ms": round(sequential * 1000, 3),
        "scheduled_median_ms": round(scheduled * 1000, 3),
        "speedup": round(sequential / scheduled, 2),
        "latency_reduction_percent": round((1 - scheduled / sequential) * 100, 2),
        "max_parallelism": max(observed_parallelism),
    }


async def _measure_conflict_safety(attempts: int) -> dict:
    write = _BenchTool("WriteFile", "write")
    scheduler: ResourceAwareScheduler[int] = ResourceAwareScheduler(".")
    active = 0
    violations = 0

    async def conflicting_write() -> int:
        nonlocal active, violations
        active += 1
        if active > 1:
            violations += 1
        await asyncio.sleep(0.001)
        active -= 1
        return 1

    for _ in range(attempts):
        scheduler.submit(
            write,
            {"file_path": "shared-state.json"},
            conflicting_write,
        )
    await scheduler.collect()
    return {
        "workload": "same_resource_writes",
        "attempts": attempts,
        "overlap_violations": violations,
        "conflicted_calls": scheduler.stats.conflicted_calls,
        "dependency_edges": scheduler.stats.dependency_edges,
        "max_parallelism": scheduler.stats.max_parallelism,
    }


def _measure_crash_recovery(trials: int) -> dict:
    recovered = 0
    detected_tampering = 0
    with tempfile.TemporaryDirectory(prefix="codenova-runtime-bench-") as temp:
        root = Path(temp)
        for index in range(trials):
            path = root / f"trial-{index}.jsonl"
            journal = ExecutionJournal(path)
            journal.append(RuntimeEventType.RUN_STARTED, payload={"trial": index})
            journal.append(
                RuntimeEventType.TOOL_SUCCEEDED,
                tool_call_id=f"call-{index}",
                idempotency_key=f"call-{index}",
                payload={"output": f"result-{index}", "is_error": False},
            )
            # Simulate a process dying halfway through the next append.
            with path.open("ab") as handle:
                handle.write(b'{"sequence":3,"event_type":"tool_started"')
            try:
                reopened = ExecutionJournal(path)
                result = reopened.get_committed_tool_result(f"call-{index}")
                if result and result["output"] == f"result-{index}":
                    recovered += 1
            except Exception:
                pass

            # A complete but modified record must not be mistaken for a crash tail.
            tampered_path = root / f"tampered-{index}.jsonl"
            tampered = ExecutionJournal(tampered_path)
            tampered.append(RuntimeEventType.RUN_STARTED, payload={"trial": index})
            raw = json.loads(tampered_path.read_text(encoding="utf-8"))
            raw["payload"]["trial"] = -1
            tampered_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
            try:
                ExecutionJournal(tampered_path)
            except Exception:
                detected_tampering += 1

    return {
        "workload": "truncated_wal_and_tamper_detection",
        "trials": trials,
        "recovered": recovered,
        "recovery_rate_percent": round(recovered / trials * 100, 2),
        "tampering_detected": detected_tampering,
        "tamper_detection_rate_percent": round(
            detected_tampering / trials * 100, 2
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=8)
    parser.add_argument("--delay-ms", type=int, default=25)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--conflict-attempts", type=int, default=100)
    parser.add_argument("--recovery-trials", type=int, default=100)
    args = parser.parse_args()

    result = {
        "benchmark": "codenova_resource_runtime_v1",
        "scope": "deterministic synthetic microbenchmark; no LLM calls",
        "parallel_io": await _measure_parallel_io(
            args.width, args.delay_ms, args.repeats
        ),
        "conflict_safety": await _measure_conflict_safety(args.conflict_attempts),
        "crash_recovery": _measure_crash_recovery(args.recovery_trials),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
