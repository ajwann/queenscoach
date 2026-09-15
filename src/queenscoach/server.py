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
    DEFAULT_DEPARTURES,
    DEFAULT_NEARBY_STOPS,
    DEFAULT_TRANSFERS,
    DEFAULT_WALK_METERS,
    MAX_ARRIVALS,
    MAX_DEPARTURES,
    MAX_RESULTS,
    MAX_TRANSFERS,
    MAX_WALK_METERS,
    Dependencies,
    ToolResult,
    get_arrivals,
    get_route,
    get_schedule,
    get_service_alerts,
    list_stops,
    list_vehicles,
    plan_trip,
)

_logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Charlotte Area Transit System (CATS) bus and light rail: live positions and "
    "predictions, the published timetable, and trip planning. "
    "Use plan_trip to get from one place to another; get_arrivals for live "
    "predictions in the next hour; get_schedule for the timetable at a stop on any "
    "date, including first and last trips; get_route for where a route goes, its "
    "stops, and how often it runs; get_service_alerts for detours and disruptions; "
    "list_stops to find stops and the routes serving them; and list_vehicles to "
    "locate buses and trains. "
    "When the user asks about their current stop, the closest station, or a trip "
    "from where they are, pass their current device location as latitude and "
    "longitude. For a named place such as a venue or address, look up its "
    "coordinates first. Route 501 is the LYNX Blue Line and 510 the CityLYNX Gold "
    "Line. Dates and clock times the user gives are Charlotte local time. "
    "Coordinates are WGS84 decimal degrees; times are ISO 8601 with their UTC "
    "offset. The timetable covers only the dates CATS has published, which each "
    "timetable response states. "
    "CATS's data does not include fares, payment, accessibility, bikes, or stop "
    "amenities: for those, search the web (ridetransit.org) or give the CATS "
    "customer service number, 704-336-7433. "
    "The raw GTFS tables and decoded realtime feeds are also available as gtfs:// "
    "resources."
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
ServiceDate = Annotated[
    str,
    Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Service date in Charlotte, YYYY-MM-DD. Defaults to today.",
    ),
]
ClockTime = Annotated[
    str,
    Field(
        pattern=r"^\d{1,2}:\d{2}$",
        description=(
            'Charlotte local time, 24-hour HH:MM, e.g. "08:30". For after midnight on the '
            'same night, count past 24: "25:15" is 1:15 am.'
        ),
    ),
]
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

    @server.tool(
        name="plan_trip",
        title="Plan a trip",
        description=(
            "Plan a trip on CATS buses and trains between two places, with walking at either "
            "end and between stops. Give the start as origin_stop or origin_latitude and "
            "origin_longitude (use the user's current device location for 'from here'), and "
            "the destination the same way (look up a venue's or address's coordinates first). "
            "Leave now by default, or give depart_at or arrive_by with an optional date. "
            "Returns up to 3 itineraries: each ride's route, where to board and get off, the "
            "vehicle's destination, times including live delays when a vehicle is reporting, "
            "wait at each transfer, and walking legs. Walking is estimated from straight-line "
            "distance."
        ),
        annotations=_READ_ONLY,
    )
    async def _plan_trip(
        origin_stop: Annotated[
            str | None,
            Field(min_length=1, max_length=128, description="Starting stop id, code, or name."),
        ] = None,
        origin_latitude: Latitude | None = None,
        origin_longitude: Longitude | None = None,
        destination_stop: Annotated[
            str | None,
            Field(min_length=1, max_length=128, description="Destination stop id, code, or name."),
        ] = None,
        destination_latitude: Latitude | None = None,
        destination_longitude: Longitude | None = None,
        date: ServiceDate | None = None,
        depart_at: ClockTime | None = None,
        arrive_by: ClockTime | None = None,
        max_transfers: Annotated[
            int | None,
            Field(
                ge=0,
                le=MAX_TRANSFERS,
                description=f"Most vehicle changes allowed (default {DEFAULT_TRANSFERS}).",
            ),
        ] = None,
        max_walk_meters: Annotated[
            int | None,
            Field(
                ge=100,
                le=MAX_WALK_METERS,
                description=(
                    "Longest walk to the first stop or from the last, in meters "
                    f"(default {DEFAULT_WALK_METERS})."
                ),
            ),
        ] = None,
    ) -> ToolResult:
        return await _respond(
            "plan_trip",
            plan_trip(
                deps,
                origin_stop=origin_stop,
                origin_latitude=origin_latitude,
                origin_longitude=origin_longitude,
                destination_stop=destination_stop,
                destination_latitude=destination_latitude,
                destination_longitude=destination_longitude,
                date=date,
                depart_at=depart_at,
                arrive_by=arrive_by,
                max_transfers=max_transfers,
                max_walk_meters=max_walk_meters,
            ),
        )

    @server.tool(
        name="get_schedule",
        title="Get the timetable at a stop",
        description=(
            "The published timetable at one stop or station for a date: scheduled departures "
            "with each trip's route and real destination, and the first and last departure "
            "that day. Use it for 'when is the last train tonight', 'does this bus run on "
            "Sunday', or times later today or on another date; use get_arrivals instead for "
            'live predictions in the next hour. Give "stop", or the user\'s location as '
            "latitude and longitude for the nearest stop serving the route or mode. Times "
            "default to from now (today) or the start of service (other dates)."
        ),
        annotations=_READ_ONLY,
    )
    async def _get_schedule(
        stop: StopQuery | None = None,
        latitude: Latitude | None = None,
        longitude: Longitude | None = None,
        route: Annotated[
            str | None,
            Field(min_length=1, max_length=64, description="Only departures on this route."),
        ] = None,
        mode: Annotated[Mode | None, Field(description="Only bus or train departures.")] = None,
        date: ServiceDate | None = None,
        after: ClockTime | None = None,
        before: ClockTime | None = None,
        limit: Annotated[
            int | None,
            Field(
                ge=1,
                le=MAX_DEPARTURES,
                description=f"Maximum departures to list (default {DEFAULT_DEPARTURES}).",
            ),
        ] = None,
    ) -> ToolResult:
        return await _respond(
            "get_schedule",
            get_schedule(
                deps,
                stop=stop,
                latitude=latitude,
                longitude=longitude,
                route=route,
                mode=mode,
                date=date,
                after=after,
                before=before,
                limit=limit,
            ),
        )

    @server.tool(
        name="get_route",
        title="Describe a route",
        description=(
            "Everything about one CATS route on a date: for each direction, where it goes, "
            "its stops in order, its first and last trips, and how often it runs in the "
            "early morning, morning rush, midday, afternoon rush, evening, and late night. "
            "Also how many trips run on each of the next seven days, and how many service "
            "alerts are active. Use it for 'where does the Blue Line go' or 'how often does "
            "the 9 run on Saturday' (pass that Saturday's date)."
        ),
        annotations=_READ_ONLY,
    )
    async def _get_route(
        route: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                description='Route number or name, e.g. "9", "501", "Blue Line", "Airport".',
            ),
        ],
        date: ServiceDate | None = None,
    ) -> ToolResult:
        return await _respond("get_route", get_route(deps, route=route, date=date))

    @server.tool(
        name="get_service_alerts",
        title="Get service alerts",
        description=(
            "Current and upcoming CATS service alerts: detours, closed stops, and suspended "
            'service, with what is affected, why, and until when. Give "route" or "stop" '
            "to narrow them, the user's location as latitude and longitude for alerts "
            "affecting stops within 800 m, or nothing for every alert in the system."
        ),
        annotations=_READ_ONLY,
    )
    async def _get_service_alerts(
        route: Annotated[
            str | None,
            Field(min_length=1, max_length=64, description="Alerts affecting this route."),
        ] = None,
        stop: StopQuery | None = None,
        latitude: Latitude | None = None,
        longitude: Longitude | None = None,
    ) -> ToolResult:
        return await _respond(
            "get_service_alerts",
            get_service_alerts(
                deps, route=route, stop=stop, latitude=latitude, longitude=longitude
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
