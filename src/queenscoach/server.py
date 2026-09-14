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
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import SERVER_NAME, SERVER_VERSION
from .resources import (
    CORE_STATIC_TABLES,
    REALTIME_FEEDS,
    STATIC_INDEX_URI,
    STATIC_TABLE_URI_TEMPLATE,
    RealtimeFeed,
    UnknownResourceError,
    realtime_feed,
    realtime_uri,
    static_index,
    static_table,
    static_table_uri,
)
from .static_gtfs import Mode
from .tools import (
    DEFAULT_NEARBY_STOPS,
    MAX_ARRIVALS,
    MAX_RESULTS,
    Dependencies,
    ToolResult,
    get_arrivals,
    list_stops,
    list_vehicles,
)

_logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Live Charlotte Area Transit System (CATS) bus and light rail data. "
    "Use list_vehicles to locate buses and trains, list_stops to find stops and "
    "the routes serving them, and get_arrivals for predicted arrival times. "
    "When the user asks about their current stop, the closest station, or "
    "anything near them, pass their current device location as latitude and "
    "longitude to list_stops or get_arrivals. Route 501 is the LYNX Blue Line "
    "and 510 the CityLYNX Gold Line. Coordinates are WGS84 decimal degrees and "
    "times are ISO 8601 UTC. The raw GTFS tables and decoded realtime feeds are "
    "also available as gtfs:// resources."
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
Latitude = Annotated[
    float,
    Field(ge=-90, le=90, description="Latitude of the user's location, e.g. 35.2271."),
]
Longitude = Annotated[
    float,
    Field(ge=-180, le=180, description="Longitude of the user's location, e.g. -80.8431."),
]


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


async def _read(uri: str, result: Awaitable[str]) -> str:
    """Await a resource read, reporting a feed failure the way :func:`_respond` does."""
    try:
        return await result
    except UnknownResourceError as error:
        raise ResourceNotFoundError(str(error)) from error
    except Exception as error:
        _logger.warning("resource %s failed: %s", uri, error)
        raise ResourceError(f"CATS feed request failed: {error}") from error


def create_server(
    deps: Dependencies,
    *,
    auth: AuthSettings | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
) -> MCPServer:
    """Build the MCP server with the transit tools and GTFS resources registered.

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
        name="list_vehicles",
        title="Find buses and trains",
        description=(
            'Current GPS positions of CATS buses and trains in service. Give "vehicle" to '
            'locate one bus or train by its number (e.g. "2301"), "route" for every vehicle '
            'on a route (e.g. "9", "501", "Blue Line"), "mode" for buses or trains, or nothing '
            "for the whole system. Includes heading, speed, occupancy, headsign, and the next "
            "stop when available."
        ),
        annotations=_READ_ONLY,
    )
    async def _list_vehicles(
        vehicle: VehicleQuery | None = None,
        route: RouteQuery | None = None,
        mode: ModeFilter | None = None,
        limit: Annotated[
            int | None,
            Field(ge=1, le=MAX_RESULTS, description="Maximum vehicles to return."),
        ] = None,
    ) -> ToolResult:
        return await _respond(
            "list_vehicles",
            list_vehicles(deps, vehicle=vehicle, route=route, mode=mode, limit=limit),
        )

    @server.tool(
        name="list_stops",
        title="Find stops and stations",
        description=(
            "CATS stops and stations with the bus routes and light rail lines serving each "
            '(501 is the LYNX Blue Line, 510 the CityLYNX Gold Line). For "closest station" '
            'or "near me" questions, pass the user\'s current device location as latitude and '
            'longitude; results are then nearest first with metersAway. Narrow with "mode" '
            '(e.g. train for the nearest rail station), "route", or "query" (part of a name). '
            "Same-named platforms close together are returned as one station. The complete raw "
            "stop table is the gtfs://static/stops.txt resource."
        ),
        annotations=_READ_ONLY,
    )
    async def _list_stops(
        latitude: Latitude | None = None,
        longitude: Longitude | None = None,
        query: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=128,
                description='Stop id, stop code, or part of a stop name, e.g. "Tryon".',
            ),
        ] = None,
        route: Annotated[
            str | None,
            Field(min_length=1, max_length=64, description="Only stops this route serves."),
        ] = None,
        mode: Annotated[
            Mode | None, Field(description="Only stops with bus or train service.")
        ] = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=MAX_RESULTS,
                description=(
                    f"Maximum stations to return (default {DEFAULT_NEARBY_STOPS} with a "
                    f"location, {MAX_RESULTS} without)."
                ),
            ),
        ] = None,
    ) -> ToolResult:
        return await _respond(
            "list_stops",
            list_stops(
                deps,
                latitude=latitude,
                longitude=longitude,
                query=query,
                route=route,
                mode=mode,
                limit=limit,
            ),
        )

    @server.tool(
        name="get_arrivals",
        title="Get arrival times at a stop",
        description=(
            'Estimated arrival times of buses or trains at one stop or station. Give "stop" '
            '(a stop id, stop code, or part of a stop name), or, for "my stop" / "near me" '
            "questions, the user's current device location as latitude and longitude to use "
            'the nearest station. With a location, "mode" or "route" picks the nearest stop '
            "that service actually calls at, so asking for trains finds the nearest rail "
            "station rather than a closer bus stop. Reports minutes away, schedule deviation, "
            "the vehicle number, and any service alerts."
        ),
        annotations=_READ_ONLY,
    )
    async def _get_arrivals(
        stop: StopQuery | None = None,
        latitude: Latitude | None = None,
        longitude: Longitude | None = None,
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
            "get_arrivals",
            get_arrivals(
                deps,
                stop=stop,
                latitude=latitude,
                longitude=longitude,
                route=route,
                mode=mode,
                limit=limit,
            ),
        )

    _register_resources(server, deps)
    return server


def _register_resources(server: MCPServer, deps: Dependencies) -> None:
    @server.resource(
        STATIC_INDEX_URI,
        name="gtfs-static",
        title="CATS static GTFS files",
        description=(
            "The files in the CATS static GTFS archive, with their sizes and resource URIs, "
            "and when the archive was fetched."
        ),
        mime_type="application/json",
    )
    async def _static_index() -> str:
        return await _read(STATIC_INDEX_URI, static_index(deps))

    @server.resource(
        STATIC_TABLE_URI_TEMPLATE,
        name="gtfs-static-table",
        title="CATS static GTFS table",
        description=(
            "One table from the CATS static GTFS archive as published, e.g. agency.txt or "
            f"calendar_dates.txt. {STATIC_INDEX_URI} lists them."
        ),
        mime_type="text/csv",
    )
    async def _static_table(file: str) -> str:
        return await _read(static_table_uri(file), static_table(deps, file))

    for table in CORE_STATIC_TABLES:
        _register_static_table(server, deps, table)
    for feed in REALTIME_FEEDS:
        _register_realtime_feed(server, deps, feed)


def _register_static_table(server: MCPServer, deps: Dependencies, table: str) -> None:
    uri = static_table_uri(table)

    @server.resource(
        uri,
        name=f"gtfs-static-{table.removesuffix('.txt').replace('_', '-')}",
        title=f"CATS GTFS {table}",
        description=f"The {table} table of the CATS static GTFS archive, as CSV.",
        mime_type="text/csv",
    )
    async def _read_table() -> str:
        return await _read(uri, static_table(deps, table))


def _register_realtime_feed(server: MCPServer, deps: Dependencies, feed: RealtimeFeed) -> None:
    uri = realtime_uri(feed)

    @server.resource(
        uri,
        name=f"gtfs-realtime-{feed}",
        title=f"CATS GTFS-Realtime {feed.replace('-', ' ')}",
        description=(
            f"The CATS GTFS-Realtime {feed.replace('-', ' ')} feed, decoded to JSON. "
            "Identifiers are raw feed ids and times are Unix seconds."
        ),
        mime_type="application/json",
    )
    async def _read_feed() -> str:
        return await _read(uri, realtime_feed(deps, feed))
