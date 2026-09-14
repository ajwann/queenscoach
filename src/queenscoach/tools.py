"""The three transit tools exposed over MCP.

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
from .static_gtfs import Mode, Schedule
from .transit import (
    ArrivalView,
    ResolvedRouteRef,
    TransitSnapshot,
    VehicleView,
    alerts_for,
    arrivals_at_stop,
    distance_meters,
    find_routes,
    find_stops,
    matches_vehicle_query,
    natural_key,
    to_vehicle_views,
)

#: Keeps a single tool response well under typical model context limits.
MAX_RESULTS = 250
MAX_ARRIVALS = 50
DEFAULT_ARRIVALS = 10

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


async def find_vehicle(
    deps: Dependencies,
    *,
    vehicle: str | None = None,
    route: str | None = None,
    mode: Mode | None = None,
) -> ToolResult:
    """Locate one vehicle by number, or every vehicle running a route."""
    if vehicle is None and route is None:
        return {
            "error": 'Provide "vehicle" (a vehicle number) or "route" to locate.',
            "hint": "Use list_vehicles to see everything currently in service.",
        }

    state = await _snapshot(deps)
    candidates = list(state.transit.vehicles)

    if vehicle is not None:
        candidates = [
            candidate for candidate in candidates if matches_vehicle_query(candidate, vehicle)
        ]

    matched_routes: list[str] | None = None
    if route is not None:
        routes = find_routes(state.schedule, route)
        if not routes:
            return {
                "error": f"No route matched {json.dumps(route)}.",
                "availableRoutes": sorted(
                    (known.short_name for known in state.schedule.routes.values()),
                    key=natural_key,
                ),
            }
        matched_routes = [known.short_name for known in routes]
        route_ids = {known.route_id for known in routes}
        trips = state.schedule.trips
        candidates = [
            candidate
            for candidate in candidates
            if (candidate.route_id is not None and candidate.route_id in route_ids)
            or (
                candidate.trip_id is not None
                and candidate.trip_id in trips
                and trips[candidate.trip_id].route_id in route_ids
            )
        ]

    views = _apply_mode(to_vehicle_views(candidates, state.transit), mode)

    if not views:
        if vehicle is not None:
            message = (
                f"Vehicle {vehicle} is not reporting a position right now. "
                "Vehicles appear only while in service."
            )
        else:
            named = ", ".join(matched_routes) if matched_routes else "that route"
            message = f"No vehicles are currently in service on {named}."
        return _drop_none(
            {
                "matches": 0,
                "message": message,
                "matchedRoutes": matched_routes,
                "feedAgeSeconds": state.feed_age_seconds,
            }
        )

    return _drop_none(
        {
            "matches": len(views),
            "matchedRoutes": matched_routes,
            "vehicles": [_vehicle_json(view) for view in views[:MAX_RESULTS]],
            "feedAgeSeconds": state.feed_age_seconds,
        }
    )


async def list_vehicles(
    deps: Dependencies,
    *,
    mode: Mode | None = None,
    route: str | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Report every vehicle currently in service, optionally filtered."""
    state = await _snapshot(deps)
    candidates = list(state.transit.vehicles)
    matched_routes: list[str] | None = None

    if route is not None:
        routes = find_routes(state.schedule, route)
        if not routes:
            return {"error": f"No route matched {json.dumps(route)}."}
        matched_routes = [known.short_name for known in routes]
        route_ids = {known.route_id for known in routes}
        candidates = [
            candidate
            for candidate in candidates
            if candidate.route_id is not None and candidate.route_id in route_ids
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

    return _drop_none(
        {
            "totalInService": len(views),
            "returned": min(len(views), capped),
            "countsByMode": counts,
            "matchedRoutes": matched_routes,
            "vehicles": [_vehicle_json(view) for view in views[:capped]],
            "feedAgeSeconds": state.feed_age_seconds,
        }
    )


async def get_arrivals(
    deps: Dependencies,
    *,
    stop: str,
    route: str | None = None,
    mode: Mode | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Report predicted arrivals at one stop, with any alerts affecting it."""
    state = await _snapshot(deps)
    stops = find_stops(state.schedule, stop)

    if not stops:
        return {
            "error": f"No stop matched {json.dumps(stop)}.",
            "hint": 'Try a stop id like "02400", or part of a stop name like "Beatties Ford".',
        }
    best = stops[0]

    route_ids: frozenset[str] | None = None
    matched_routes: list[str] | None = None
    if route is not None:
        routes = find_routes(state.schedule, route)
        if not routes:
            return {"error": f"No route matched {json.dumps(route)}."}
        matched_routes = [known.short_name for known in routes]
        route_ids = frozenset(known.route_id for known in routes)

    arrivals = arrivals_at_stop(best, state.transit, route_ids=route_ids, mode=mode)
    capped = min(limit or DEFAULT_ARRIVALS, MAX_ARRIVALS)

    alert_route_ids = frozenset(
        arrival.route.route_id for arrival in arrivals if arrival.route is not None
    )
    try:
        cached_alerts = await deps.feeds.alerts()
        alerts = alerts_for(cached_alerts.value, best.stop_id, alert_route_ids)
    except Exception as error:
        # Alerts are supplementary; arrivals stay useful if that one feed is down.
        _logger.warning("alerts feed unavailable for get_arrivals: %s", error)
        alerts = []

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
        for candidate in stops[1:6]
    ]

    return _drop_none(
        {
            "stop": {
                "stopId": best.stop_id,
                "name": best.name,
                "latitude": best.latitude,
                "longitude": best.longitude,
            },
            "otherStopsMatchingQuery": other_matches or None,
            "matchedRoutes": matched_routes,
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
