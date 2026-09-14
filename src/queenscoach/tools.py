"""The transit tools exposed over MCP.

Each tool returns a plain JSON-shaped ``dict`` so the MCP layer can render it
both as text and as structured content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .cache import Cached
from .realtime import RealtimeFeeds
from .static_gtfs import Mode, Route, Schedule, Stop
from .transit import (
    ArrivalView,
    ResolvedRouteRef,
    Station,
    TransitSnapshot,
    VehicleView,
    alerts_for,
    arrivals_at_stops,
    by_distance,
    distance_meters,
    find_routes,
    find_stops,
    group_into_stations,
    matches_vehicle_query,
    natural_key,
    routes_by_name,
    serves,
    station_for,
    to_vehicle_views,
)

#: Keeps a single tool response well under typical model context limits.
MAX_RESULTS = 250
MAX_ARRIVALS = 50
DEFAULT_ARRIVALS = 10
DEFAULT_NEARBY_STOPS = 10
#: CATS serves the Charlotte region; a "nearest stop" farther than this is not near.
MAX_NEARBY_METERS = 50_000

ToolResult = dict[str, Any]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Dependencies:
    """Everything the tools need, injected so tests can supply fixtures."""

    load_schedule: Callable[[], Awaitable[Cached[Schedule]]]
    #: The raw static GTFS zip the schedule was parsed from.
    load_archive: Callable[[], Awaitable[Cached[bytes]]]
    feeds: RealtimeFeeds
    #: Where the static archive comes from, reported by the ``gtfs://static`` resource.
    static_gtfs_url: str = ""
    #: Current Unix time in seconds; overridden in tests for determinism.
    now: Callable[[], float] = field(default=time.time)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    transit: TransitSnapshot
    feed_age_seconds: int

    @property
    def schedule(self) -> Schedule:
        return self.transit.schedule


async def _snapshot(deps: Dependencies) -> _Snapshot:
    schedule, vehicles, trip_updates = await asyncio.gather(
        deps.load_schedule(), deps.feeds.vehicle_positions(), deps.feeds.trip_updates()
    )
    now = deps.now()
    return _Snapshot(
        transit=TransitSnapshot(
            schedule=schedule.value,
            vehicles=vehicles.value,
            trip_updates=trip_updates.value,
            now=now,
        ),
        feed_age_seconds=max(0, round(now - vehicles.fetched_at)),
    )


def _route_ref_json(route: ResolvedRouteRef | None) -> dict[str, Any] | None:
    if route is None:
        return None
    return {
        "routeId": route.route_id,
        "name": route.name,
        "longName": route.long_name,
        "mode": route.mode,
    }


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    """Omit absent fields, so a response carries only what is actually known."""
    return {key: value for key, value in payload.items() if value is not None}


def _vehicle_json(view: VehicleView) -> dict[str, Any]:
    next_stop = view.next_stop
    return _drop_none(
        {
            "vehicle": view.vehicle,
            "mode": view.mode,
            "route": _route_ref_json(view.route),
            "headsign": view.headsign,
            "tripId": view.trip_id,
            "latitude": view.latitude,
            "longitude": view.longitude,
            "bearingDegrees": view.bearing_degrees,
            "speedMph": view.speed_mph,
            "occupancy": view.occupancy,
            "reportedAt": view.reported_at,
            "positionAgeSeconds": view.position_age_seconds,
            "nextStop": None
            if next_stop is None
            else _drop_none(
                {
                    "stopId": next_stop.stop_id,
                    "name": next_stop.name,
                    "arrivalTime": next_stop.arrival_time,
                    "minutesAway": next_stop.minutes_away,
                }
            ),
        }
    )


def _arrival_json(arrival: ArrivalView) -> dict[str, Any]:
    position = arrival.vehicle_position
    return _drop_none(
        {
            "stopId": arrival.stop_id,
            "route": _route_ref_json(arrival.route),
            "headsign": arrival.headsign,
            "vehicle": arrival.vehicle,
            "tripId": arrival.trip_id,
            "arrivalTime": arrival.arrival_time,
            "minutesAway": arrival.minutes_away,
            "scheduledArrivalTime": arrival.scheduled_arrival_time,
            "scheduleDeviationMinutes": arrival.schedule_deviation_minutes,
            "vehiclePosition": None
            if position is None
            else {"latitude": position[0], "longitude": position[1]},
        }
    )


def _apply_mode(views: list[VehicleView], mode: Mode | None) -> list[VehicleView]:
    return views if mode is None else [view for view in views if view.mode == mode]


def _route_json(route: Route) -> dict[str, Any]:
    return {
        "routeId": route.route_id,
        "name": route.short_name,
        "longName": route.long_name,
        "mode": route.mode,
    }


def _resolve_routes(schedule: Schedule, query: str | None) -> list[Route] | ToolResult | None:
    """Resolve an optional route filter: ``None`` for no filter, a dict for a miss."""
    if query is None:
        return None
    routes = find_routes(schedule, query)
    if routes:
        return routes
    return {
        "error": f"No route matched {json.dumps(query)}.",
        "availableRoutes": sorted(
            (known.short_name for known in schedule.routes.values()), key=natural_key
        ),
    }


def _coordinates_error(latitude: float | None, longitude: float | None) -> ToolResult | None:
    if (latitude is None) != (longitude is None):
        return {"error": 'Provide "latitude" and "longitude" together.'}
    return None


def _describe_filter(routes: list[Route] | None, mode: Mode | None) -> str:
    if routes:
        return " serving " + ", ".join(route.short_name for route in routes)
    if mode is not None:
        return f" with {mode} service"
    return ""


async def list_vehicles(
    deps: Dependencies,
    *,
    vehicle: str | None = None,
    route: str | None = None,
    mode: Mode | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Report vehicles in service with their positions: one, a route's, or all of them."""
    state = await _snapshot(deps)
    candidates = list(state.transit.vehicles)

    if vehicle is not None:
        candidates = [
            candidate for candidate in candidates if matches_vehicle_query(candidate, vehicle)
        ]

    routes = _resolve_routes(state.schedule, route)
    if isinstance(routes, dict):
        return routes
    matched_routes: list[str] | None = None
    if routes is not None:
        matched_routes = [known.short_name for known in routes]
        route_ids = {known.route_id for known in routes}
        trips = state.schedule.trips
        candidates = [
            candidate
            for candidate in candidates
            # Fall back to the trip's route: some vehicles report a trip but no route.
            if (candidate.route_id is not None and candidate.route_id in route_ids)
            or (
                candidate.trip_id is not None
                and candidate.trip_id in trips
                and trips[candidate.trip_id].route_id in route_ids
            )
        ]

    views = _apply_mode(to_vehicle_views(candidates, state.transit), mode)
    views.sort(
        key=lambda view: (
            natural_key(view.route.name if view.route is not None else ""),
            natural_key(view.vehicle),
        )
    )

    capped = min(limit or MAX_RESULTS, MAX_RESULTS)
    counts = {"bus": 0, "train": 0, "unknown": 0}
    for view in views:
        counts[view.mode] += 1

    message: str | None = None
    if not views:
        if vehicle is not None:
            message = (
                f"Vehicle {vehicle} is not reporting a position right now. "
                "Vehicles appear only while in service."
            )
        elif matched_routes is not None:
            message = f"No vehicles are currently in service on {', '.join(matched_routes)}."
        else:
            message = "No vehicles are reporting a position right now."

    return _drop_none(
        {
            "matches": len(views),
            "returned": min(len(views), capped),
            "countsByMode": counts,
            "matchedRoutes": matched_routes,
            "vehicles": [_vehicle_json(view) for view in views[:capped]],
            "message": message,
            "feedAgeSeconds": state.feed_age_seconds,
        }
    )


def _station_json(
    schedule: Schedule, station: Station, origin: tuple[float, float] | None
) -> dict[str, Any]:
    stop = station.primary
    routes = routes_by_name(schedule, station.route_ids)
    return _drop_none(
        {
            "name": stop.name,
            "stopIds": sorted(station.stop_ids),
            "latitude": stop.latitude,
            "longitude": stop.longitude,
            "metersAway": None
            if origin is None
            else round(distance_meters(*origin, stop.latitude, stop.longitude)),
            "modes": sorted({route.mode for route in routes}),
            "routes": [_route_json(route) for route in routes],
        }
    )


async def list_stops(
    deps: Dependencies,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    query: str | None = None,
    route: str | None = None,
    mode: Mode | None = None,
    limit: int | None = None,
) -> ToolResult:
    """List stations with the routes serving each, nearest first when given a location."""
    coordinates_error = _coordinates_error(latitude, longitude)
    if coordinates_error is not None:
        return coordinates_error

    schedule = (await deps.load_schedule()).value
    routes = _resolve_routes(schedule, route)
    if isinstance(routes, dict):
        return routes
    route_ids = None if routes is None else frozenset(known.route_id for known in routes)

    pool: list[Stop] = (
        list(schedule.stops.values())
        if query is None
        else find_stops(schedule, query, limit=len(schedule.stops))
    )
    stops = [stop for stop in pool if serves(schedule, stop, route_ids=route_ids, mode=mode)]

    origin: tuple[float, float] | None = None
    if latitude is not None and longitude is not None:
        origin = (latitude, longitude)
        stops = [
            stop
            for stop in by_distance(stops, latitude, longitude)
            if distance_meters(latitude, longitude, stop.latitude, stop.longitude)
            <= MAX_NEARBY_METERS
        ]
        default_limit = DEFAULT_NEARBY_STOPS
    else:
        stops.sort(key=lambda stop: (natural_key(stop.name), stop.stop_id))
        default_limit = MAX_RESULTS

    stations = group_into_stations(schedule, stops)
    capped = min(limit or default_limit, MAX_RESULTS)

    message: str | None = None
    if not stations:
        described = _describe_filter(routes, mode)
        if origin is not None:
            message = (
                f"No CATS stop{described} is within {MAX_NEARBY_METERS // 1000} km of "
                "those coordinates. CATS serves the Charlotte, North Carolina area."
            )
        else:
            message = f"No CATS stop{described} matched."

    return _drop_none(
        {
            "totalMatching": len(stations),
            "returned": min(len(stations), capped),
            "matchedRoutes": None if routes is None else [known.short_name for known in routes],
            "stations": [_station_json(schedule, station, origin) for station in stations[:capped]],
            "message": message,
        }
    )


async def get_arrivals(
    deps: Dependencies,
    *,
    stop: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    route: str | None = None,
    mode: Mode | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Report predicted arrivals at a station, named or nearest to a location."""
    if stop is not None and (latitude is not None or longitude is not None):
        return {"error": 'Provide "stop" or a location, not both.'}
    coordinates_error = _coordinates_error(latitude, longitude)
    if coordinates_error is not None:
        return coordinates_error

    state = await _snapshot(deps)
    schedule = state.schedule

    routes = _resolve_routes(schedule, route)
    if isinstance(routes, dict):
        return routes
    route_ids = None if routes is None else frozenset(known.route_id for known in routes)

    other_matches: list[dict[str, Any]] = []
    meters_away: int | None = None
    if latitude is not None and longitude is not None:
        nearest = next(
            (
                candidate
                for candidate in by_distance(schedule.stops.values(), latitude, longitude)
                if serves(schedule, candidate, route_ids=route_ids, mode=mode)
            ),
            None,
        )
        if nearest is not None:
            meters_away = round(
                distance_meters(latitude, longitude, nearest.latitude, nearest.longitude)
            )
        if nearest is None or meters_away is None or meters_away > MAX_NEARBY_METERS:
            return {
                "error": (
                    f"No CATS stop{_describe_filter(routes, mode)} is within "
                    f"{MAX_NEARBY_METERS // 1000} km of those coordinates."
                ),
                "hint": "CATS serves the Charlotte, North Carolina area.",
            }
        station = station_for(schedule, nearest)
    elif stop is not None:
        stops = find_stops(schedule, stop)
        if not stops:
            return {
                "error": f"No stop matched {json.dumps(stop)}.",
                "hint": 'Try a stop id like "02400", or part of a stop name like "Beatties Ford".',
            }
        station = station_for(schedule, stops[0])
        best = station.primary
        other_matches = [
            {
                "stopId": candidate.stop_id,
                "name": candidate.name,
                "metersAway": round(
                    distance_meters(
                        best.latitude, best.longitude, candidate.latitude, candidate.longitude
                    )
                ),
            }
            for candidate in stops[1:]
            if candidate.stop_id not in station.stop_ids
        ][:5]
    else:
        return {
            "error": 'Provide "stop", or "latitude" and "longitude" for the nearest station.',
        }

    arrivals = arrivals_at_stops(station.stop_ids, state.transit, route_ids=route_ids, mode=mode)
    capped = min(limit or DEFAULT_ARRIVALS, MAX_ARRIVALS)

    alert_route_ids = frozenset(
        arrival.route.route_id for arrival in arrivals if arrival.route is not None
    )
    try:
        cached_alerts = await deps.feeds.alerts()
        alerts = alerts_for(cached_alerts.value, station.stop_ids, alert_route_ids)
    except Exception as error:
        # Alerts are supplementary; arrivals stay useful if that one feed is down.
        _logger.warning("alerts feed unavailable for get_arrivals: %s", error)
        alerts = []

    primary = station.primary
    return _drop_none(
        {
            "stop": _drop_none(
                {
                    "stopId": primary.stop_id,
                    "name": primary.name,
                    "latitude": primary.latitude,
                    "longitude": primary.longitude,
                    "metersAway": meters_away,
                    "platformStopIds": sorted(station.stop_ids)
                    if len(station.platforms) > 1
                    else None,
                }
            ),
            "otherStopsMatchingQuery": other_matches or None,
            "matchedRoutes": None if routes is None else [known.short_name for known in routes],
            "arrivalCount": len(arrivals),
            "arrivals": [_arrival_json(arrival) for arrival in arrivals[:capped]],
            "message": None
            if arrivals
            else "No realtime arrivals are predicted for this stop right now.",
            "serviceAlerts": [
                _drop_none(
                    {
                        "headerText": alert.header_text,
                        "descriptionText": alert.description_text,
                        "cause": alert.cause,
                        "effect": alert.effect,
                        "severityLevel": alert.severity_level,
                        "informedRouteIds": list(alert.informed_route_ids),
                        "informedStopIds": list(alert.informed_stop_ids),
                    }
                )
                for alert in alerts
            ]
            or None,
            "feedAgeSeconds": state.feed_age_seconds,
        }
    )
