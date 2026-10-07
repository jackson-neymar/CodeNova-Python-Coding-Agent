"""Tamper-evident event journal and atomic runtime checkpoints.

The journal is deliberately independent from the UI and model protocol.  It is an
append-only JSONL write-ahead log whose records form a SHA-256 hash chain.  A
checkpoint is an optimization only: the journal remains the source of truth and
the checkpoint records the exact event hash it was derived from.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, TypeVar


class RuntimeEventType(StrEnum):
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    TOOL_DEDUPLICATED = "tool_deduplicated"
    CHECKPOINT_CREATED = "checkpoint_created"
    CONTEXT_COMPACTED = "context_compacted"


class JournalIntegrityError(RuntimeError):
    """Raised when an event sequence or its hash chain has been modified."""


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    event_type: str
    timestamp_ns: int
    trace_id: str
    tool_call_id: str
    idempotency_key: str
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str
    version: int = 1

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuntimeEvent":
        return cls(
            sequence=int(raw["sequence"]),
            event_type=str(raw["event_type"]),
            timestamp_ns=int(raw["timestamp_ns"]),
            trace_id=str(raw.get("trace_id", "")),
            tool_call_id=str(raw.get("tool_call_id", "")),
            idempotency_key=str(raw.get("idempotency_key", "")),
            payload=dict(raw.get("payload", {})),
            prev_hash=str(raw.get("prev_hash", "")),
            event_hash=str(raw["event_hash"]),
            version=int(raw.get("version", 1)),
        )


T = TypeVar("T")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _event_hash(raw_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(raw_without_hash).encode("utf-8")).hexdigest()


class ExecutionJournal:
    """Thread-safe append-only runtime journal.

    Completed tool results are indexed by idempotency key.  Reusing a provider
    tool-call id after a crash can therefore return the durable result without
    executing the side effect a second time.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        fsync: bool = False,
        inline_result_limit: int = 10_000,
    ) -> None:
        self.path = Path(path)
        self.checkpoint_path = self.path.with_suffix(".checkpoint.json")
        self.blob_dir = self.path.parent / "blobs"
        self.fsync = fsync
        self.inline_result_limit = inline_result_limit
        self._lock = threading.RLock()
        self._events: list[RuntimeEvent] = []
        self._committed_results: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._repair_crash_tail()
            self._events = self._read_and_verify(allow_truncated_tail=False)
            self._rebuild_indexes()

    @classmethod
    def for_session(
        cls,
        work_dir: str | Path,
        session_id: str,
        *,
        fsync: bool = False,
    ) -> "ExecutionJournal":
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")
        if not safe_id:
            raise ValueError("session_id must contain at least one safe character")
        path = Path(work_dir) / ".codenova" / "runtime" / f"{safe_id}.jsonl"
        return cls(path, fsync=fsync)

    @property
    def last_sequence(self) -> int:
        return self._events[-1].sequence if self._events else 0

    @property
    def last_hash(self) -> str:
        return self._events[-1].event_hash if self._events else ""

    def append(
        self,
        event_type: RuntimeEventType | str,
        *,
        trace_id: str = "",
        tool_call_id: str = "",
        idempotency_key: str = "",
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        with self._lock:
            raw: dict[str, Any] = {
                "sequence": self.last_sequence + 1,
                "event_type": str(event_type),
                "timestamp_ns": time.time_ns(),
                "trace_id": trace_id,
                "tool_call_id": tool_call_id,
                "idempotency_key": idempotency_key,
                "payload": self._externalize_large_result(
                    str(event_type), payload or {}
                ),
                "prev_hash": self.last_hash,
                "version": 1,
            }
            raw["event_hash"] = _event_hash(raw)
            event = RuntimeEvent.from_dict(raw)

            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = _canonical_json(asdict(event)) + "\n"
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())

            self._events.append(event)
            self._index_event(event)
            return event

    def events(self) -> list[RuntimeEvent]:
        with self._lock:
            return list(self._events)

    def verify(self) -> list[RuntimeEvent]:
        with self._lock:
            verified = self._read_and_verify(allow_truncated_tail=False)
            if verified != self._events:
                raise JournalIntegrityError("journal changed outside the active writer")
            return list(verified)

    def replay(self, reducer: Callable[[T, RuntimeEvent], T], initial: T) -> T:
        state = initial
        for event in self.verify():
            state = reducer(state, event)
        return state

    def get_committed_tool_result(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock:
            result = self._committed_results.get(idempotency_key)
            if result is None:
                return None
            resolved = dict(result)
            blob = resolved.get("output_blob")
            if isinstance(blob, dict):
                digest = str(blob.get("sha256", ""))
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise JournalIntegrityError("invalid tool-result blob digest")
                path = self.blob_dir / f"{digest}.txt"
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise JournalIntegrityError(
                        f"missing tool-result blob {digest}"
                    ) from exc
                if hashlib.sha256(data).hexdigest() != digest:
                    raise JournalIntegrityError(
                        f"tool-result blob hash mismatch: {digest}"
                    )
                resolved["output"] = data.decode("utf-8")
            return resolved

    def write_checkpoint(
        self,
        state: dict[str, Any],
        *,
        trace_id: str = "",
    ) -> Path:
        """Atomically persist state tied to the current journal head."""
        with self._lock:
            checkpoint = {
                "version": 1,
                "sequence": self.last_sequence,
                "event_hash": self.last_hash,
                "created_at_ns": time.time_ns(),
                "state": state,
                "state_hash": hashlib.sha256(
                    _canonical_json(state).encode("utf-8")
                ).hexdigest(),
            }
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.checkpoint_path.with_name(
                f".{self.checkpoint_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                with temp_path.open("w", encoding="utf-8") as handle:
                    handle.write(_canonical_json(checkpoint))
                    handle.flush()
                    if self.fsync:
                        os.fsync(handle.fileno())
                os.replace(temp_path, self.checkpoint_path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()

            self.append(
                RuntimeEventType.CHECKPOINT_CREATED,
                trace_id=trace_id,
                payload={
                    "checkpoint_sequence": checkpoint["sequence"],
                    "checkpoint_hash": checkpoint["event_hash"],
                },
            )
            return self.checkpoint_path

    def load_checkpoint(self) -> dict[str, Any] | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            raw = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JournalIntegrityError(f"invalid checkpoint: {exc}") from exc

        sequence = int(raw.get("sequence", -1))
        event_hash = str(raw.get("event_hash", ""))
        if sequence == 0:
            if event_hash:
                raise JournalIntegrityError("empty checkpoint has a non-empty event hash")
        else:
            matching = next((e for e in self._events if e.sequence == sequence), None)
            if matching is None or matching.event_hash != event_hash:
                raise JournalIntegrityError("checkpoint does not match the journal history")
        state = raw.get("state")
        if not isinstance(state, dict):
            raise JournalIntegrityError("checkpoint state must be an object")
        expected_state_hash = hashlib.sha256(
            _canonical_json(state).encode("utf-8")
        ).hexdigest()
        if raw.get("state_hash") != expected_state_hash:
            raise JournalIntegrityError("checkpoint state hash mismatch")
        return dict(state)

    def _repair_crash_tail(self) -> None:
        """Remove only an incomplete final record left by a crashed writer.

        A valid record that merely missed its trailing newline is preserved.  A
        malformed record in the middle is never repaired and will fail normal
        verification, which prevents corruption from being hidden.
        """
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        last_newline = data.rfind(b"\n")
        tail_start = last_newline + 1
        tail = data[tail_start:]
        try:
            json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            repaired = data[:tail_start]
        else:
            repaired = data + b"\n"
        with self.path.open("wb") as handle:
            handle.write(repaired)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())

    def _externalize_large_result(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        copied = dict(payload)
        if event_type not in {
            RuntimeEventType.TOOL_SUCCEEDED.value,
            RuntimeEventType.TOOL_FAILED.value,
        }:
            return copied
        output = copied.get("output")
        if not isinstance(output, str) or len(output) <= self.inline_result_limit:
            return copied

        data = output.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        blob_path = self.blob_dir / f"{digest}.txt"
        try:
            descriptor = os.open(
                str(blob_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            existing = blob_path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise JournalIntegrityError(
                    f"content-addressed blob collision: {digest}"
                )
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())

        copied.pop("output", None)
        copied["output_blob"] = {
            "sha256": digest,
            "chars": len(output),
            "bytes": len(data),
        }
        return copied

    def _read_and_verify(self, *, allow_truncated_tail: bool) -> list[RuntimeEvent]:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []

        lines = data.splitlines(keepends=True)
        events: list[RuntimeEvent] = []
        expected_sequence = 1
        previous_hash = ""
        for index, encoded in enumerate(lines):
            is_last = index == len(lines) - 1
            has_newline = encoded.endswith((b"\n", b"\r"))
            try:
                raw = json.loads(encoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if allow_truncated_tail and is_last and not has_newline:
                    break
                raise JournalIntegrityError(f"invalid event at line {index + 1}: {exc}") from exc

            event = RuntimeEvent.from_dict(raw)
            if event.sequence != expected_sequence:
                raise JournalIntegrityError(
                    f"invalid sequence at line {index + 1}: expected {expected_sequence}, got {event.sequence}"
                )
            if event.prev_hash != previous_hash:
                raise JournalIntegrityError(f"broken hash chain at line {index + 1}")
            hash_input = dict(raw)
            stored_hash = str(hash_input.pop("event_hash", ""))
            if not stored_hash or _event_hash(hash_input) != stored_hash:
                raise JournalIntegrityError(f"event hash mismatch at line {index + 1}")
            events.append(event)
            expected_sequence += 1
            previous_hash = event.event_hash
        return events

    def _rebuild_indexes(self) -> None:
        self._committed_results.clear()
        for event in self._events:
            self._index_event(event)

    def _index_event(self, event: RuntimeEvent) -> None:
        if (
            event.idempotency_key
            and event.event_type
            in {RuntimeEventType.TOOL_SUCCEEDED.value, RuntimeEventType.TOOL_FAILED.value}
        ):
            self._committed_results[event.idempotency_key] = dict(event.payload)
