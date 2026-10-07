"""Resource-aware asynchronous tool scheduler.

Each submitted tool call is converted to a read/write set.  A call waits only for
older calls whose resources conflict with it, producing an implicit dependency
DAG while preserving maximal safe concurrency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, TypeVar

from codenova.tools.base import Tool


@dataclass(frozen=True)
class ResourceRef:
    path: str
    recursive: bool = False


@dataclass(frozen=True)
class ResourceAccess:
    reads: frozenset[ResourceRef] = frozenset()
    writes: frozenset[ResourceRef] = frozenset()
    exclusive: bool = False
    side_effect: str = "none"


@dataclass
class SchedulerStats:
    submitted: int = 0
    dependency_edges: int = 0
    conflicted_calls: int = 0
    max_parallelism: int = 0


T = TypeVar("T")


@dataclass
class _Scheduled(Generic[T]):
    access: ResourceAccess
    task: asyncio.Task[T]
    ancestors: frozenset[asyncio.Task[Any]] = frozenset()


def _normal_path(value: str, work_dir: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(work_dir) / path
    return str(path.resolve(strict=False))


def infer_resource_access(
    tool: Tool | None,
    arguments: dict[str, Any],
    work_dir: str,
) -> ResourceAccess:
    """Infer a conservative resource contract for built-in and MCP tools."""
    if tool is None:
        return ResourceAccess(exclusive=True, side_effect="unknown")

    name = tool.name
    raw_path = arguments.get("file_path") or arguments.get("path")
    ref = (
        ResourceRef(_normal_path(str(raw_path), work_dir))
        if raw_path not in (None, "")
        else None
    )

    if name in {"Glob", "Grep"}:
        base = ref or ResourceRef(_normal_path(".", work_dir))
        return ResourceAccess(reads=frozenset({ResourceRef(base.path, recursive=True)}))
    if name == "ReadFile" and ref:
        return ResourceAccess(reads=frozenset({ref}))
    if tool.category == "write" and ref:
        return ResourceAccess(
            reads=frozenset({ref}) if name == "EditFile" else frozenset(),
            writes=frozenset({ref}),
            side_effect="filesystem",
        )
    if tool.category == "command" and not tool.is_concurrency_safe:
        return ResourceAccess(exclusive=True, side_effect="process")
    if tool.category == "command":
        return ResourceAccess(side_effect="declared_safe")
    if not tool.is_concurrency_safe:
        return ResourceAccess(exclusive=True, side_effect="unknown")
    if ref:
        return ResourceAccess(reads=frozenset({ref}))
    return ResourceAccess()


def _is_same_or_child(path: str, directory: str) -> bool:
    try:
        Path(path).relative_to(directory)
        return True
    except ValueError:
        return False


def _overlaps(left: ResourceRef, right: ResourceRef) -> bool:
    if left.path == right.path:
        return True
    if left.recursive and _is_same_or_child(right.path, left.path):
        return True
    if right.recursive and _is_same_or_child(left.path, right.path):
        return True
    return False


def accesses_conflict(left: ResourceAccess, right: ResourceAccess) -> bool:
    if left.exclusive or right.exclusive:
        return True
    for written in left.writes:
        if any(_overlaps(written, other) for other in right.reads | right.writes):
            return True
    for written in right.writes:
        if any(_overlaps(written, other) for other in left.reads):
            return True
    return False


class ResourceAwareScheduler(Generic[T]):
    """Build an implicit DAG from calls as they arrive from an LLM stream."""

    def __init__(self, work_dir: str = ".") -> None:
        self.work_dir = work_dir
        self.stats = SchedulerStats()
        self._scheduled: list[_Scheduled[T]] = []
        self._active = 0

    def submit(
        self,
        tool: Tool | None,
        arguments: dict[str, Any],
        runner: Callable[[], Awaitable[T]],
    ) -> asyncio.Task[T]:
        access = infer_resource_access(tool, arguments, self.work_dir)
        candidates = [
            item
            for item in self._scheduled
            if not item.task.done() and accesses_conflict(item.access, access)
        ]
        # Keep only the dependency frontier.  If candidate A is already an
        # ancestor of candidate B, waiting for B also waits for A.  This turns
        # repeated writes to one resource from a dense O(n²)-edge graph into a
        # linear chain without weakening ordering.
        dependencies = [
            item.task
            for item in candidates
            if not any(
                item.task in other.ancestors
                for other in candidates
                if other is not item
            )
        ]
        self.stats.submitted += 1
        self.stats.dependency_edges += len(dependencies)
        if dependencies:
            self.stats.conflicted_calls += 1

        async def run_after_dependencies() -> T:
            if dependencies:
                await asyncio.gather(*dependencies, return_exceptions=True)
            self._active += 1
            self.stats.max_parallelism = max(self.stats.max_parallelism, self._active)
            try:
                return await runner()
            finally:
                self._active -= 1

        task = asyncio.create_task(run_after_dependencies())
        ancestors: set[asyncio.Task[Any]] = set(dependencies)
        for item in candidates:
            if item.task in dependencies:
                ancestors.update(item.ancestors)
        self._scheduled.append(
            _Scheduled(access=access, task=task, ancestors=frozenset(ancestors))
        )
        return task

    async def collect(self, *, return_exceptions: bool = False) -> list[T | BaseException]:
        tasks = [item.task for item in self._scheduled]
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks, return_exceptions=return_exceptions))
