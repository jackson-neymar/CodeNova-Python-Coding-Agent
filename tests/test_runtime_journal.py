from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from codenova.agent import Agent
from codenova.client import LLMClient
from codenova.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from codenova.runtime import ExecutionJournal, JournalIntegrityError, RuntimeEventType
from codenova.tools import ToolRegistry
from codenova.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)


class _NoopClient(LLMClient):
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if False:
            yield  # pragma: no cover


class _Params(BaseModel):
    value: str = ""


class _CountingTool(Tool):
    name = "CountingTool"
    description = "A side-effecting counter used to verify idempotency"
    params_model = _Params
    category = "command"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, params: BaseModel) -> ToolResult:
        self.calls += 1
        return ToolResult(output=f"effect-{self.calls}")


class _ScriptedClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self.responses = responses
        self.index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        response = self.responses[self.index]
        self.index += 1
        for event in response:
            yield event


def test_hash_chain_roundtrip_and_replay(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "events.jsonl")
    journal.append(RuntimeEventType.RUN_STARTED, payload={"n": 1})
    journal.append(RuntimeEventType.TURN_STARTED, payload={"n": 2})

    reopened = ExecutionJournal(tmp_path / "events.jsonl")
    events = reopened.verify()

    assert [event.sequence for event in events] == [1, 2]
    assert events[1].prev_hash == events[0].event_hash
    total = reopened.replay(
        lambda state, event: state + int(event.payload["n"]),
        0,
    )
    assert total == 3


def test_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    journal = ExecutionJournal(path)
    journal.append(RuntimeEventType.RUN_STARTED, payload={"safe": True})

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["safe"] = False
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(JournalIntegrityError, match="hash mismatch"):
        ExecutionJournal(path)


def test_truncated_crash_tail_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    journal = ExecutionJournal(path)
    journal.append(RuntimeEventType.RUN_STARTED)
    with path.open("ab") as handle:
        handle.write(b'{"sequence":2,"event_type":"tool_started"')

    recovered = ExecutionJournal(path)
    assert recovered.last_sequence == 1
    assert len(recovered.events()) == 1
    assert path.read_bytes().endswith(b"\n")
    recovered.append(RuntimeEventType.TURN_STARTED)
    assert recovered.verify()[-1].sequence == 2


def test_checkpoint_is_atomic_and_bound_to_journal_hash(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "events.jsonl", fsync=True)
    head = journal.append(RuntimeEventType.RUN_STARTED)
    checkpoint_path = journal.write_checkpoint({"cursor": 7}, trace_id="trace-1")

    assert checkpoint_path.exists()
    assert journal.load_checkpoint() == {"cursor": 7}
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["sequence"] == head.sequence
    assert checkpoint["event_hash"] == head.event_hash
    assert journal.events()[-1].event_type == RuntimeEventType.CHECKPOINT_CREATED


def test_checkpoint_state_tampering_is_detected(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "events.jsonl")
    journal.append(RuntimeEventType.RUN_STARTED)
    checkpoint_path = journal.write_checkpoint({"cursor": 7})
    raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    raw["state"]["cursor"] = 999
    checkpoint_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(JournalIntegrityError, match="state hash mismatch"):
        journal.load_checkpoint()


def test_large_tool_result_uses_verified_content_addressed_blob(tmp_path: Path) -> None:
    journal = ExecutionJournal(
        tmp_path / "events.jsonl", inline_result_limit=8
    )
    output = "large-result-内容"
    journal.append(
        RuntimeEventType.TOOL_SUCCEEDED,
        tool_call_id="large-1",
        idempotency_key="large-1",
        payload={"output": output, "is_error": False},
    )

    event = journal.events()[0]
    assert "output" not in event.payload
    assert event.payload["output_blob"]["chars"] == len(output)
    assert journal.get_committed_tool_result("large-1")["output"] == output

    digest = event.payload["output_blob"]["sha256"]
    (journal.blob_dir / f"{digest}.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(JournalIntegrityError, match="blob hash mismatch"):
        journal.get_committed_tool_result("large-1")


@pytest.mark.asyncio
async def test_tool_result_is_effectively_once_by_idempotency_key(tmp_path: Path) -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    journal = ExecutionJournal(tmp_path / "events.jsonl")
    agent = Agent(
        _NoopClient(),
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        runtime_journal=journal,
    )
    call = ToolCallComplete("call-42", tool.name, {"value": "x"})

    first = await agent._execute_single_tool_direct(call)
    second = await agent._execute_single_tool_direct(call)

    assert tool.calls == 1
    assert first.result.output == second.result.output == "effect-1"
    assert second.elapsed == 0.0
    assert any(
        event.event_type == RuntimeEventType.TOOL_DEDUPLICATED
        for event in journal.events()
    )


def test_agent_checkpoint_restores_nested_conversation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    journal = ExecutionJournal(tmp_path / "events.jsonl")
    agent = Agent(
        _NoopClient(),
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        runtime_journal=journal,
    )
    agent.session_id = "session-1"
    agent.total_input_tokens = 123
    source = ConversationManager()
    source.add_user_message("do work")
    source.add_assistant_message(
        "working",
        tool_uses=[ToolUseBlock("call-1", "ReadFile", {"file_path": "a.py"})],
    )
    source.add_tool_results_message([ToolResultBlock("call-1", "contents")])
    assert agent.checkpoint(source) is not None

    restored_agent = Agent(
        _NoopClient(),
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        runtime_journal=ExecutionJournal(tmp_path / "events.jsonl"),
    )
    target = ConversationManager()

    assert restored_agent.restore_from_checkpoint(target)
    assert restored_agent.total_input_tokens == 123
    assert target.history[1].tool_uses[0].tool_name == "ReadFile"
    assert target.history[2].tool_results[0].content == "contents"


@pytest.mark.asyncio
async def test_agent_run_emits_durable_lifecycle_and_checkpoint(tmp_path: Path) -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    journal = ExecutionJournal(tmp_path / "events.jsonl")
    client = _ScriptedClient([
        [
            ToolCallComplete("call-1", tool.name, {"value": "x"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=5),
        ],
        [
            TextDelta("done"),
            StreamEnd("end_turn", input_tokens=20, output_tokens=4),
        ],
    ])
    agent = Agent(
        client,
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        runtime_journal=journal,
    )
    conversation = ConversationManager()
    conversation.add_user_message("run the tool")

    events = [event async for event in agent.run(conversation)]
    event_types = [event.event_type for event in journal.verify()]

    assert events
    assert tool.calls == 1
    assert RuntimeEventType.RUN_STARTED in event_types
    assert RuntimeEventType.TOOL_REQUESTED in event_types
    assert RuntimeEventType.TOOL_SUCCEEDED in event_types
    assert RuntimeEventType.RUN_COMPLETED in event_types
    assert journal.load_checkpoint() is not None
