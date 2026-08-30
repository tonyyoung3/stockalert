from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from harness.models import Model, ModelTurn
from harness.prompt import SYSTEM_PROMPT
from harness.tools import Tool, default_tools, dump_tool_result, execute_tool, openai_tool_schemas


@dataclass
class TraceStep:
    kind: str
    content: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None


@dataclass
class AgentResult:
    answer: str
    steps: list[TraceStep] = field(default_factory=list)
    stop_reason: str = "completed"
    messages: list[dict] = field(default_factory=list)


def _assistant_message(turn: ModelTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.content or None}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in turn.tool_calls
        ]
    return message


def run_agent(
    question: str,
    model: Model,
    tools: list[Tool] | None = None,
    max_steps: int = 8,
    system_prompt: str = SYSTEM_PROMPT,
) -> AgentResult:
    """Think → tool → observe loop. Read-only: tools never write the database."""
    if not (question or "").strip():
        return AgentResult(answer="", stop_reason="empty_question")

    toolset = tools if tools is not None else default_tools()
    schemas = openai_tool_schemas(toolset)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question.strip()},
    ]
    steps: list[TraceStep] = []
    last_text = ""

    for _ in range(max_steps):
        turn = model.complete(messages, schemas)
        messages.append(_assistant_message(turn))

        if turn.tool_calls:
            for call in turn.tool_calls:
                result = execute_tool(call.name, call.arguments, toolset)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": dump_tool_result(result),
                    }
                )
                steps.append(
                    TraceStep(
                        kind="tool",
                        tool_name=call.name,
                        tool_args=call.arguments,
                        tool_result=result,
                    )
                )
            continue

        if turn.content and turn.content.strip():
            last_text = turn.content.strip()
            steps.append(TraceStep(kind="answer", content=last_text))
            return AgentResult(
                answer=last_text,
                steps=steps,
                stop_reason="completed",
                messages=messages,
            )

        return AgentResult(
            answer=last_text,
            steps=steps,
            stop_reason="no_answer",
            messages=messages,
        )

    return AgentResult(
        answer=last_text or "Reached the tool-call limit before a final answer.",
        steps=steps,
        stop_reason="max_steps",
        messages=messages,
    )
