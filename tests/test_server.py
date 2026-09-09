"""The tools as the MCP layer exposes them: schemas, results, and errors."""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from queenscoach.cache import Cached
from queenscoach.server import create_server
from queenscoach.static_gtfs import Schedule
from queenscoach.tools import Dependencies

from .conftest import FixtureFeeds, fixture_deps

TOOL_NAMES = {"find_vehicle", "list_vehicles", "get_arrivals"}


async def _call(deps: Dependencies, name: str, arguments: dict[str, object]) -> CallToolResult:
    """Call a tool, asserting it produced a result rather than an input request."""
    result = await create_server(deps).call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    return result


async def test_all_three_tools_are_registered_read_only(deps: Dependencies) -> None:
    tools = await create_server(deps).list_tools()
    assert {tool.name for tool in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.description
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is True


async def test_get_arrivals_declares_stop_as_its_only_required_argument(
    deps: Dependencies,
) -> None:
    tools = {tool.name: tool for tool in await create_server(deps).list_tools()}
    schema = tools["get_arrivals"].input_schema
    assert schema["required"] == ["stop"]
    assert schema["properties"]["mode"]["anyOf"][0]["enum"] == ["bus", "train"]
    assert "required" not in tools["find_vehicle"].input_schema


async def test_a_tool_call_returns_both_text_and_structured_content(
    deps: Dependencies,
) -> None:
    result = await _call(deps, "find_vehicle", {"vehicle": "2301"})
    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["matches"] == 1
    # The text block must carry the same payload, for clients that ignore structure.
    text = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert text == result.structured_content


async def test_an_out_of_range_argument_is_rejected_before_the_tool_runs(
    deps: Dependencies,
) -> None:
    with pytest.raises(ToolError):
        await create_server(deps).call_tool("list_vehicles", {"limit": 0})
    with pytest.raises(ToolError):
        await create_server(deps).call_tool("get_arrivals", {"stop": ""})
    with pytest.raises(ToolError):
        await create_server(deps).call_tool("find_vehicle", {"mode": "helicopter"})


async def test_a_feed_failure_becomes_a_readable_tool_error() -> None:
    async def load_schedule() -> Cached[Schedule]:
        raise RuntimeError("Request to https://feed.test/GTFS.zip failed: timed out")

    deps = Dependencies(load_schedule=load_schedule, feeds=FixtureFeeds())
    with pytest.raises(ToolError, match=r"CATS feed request failed: .*timed out"):
        await create_server(deps).call_tool("list_vehicles", {})


async def test_tool_output_is_json_serializable() -> None:
    deps = fixture_deps()
    calls: list[tuple[str, dict[str, object]]] = [
        ("find_vehicle", {"route": "501"}),
        ("list_vehicles", {"limit": 5}),
        ("get_arrivals", {"stop": "00015"}),
    ]
    for name, arguments in calls:
        result = await _call(deps, name, arguments)
        json.dumps(result.structured_content)
