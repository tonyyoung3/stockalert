from __future__ import annotations

import argparse
import json
import sys

from harness.agent import run_agent
from harness.models import OpenAICompatModel
from harness.tools import default_tools, dump_tool_result, execute_tool


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m harness",
        description="Read-only agent harness for stockalert alerts and performance.",
    )
    parser.add_argument("question", nargs="?", help="Ask the agent (needs OPENAI_API_KEY).")
    parser.add_argument("--tool", help="Call one tool directly, no model.")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Argument for --tool. Repeatable.",
    )
    parser.add_argument("--trace", action="store_true", help="Print tool calls after the answer.")
    parser.add_argument("--list-tools", action="store_true", help="Show available tools and exit.")
    return parser.parse_args(argv)


def _parse_tool_args(pairs: list[str]) -> dict:
    args: dict = {}
    for raw in pairs:
        if "=" not in raw:
            raise SystemExit(f"Expected KEY=VALUE, got {raw!r}")
        key, value = raw.split("=", 1)
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    return args


def _print_trace(result) -> None:
    for step in result.steps:
        if step.kind != "tool":
            continue
        print(f"→ {step.tool_name}({json.dumps(step.tool_args or {}, ensure_ascii=False)})")
        print(f"  {dump_tool_result(step.tool_result or {})}")


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = _parse_args(argv)
    tools = default_tools()

    if args.list_tools:
        for tool in tools:
            print(f"{tool.name}\t{tool.description}")
        return 0

    if args.tool:
        result = execute_tool(args.tool, _parse_tool_args(args.arg), tools)
        print(dump_tool_result(result))
        return 1 if "error" in result else 0

    if not args.question:
        print("Pass a question, or use --tool / --list-tools.", file=sys.stderr)
        return 2

    result = run_agent(args.question, OpenAICompatModel.from_env(), tools=tools)
    print(result.answer)
    if args.trace:
        print()
        _print_trace(result)
    return 0 if result.stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
