"""MCP server definition: the tool registrations and their argument schemas.

Kept apart from the transport. :mod:`queenscoach.main` binds it to stdio, and
:mod:`queenscoach.http` binds the same definition to Streamable HTTP behind OAuth.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Annotated, Any

from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import SERVER_NAME, SERVER_VERSION
from .static_gtfs import Mode
from .tools import (
    MAX_ARRIVALS,
    MAX_RESULTS,
    Dependencies,
    ToolResult,
    find_vehicle,
    get_arrivals,
    list_vehicles,
)

_logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Live Charlotte Area Transit System (CATS) bus and light rail data. "
    "Use find_vehicle to locate one bus/train or a whole route, list_vehicles "
    "for a system-wide position snapshot, and get_arrivals for predicted "
    "arrival times at a stop. Coordinates are WGS84 decimal degrees and "
    "times are ISO 8601 UTC."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

VehicleQuery = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        description='Vehicle number as shown on the bus or train, e.g. "2301".',
    ),
]
RouteQuery = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        description='Route to locate, e.g. "9", "501", or "Blue Line".',
    ),
]
StopQuery = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description='Stop id, stop code, or part of a stop name, e.g. "02400" or "CTC Station".',
    ),
]
ModeFilter = Annotated[Mode, Field(description="Restrict results to buses or trains.")]


async def _respond(name: str, result: Awaitable[ToolResult]) -> ToolResult:
    """Await a tool coroutine, turning a feed failure into a readable tool error.

    Feed failures are reported as tool errors rather than raised as crashes, so
    the model can retry or explain instead of the call failing opaquely. Only
    the message is surfaced; tracebacks stay in the server log.
    """
    try:
        payload: ToolResult = await result
    except Exception as error:
        _logger.warning("tool %s failed: %s", name, error)
        raise ToolError(f"CATS feed request failed: {error}") from error
    return payload


def create_server(
    deps: Dependencies,
    *,
    auth: AuthSettings | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
) -> MCPServer:
    """Build the MCP server with the three CATS tools registered.

    Args:
        deps: Feed and schedule loaders the tools read through.
        auth: OAuth settings; only the HTTP transport passes these. Left unset,
            the server is unauthenticated, which is what stdio wants - the
            client already owns the process.
        auth_server_provider: The authorization server backing ``auth``.
    """
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        auth=auth,
        auth_server_provider=auth_server_provider,
    )

    @server.tool(
        name="find_vehicle",
        title="Find a bus or train",
        description=(
            "Locate a specific CATS bus or train and return its current GPS coordinates. "
            'Give "vehicle" for a vehicle number (e.g. "2301"), or "route" to get every '
            'vehicle currently running a route (e.g. "9", "501", "Blue Line"). Includes '
            "heading, speed, occupancy, and next scheduled stop when available."
        ),
        annotations=_READ_ONLY,
    )
    async def _find_vehicle(
        vehicle: VehicleQuery | None = None,
        route: RouteQuery | None = None,
        mode: ModeFilter | None = None,
    ) -> ToolResult:
        return await _respond(
            "find_vehicle", find_vehicle(deps, vehicle=vehicle, route=route, mode=mode)
        )

    @server.tool(
        name="list_vehicles",
        title="List all vehicle positions",
        description=(
            "Return the current GPS coordinates of every CATS bus and train in service. "
            "Optionally filter to buses or trains, or to a single route."
        ),
        annotations=_READ_ONLY,
    )
    async def _list_vehicles(
        mode: ModeFilter | None = None,
        route: Annotated[
            str | None,
            Field(min_length=1, max_length=64, description="Restrict results to one route."),
        ] = None,
        limit: Annotated[
            int | None,
            Field(ge=1, le=MAX_RESULTS, description="Maximum vehicles to return."),
        ] = None,
    ) -> ToolResult:
        return await _respond(
            "list_vehicles", list_vehicles(deps, mode=mode, route=route, limit=limit)
        )

    @server.tool(
        name="get_arrivals",
        title="Get arrival times at a stop",
        description=(
            "Estimated arrival times of buses or trains at a specific stop or station. "
            "Accepts a stop id, stop code, or part of a stop name. Reports minutes away, "
            "schedule deviation, the vehicle number, and any service alerts for that stop."
        ),
        annotations=_READ_ONLY,
    )
    async def _get_arrivals(
        stop: StopQuery,
        route: Annotated[
            str | None,
            Field(min_length=1, max_length=64, description="Only show arrivals for this route."),
        ] = None,
        mode: Annotated[Mode | None, Field(description="Only show bus or train arrivals.")] = None,
        limit: Annotated[
            int | None, Field(ge=1, le=MAX_ARRIVALS, description="Maximum arrivals to return.")
        ] = None,
    ) -> ToolResult:
        return await _respond(
            "get_arrivals", get_arrivals(deps, stop=stop, route=route, mode=mode, limit=limit)
        )

    return server
