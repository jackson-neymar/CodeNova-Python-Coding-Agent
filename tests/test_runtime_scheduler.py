from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from codenova.runtime.scheduler import ResourceAwareScheduler
from codenova.tools.base import Tool, ToolResult


class _Params(BaseModel):
    file_path: str = ""


class _Tool(Tool):
    description = "scheduler test tool"
    params_model = _Params

    def __init__(
        self,
        name: str,
        category: str,
        *,
        concurrency_safe: bool = True,
    ) -> None:
        self.name = name
        self.category = category  # type: ignore[assignment]
        self.is_concurrency_safe = concurrency_safe

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok")


async def _tracked_runner(
    active: dict[str, int],
    key: str,
    overlaps: list[str],
    delay: float = 0.02,
) -> str:
    active[key] = active.get(key, 0) + 1
    if active[key] > 1:
        overlaps.append(key)
    await asyncio.sleep(delay)
    active[key] -= 1
    return key


@pytest.mark.asyncio
async def test_reads_run_concurrently_and_preserve_result_order(tmp_path: Path) -> None:
    scheduler: ResourceAwareScheduler[str] = ResourceAwareScheduler(str(tmp_path))
    read = _Tool("ReadFile", "read")
    active: dict[str, int] = {}
    overlaps: list[str] = []

    scheduler.submit(
        read,
        {"file_path": "a.py"},
        lambda: _tracked_runner(active, "read", overlaps, 0.04),
    )
    scheduler.submit(
        read,
        {"file_path": "a.py"},
        lambda: _tracked_runner(active, "read", overlaps, 0.01),
    )
    results = await scheduler.collect()

    assert results == ["read", "read"]
    assert overlaps == ["read"]
    assert scheduler.stats.max_parallelism == 2
    assert scheduler.stats.dependency_edges == 0


@pytest.mark.asyncio
async def test_same_file_writes_are_serialized(tmp_path: Path) -> None:
    scheduler: ResourceAwareScheduler[str] = ResourceAwareScheduler(str(tmp_path))
    write = _Tool("WriteFile", "write")
    active: dict[str, int] = {}
    overlaps: list[str] = []

    for _ in range(8):
        scheduler.submit(
            write,
            {"file_path": "state.json"},
            lambda: _tracked_runner(active, "state.json", overlaps, 0.005),
        )
    await scheduler.collect()

    assert overlaps == []
    assert scheduler.stats.max_parallelism == 1
    assert scheduler.stats.conflicted_calls == 7
    assert scheduler.stats.dependency_edges == 7


@pytest.mark.asyncio
async def test_distinct_file_writes_can_run_concurrently(tmp_path: Path) -> None:
    scheduler: ResourceAwareScheduler[str] = ResourceAwareScheduler(str(tmp_path))
    write = _Tool("WriteFile", "write")
    active: dict[str, int] = {}
    overlaps: list[str] = []

    for index in range(4):
        scheduler.submit(
            write,
            {"file_path": f"{index}.txt"},
            lambda: _tracked_runner(active, "all-writes", overlaps),
        )
    await scheduler.collect()

    assert overlaps
    assert scheduler.stats.max_parallelism == 4
    assert scheduler.stats.dependency_edges == 0


@pytest.mark.asyncio
async def test_recursive_read_conflicts_with_nested_write(tmp_path: Path) -> None:
    scheduler: ResourceAwareScheduler[str] = ResourceAwareScheduler(str(tmp_path))
    glob = _Tool("Glob", "read")
    write = _Tool("WriteFile", "write")
    order: list[str] = []

    async def run(label: str) -> str:
        order.append(f"{label}-start")
        await asyncio.sleep(0.01)
        order.append(f"{label}-end")
        return label

    scheduler.submit(glob, {"path": "."}, lambda: run("glob"))
    scheduler.submit(
        write,
        {"file_path": "nested/file.py"},
        lambda: run("write"),
    )
    await scheduler.collect()

    assert order == ["glob-start", "glob-end", "write-start", "write-end"]
    assert scheduler.stats.dependency_edges == 1


@pytest.mark.asyncio
async def test_exclusive_command_waits_for_all_older_calls(tmp_path: Path) -> None:
    scheduler: ResourceAwareScheduler[str] = ResourceAwareScheduler(str(tmp_path))
    read = _Tool("ReadFile", "read")
    bash = _Tool("Bash", "command", concurrency_safe=False)
    order: list[str] = []

    async def run(label: str) -> str:
        order.append(f"{label}-start")
        await asyncio.sleep(0.01)
        order.append(f"{label}-end")
        return label

    scheduler.submit(read, {"file_path": "a"}, lambda: run("read-a"))
    scheduler.submit(read, {"file_path": "b"}, lambda: run("read-b"))
    scheduler.submit(bash, {"command": "test"}, lambda: run("bash"))
    await scheduler.collect()

    assert order.index("bash-start") > order.index("read-a-end")
    assert order.index("bash-start") > order.index("read-b-end")
    assert scheduler.stats.max_parallelism == 2


@pytest.mark.asyncio
async def test_explicitly_safe_commands_remain_parallel(tmp_path: Path) -> None:
    scheduler: ResourceAwareScheduler[str] = ResourceAwareScheduler(str(tmp_path))
    message = _Tool("SendMessage", "command", concurrency_safe=True)
    active: dict[str, int] = {}
    overlaps: list[str] = []

    scheduler.submit(
        message, {}, lambda: _tracked_runner(active, "message", overlaps)
    )
    scheduler.submit(
        message, {}, lambda: _tracked_runner(active, "message", overlaps)
    )
    await scheduler.collect()

    assert overlaps == ["message"]
    assert scheduler.stats.max_parallelism == 2
