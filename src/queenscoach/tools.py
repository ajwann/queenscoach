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
from datetime import date, timedelta
from typing import Any

from . import planner
from .cache import Cached
from .realtime import RealtimeFeeds, ServiceAlert
from .static_gtfs import Mode, Route, Schedule, Stop
from .timetable import (
    Departure,
    departures_at,
    destination_of,
    frequencies,
    informative_headsign,
    iso_local,
    local_date,
    most_common_destination,
    parse_clock,
    parse_service_date,
    route_directions,
    service_dates_from,
    service_day_origin,
    trips_running,
)
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


@dataclass(frozen=True, slots=True)
class _Located:
    station: Station
    #: From the rider's location to the station, when they gave one.
    meters_away: int | None
    #: Other stops a name query matched, for the rider to pick instead.
    other_matches: list[dict[str, Any]]


def _place_error(
    stop: str | None, latitude: float | None, longitude: float | None
) -> ToolResult | None:
    """Check that exactly one of a stop and a full location was given."""
    if stop is not None and (latitude is not None or longitude is not None):
        return {"error": 'Provide "stop" or a location, not both.'}
    coordinates_error = _coordinates_error(latitude, longitude)
    if coordinates_error is not None:
        return coordinates_error
    if stop is None and latitude is None:
        return {"error": 'Provide "stop", or "latitude" and "longitude" for the nearest station.'}
    return None


def _locate(
    schedule: Schedule,
    *,
    stop: str | None,
    latitude: float | None,
    longitude: float | None,
    routes: list[Route] | None,
    mode: Mode | None,
) -> _Located | ToolResult:
    """The station a stop query names, or the nearest one serving the filter to a location."""
    route_ids = None if routes is None else frozenset(known.route_id for known in routes)
    if latitude is not None and longitude is not None:
        nearest = next(
            (
                candidate
                for candidate in by_distance(schedule.stops.values(), latitude, longitude)
                if serves(schedule, candidate, route_ids=route_ids, mode=mode)
            ),
            None,
        )
        meters_away = (
            None
            if nearest is None
            else round(distance_meters(latitude, longitude, nearest.latitude, nearest.longitude))
        )
        if nearest is None or meters_away is None or meters_away > MAX_NEARBY_METERS:
            return {
                "error": (
                    f"No CATS stop{_describe_filter(routes, mode)} is within "
                    f"{MAX_NEARBY_METERS // 1000} km of those coordinates."
                ),
                "hint": "CATS serves the Charlotte, North Carolina area.",
            }
        return _Located(
            station=station_for(schedule, nearest), meters_away=meters_away, other_matches=[]
        )

    stops = find_stops(schedule, stop or "")
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
    return _Located(station=station, meters_away=None, other_matches=other_matches)


def _located_stop_json(located: _Located) -> dict[str, Any]:
    primary = located.station.primary
    return _drop_none(
        {
            "stopId": primary.stop_id,
            "name": primary.name,
            "latitude": primary.latitude,
            "longitude": primary.longitude,
            "metersAway": located.meters_away,
            "platformStopIds": sorted(located.station.stop_ids)
            if len(located.station.platforms) > 1
            else None,
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
    place_error = _place_error(stop, latitude, longitude)
    if place_error is not None:
        return place_error

    state = await _snapshot(deps)
    schedule = state.schedule
    routes = _resolve_routes(schedule, route)
    if isinstance(routes, dict):
        return routes
    route_ids = None if routes is None else frozenset(known.route_id for known in routes)
    located = _locate(
        schedule, stop=stop, latitude=latitude, longitude=longitude, routes=routes, mode=mode
    )
    if isinstance(located, dict):
        return located
    station = located.station

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

    return _drop_none(
        {
            "stop": _located_stop_json(located),
            "otherStopsMatchingQuery": located.other_matches or None,
            "matchedRoutes": None if routes is None else [known.short_name for known in routes],
            "arrivalCount": len(arrivals),
            "arrivals": [_arrival_json(arrival) for arrival in arrivals[:capped]],
            "message": None
            if arrivals
            else "No realtime arrivals are predicted for this stop right now.",
            "serviceAlerts": [
                _alert_json(schedule, alert, state.transit.now)
                for alert in alerts
                if not alert.has_ended(state.transit.now)
            ]
            or None,
            "feedAgeSeconds": state.feed_age_seconds,
        }
    )


# -- timetable tools ---------------------------------------------------------------------

#: An alert whose end is further off than this is open-ended in practice; CATS
#: writes "until further notice" as the year 3000.
_OPEN_ENDED_SECONDS = 5 * 365 * 86_400
#: How far from a rider's location to look for stops whose alerts affect them.
ALERT_NEARBY_METERS = 800
DEFAULT_DEPARTURES = 20
MAX_DEPARTURES = 100
DEFAULT_WALK_METERS = 800
MAX_WALK_METERS = 2_000
MAX_TRANSFERS = 3
DEFAULT_TRANSFERS = 2
#: Candidate stops considered around a trip's start or end point.
_MAX_ENDPOINT_STOPS = 40
_ALERT_STOPS_LISTED = 20
_OTHER_PATTERNS_LISTED = 5

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _coverage_json(schedule: Schedule) -> dict[str, Any] | None:
    first, last = schedule.calendar.first_date, schedule.calendar.last_date
    if first is None or last is None:
        return None
    return {"from": first.isoformat(), "through": last.isoformat()}


def _service_date(schedule: Schedule, raw: str | None, now: float) -> date | ToolResult:
    """The requested date (default today, locally), if the published schedule covers it."""
    today = local_date(now, schedule.zone)
    day = today if raw is None else parse_service_date(raw)
    if day is None:
        return {"error": f'{json.dumps(raw)} is not a date; use YYYY-MM-DD, e.g. "{today}".'}
    first, last = schedule.calendar.first_date, schedule.calendar.last_date
    if first is None or last is None:
        return {"error": "The published CATS schedule lists no service dates."}
    if not first <= day <= last:
        return {
            "error": (
                f"The published CATS schedule covers {first.isoformat()} through "
                f"{last.isoformat()}, so {day.isoformat()} can't be answered yet."
            ),
            "hint": "CATS publishes schedule updates ahead of changes; ask closer to the date.",
            "scheduleCovers": _coverage_json(schedule),
        }
    return day


def _clock(raw: str | None, name: str) -> int | ToolResult | None:
    if raw is None:
        return None
    seconds = parse_clock(raw)
    if seconds is None:
        return {
            "error": (
                f'{name} {json.dumps(raw)} is not a time; use 24-hour HH:MM, e.g. "17:30". '
                'After midnight on the same night, use hours past 24, e.g. "25:15" for 1:15 am.'
            )
        }
    return seconds


def _route_name_json(schedule: Schedule, route_id: str) -> dict[str, Any]:
    route = schedule.routes.get(route_id)
    return {"routeId": route_id} if route is None else _route_json(route)


def _alert_json(schedule: Schedule, alert: ServiceAlert, now: float) -> dict[str, Any]:
    zone = schedule.zone
    stops = [
        {"stopId": stop_id, "name": schedule.stops[stop_id].name}
        if stop_id in schedule.stops
        else {"stopId": stop_id}
        for stop_id in alert.informed_stop_ids
    ]
    return _drop_none(
        {
            "headerText": alert.header_text,
            "descriptionText": alert.description_text,
            "status": "active" if alert.is_active(now) else "upcoming",
            "effect": alert.effect,
            "effectDetail": alert.effect_detail,
            "cause": alert.cause,
            "causeDetail": alert.cause_detail,
            "severityLevel": alert.severity_level,
            "url": alert.url,
            "activePeriods": [
                _drop_none(
                    {
                        "start": None if period.start is None else iso_local(period.start, zone),
                        "end": None
                        if period.end is None or period.end - now > _OPEN_ENDED_SECONDS
                        else iso_local(period.end, zone),
                        "untilFurtherNotice": period.end is None
                        or period.end - now > _OPEN_ENDED_SECONDS,
                    }
                )
                for period in alert.active_periods
            ]
            or None,
            "routes": [
                _route_name_json(schedule, route_id) for route_id in alert.informed_route_ids
            ]
            or None,
            "stops": stops[:_ALERT_STOPS_LISTED] or None,
            "stopCount": len(stops) if len(stops) > _ALERT_STOPS_LISTED else None,
        }
    )


def _departure_json(schedule: Schedule, departure: Departure) -> dict[str, Any]:
    return _drop_none(
        {
            "departsAt": iso_local(departure.departs_at, schedule.zone),
            "route": None if departure.route is None else _route_json(departure.route),
            "destination": departure.destination,
            "headsign": informative_headsign(departure.trip),
            "stopId": departure.stop_id,
            "tripId": departure.trip.trip_id,
        }
    )


async def get_schedule(
    deps: Dependencies,
    *,
    stop: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    route: str | None = None,
    mode: Mode | None = None,
    date: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Report the published timetable at a station for one service date."""
    place_error = _place_error(stop, latitude, longitude)
    if place_error is not None:
        return place_error
    after_seconds, before_seconds = _clock(after, "after"), _clock(before, "before")
    for parsed in (after_seconds, before_seconds):
        if isinstance(parsed, dict):
            return parsed

    schedule = (await deps.load_schedule()).value
    now = deps.now()
    day = _service_date(schedule, date, now)
    if isinstance(day, dict):
        return day
    routes = _resolve_routes(schedule, route)
    if isinstance(routes, dict):
        return routes
    route_ids = None if routes is None else frozenset(known.route_id for known in routes)
    located = _locate(
        schedule, stop=stop, latitude=latitude, longitude=longitude, routes=routes, mode=mode
    )
    if isinstance(located, dict):
        return located

    zone = schedule.zone
    origin = service_day_origin(day, zone)
    if isinstance(after_seconds, int):
        window_start = origin + after_seconds
    elif day == local_date(now, zone):
        window_start = int(now)
    else:
        window_start = origin
    window_end = origin + before_seconds if isinstance(before_seconds, int) else None

    departures = await asyncio.to_thread(
        departures_at,
        schedule,
        located.station.stop_ids,
        [day - timedelta(days=1), day],
        route_ids=route_ids,
        mode=mode,
    )
    that_day = [departure for departure in departures if departure.service_date == day]
    in_window = [
        departure
        for departure in departures
        # The previous service day counts only for its trips running past midnight into this one.
        if (departure.service_date == day or departure.departs_at >= origin)
        and departure.departs_at >= window_start
        and (window_end is None or departure.departs_at <= window_end)
    ]
    capped = min(limit or DEFAULT_DEPARTURES, MAX_DEPARTURES)

    message: str | None = None
    if not that_day:
        described = _describe_filter(routes, mode)
        message = f"No scheduled departures from this stop{described} on {day.isoformat()}."
    elif not in_window:
        message = "No more scheduled departures in that time window."

    return _drop_none(
        {
            "stop": _located_stop_json(located),
            "otherStopsMatchingQuery": located.other_matches or None,
            "serviceDate": day.isoformat(),
            "weekday": _WEEKDAYS[day.weekday()],
            "matchedRoutes": None if routes is None else [known.short_name for known in routes],
            "firstDeparture": None if not that_day else iso_local(that_day[0].departs_at, zone),
            "lastDeparture": None if not that_day else iso_local(that_day[-1].departs_at, zone),
            "departuresThatDay": len(that_day),
            "totalInWindow": len(in_window),
            "departures": [_departure_json(schedule, d) for d in in_window[:capped]],
            "message": message,
            "scheduleCovers": _coverage_json(schedule),
            "note": "Published timetable. For live predictions in the next hour, use get_arrivals.",
        }
    )


async def get_route(
    deps: Dependencies,
    *,
    route: str,
    date: str | None = None,
) -> ToolResult:
    """Describe one route on a service date: where it goes, its stops, and how often it runs."""
    schedule = (await deps.load_schedule()).value
    now = deps.now()
    routes = _resolve_routes(schedule, route)
    if isinstance(routes, dict):
        return routes
    if not routes:
        return {"error": 'Provide "route".'}
    if len(routes) > 1:
        return {
            "error": f"{len(routes)} routes matched {json.dumps(route)}; name one.",
            "matchingRoutes": [
                _route_json(known)
                for known in sorted(routes, key=lambda r: natural_key(r.short_name))
            ],
        }
    known = routes[0]
    day = _service_date(schedule, date, now)
    if isinstance(day, dict):
        return day
    zone = schedule.zone
    route_ids = frozenset({known.route_id})

    def summarize() -> dict[str, Any]:
        trips = trips_running(schedule, day, route_ids=route_ids)
        directions = []
        for direction in route_directions(trips):
            starts = [timetable.departures[0] for _, timetable in direction.trips]
            origin = service_day_origin(day, zone)
            main = direction.main_pattern
            headsigns = sorted(
                {h for trip, _ in direction.trips if (h := informative_headsign(trip)) is not None}
            )
            directions.append(
                _drop_none(
                    {
                        "directionId": direction.direction_id,
                        "destination": most_common_destination(schedule, direction.trips),
                        "headsigns": headsigns or None,
                        "tripCount": len(direction.trips),
                        "firstDeparture": iso_local(origin + min(starts), zone),
                        "lastDeparture": iso_local(origin + max(starts), zone),
                        "frequency": [
                            _drop_none(
                                {
                                    "period": frequency.period.name,
                                    "trips": frequency.trips,
                                    "typicalMinutesBetween": frequency.typical_minutes_between,
                                }
                            )
                            for frequency in frequencies(starts)
                        ],
                        "stops": [
                            {
                                "stopId": stop_id,
                                "name": schedule.stops[stop_id].name,
                                "latitude": schedule.stops[stop_id].latitude,
                                "longitude": schedule.stops[stop_id].longitude,
                            }
                            for stop_id in main.stop_ids
                            if stop_id in schedule.stops
                        ],
                        "stopsFollowedBy": f"{len(main.trips)} of {len(direction.trips)} trips",
                        "otherPatterns": [
                            _drop_none(
                                {
                                    "from": schedule.stops[pattern.stop_ids[0]].name
                                    if pattern.stop_ids[0] in schedule.stops
                                    else None,
                                    "to": schedule.stops[pattern.stop_ids[-1]].name
                                    if pattern.stop_ids[-1] in schedule.stops
                                    else None,
                                    "stopCount": len(pattern.stop_ids),
                                    "tripCount": len(pattern.trips),
                                }
                            )
                            for pattern in direction.patterns[1 : 1 + _OTHER_PATTERNS_LISTED]
                        ]
                        or None,
                    }
                )
            )
        week = [
            {
                "date": service_date.isoformat(),
                "weekday": _WEEKDAYS[service_date.weekday()],
                "trips": len(trips_running(schedule, service_date, route_ids=route_ids)),
            }
            for service_date in service_dates_from(schedule, day, 7)
        ]
        return {"directions": directions, "week": week, "tripCount": len(trips)}

    summary = await asyncio.to_thread(summarize)

    active_alerts: int | None = None
    try:
        cached_alerts = await deps.feeds.alerts()
        route_stops = frozenset(
            stop_id for stop_id, served in schedule.stop_routes.items() if known.route_id in served
        )
        active_alerts = sum(
            1
            for alert in alerts_for(cached_alerts.value, route_stops, route_ids)
            if alert.is_active(now)
        )
    except Exception as error:
        _logger.warning("alerts feed unavailable for get_route: %s", error)

    return _drop_none(
        {
            "route": _route_json(known),
            "serviceDate": day.isoformat(),
            "weekday": _WEEKDAYS[day.weekday()],
            "tripCount": summary["tripCount"],
            "directions": summary["directions"],
            "message": None
            if summary["tripCount"]
            else f"Route {known.short_name} has no scheduled trips on {day.isoformat()}.",
            "serviceNextSevenDays": summary["week"],
            "activeAlerts": active_alerts,
            "alertsHint": "Use get_service_alerts for details." if active_alerts else None,
            "scheduleCovers": _coverage_json(schedule),
        }
    )


async def get_service_alerts(
    deps: Dependencies,
    *,
    route: str | None = None,
    stop: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> ToolResult:
    """Report current and upcoming service alerts, system-wide or for a route, stop, or place."""
    has_location = latitude is not None or longitude is not None
    if sum((route is not None, stop is not None, has_location)) > 1:
        return {"error": 'Provide at most one of "route", "stop", or a location.'}
    coordinates_error = _coordinates_error(latitude, longitude)
    if coordinates_error is not None:
        return coordinates_error

    schedule_entry, alerts_entry = await asyncio.gather(deps.load_schedule(), deps.feeds.alerts())
    schedule = schedule_entry.value
    now = deps.now()

    scope: str
    stop_ids: frozenset[str] | None = None
    route_ids: frozenset[str] | None = None
    matched_routes: list[str] | None = None
    located_json: dict[str, Any] | None = None
    if route is not None:
        routes = _resolve_routes(schedule, route)
        if isinstance(routes, dict):
            return routes
        if not routes:
            return {"error": 'Provide "route".'}
        route_ids = frozenset(known.route_id for known in routes)
        matched_routes = [known.short_name for known in routes]
        stop_ids = frozenset(
            stop_id for stop_id, served in schedule.stop_routes.items() if served & route_ids
        )
        scope = f"route {', '.join(matched_routes)}"
    elif stop is not None:
        located = _locate(
            schedule, stop=stop, latitude=None, longitude=None, routes=None, mode=None
        )
        if isinstance(located, dict):
            return located
        stop_ids = located.station.stop_ids
        route_ids = located.station.route_ids
        located_json = _located_stop_json(located)
        scope = located.station.primary.name
    elif latitude is not None and longitude is not None:
        nearby = [
            candidate
            for candidate in by_distance(schedule.stops.values(), latitude, longitude)
            if candidate.stop_id in schedule.stop_routes
            and distance_meters(latitude, longitude, candidate.latitude, candidate.longitude)
            <= ALERT_NEARBY_METERS
        ]
        if not nearby:
            return {
                "error": f"No CATS stop is within {ALERT_NEARBY_METERS} m of those coordinates.",
                "hint": 'Ask about a "route" or "stop" instead, or leave all out for every alert.',
            }
        stop_ids = frozenset(candidate.stop_id for candidate in nearby)
        route_ids = frozenset(
            route_id for candidate in nearby for route_id in schedule.stop_routes[candidate.stop_id]
        )
        scope = f"stops within {ALERT_NEARBY_METERS} m"
    else:
        scope = "the CATS system"

    current = [alert for alert in alerts_entry.value if not alert.has_ended(now)]
    selected = (
        current
        if stop_ids is None or route_ids is None
        else alerts_for(current, stop_ids, route_ids)
    )
    selected.sort(key=lambda alert: (not alert.is_active(now), alert.header_text or ""))

    return _drop_none(
        {
            "scope": scope,
            "stop": located_json,
            "matchedRoutes": matched_routes,
            "alertCount": len(selected),
            "alerts": [_alert_json(schedule, alert, now) for alert in selected],
            "message": None if selected else f"No current or upcoming service alerts for {scope}.",
            "feedAgeSeconds": max(0, round(now - alerts_entry.fetched_at)),
        }
    )


def _endpoint(
    schedule: Schedule,
    *,
    label: str,
    stop: str | None,
    latitude: float | None,
    longitude: float | None,
    max_walk_meters: int,
) -> tuple[dict[str, int], tuple[float, float], str | None] | ToolResult:
    """Stops a trip can start or end at, with the walk to each, plus the point itself."""
    if stop is not None and (latitude is not None or longitude is not None):
        return {"error": f"Give the {label} as a stop or as a location, not both."}
    if (latitude is None) != (longitude is None):
        return {"error": f"Give the {label} latitude and longitude together."}
    if stop is not None:
        stops = find_stops(schedule, stop)
        if not stops:
            return {
                "error": f"No stop matched {json.dumps(stop)} for the {label}.",
                "hint": 'Try a stop id like "02400", or part of a stop name like "Beatties Ford".',
            }
        station = station_for(schedule, stops[0])
        primary = station.primary
        return (
            dict.fromkeys(station.stop_ids, 0),
            (primary.latitude, primary.longitude),
            primary.name,
        )
    if latitude is None or longitude is None:
        return {"error": f'Give the {label}: a "stop", or a latitude and longitude.'}
    nearby: dict[str, int] = {}
    for candidate in by_distance(schedule.stops.values(), latitude, longitude):
        if candidate.stop_id not in schedule.stop_routes:
            continue
        meters = planner.walk_meters(latitude, longitude, candidate.latitude, candidate.longitude)
        if meters > max_walk_meters:
            break
        nearby[candidate.stop_id] = meters
        if len(nearby) >= _MAX_ENDPOINT_STOPS:
            break
    if not nearby:
        return {
            "error": f"No CATS stop is within a {max_walk_meters} m walk of the {label}.",
            "hint": f'Raise "max_walk_meters" (up to {MAX_WALK_METERS}), or check the coordinates.',
        }
    return nearby, (latitude, longitude), None


def _stop_point_json(
    schedule: Schedule, stop_id: str | None, fallback: str | None
) -> dict[str, Any]:
    if stop_id is None or stop_id not in schedule.stops:
        return {"name": fallback or "Your location"}
    found = schedule.stops[stop_id]
    return {
        "stopId": found.stop_id,
        "name": found.name,
        "latitude": found.latitude,
        "longitude": found.longitude,
    }


def _itinerary_json(
    schedule: Schedule,
    itinerary: planner.Itinerary,
    origin_name: str | None,
    destination_name: str | None,
) -> dict[str, Any]:
    zone = schedule.zone
    legs: list[dict[str, Any]] = []
    #: When the rider got off the previous vehicle, to report the wait at a transfer.
    previous_ride_arrival: int | None = None
    for leg in itinerary.legs:
        if isinstance(leg, planner.WalkLeg):
            legs.append(
                {
                    "type": "walk",
                    "from": _stop_point_json(schedule, leg.from_stop, origin_name),
                    "to": _stop_point_json(schedule, leg.to_stop, destination_name),
                    "meters": leg.meters,
                    "minutes": max(1, round((leg.arrives_at - leg.departs_at) / 60)),
                    "departAt": iso_local(leg.departs_at, zone),
                    "arriveAt": iso_local(leg.arrives_at, zone),
                }
            )
        else:
            trip = schedule.trips[leg.trip_id]
            route = schedule.routes.get(trip.route_id)
            legs.append(
                _drop_none(
                    {
                        "type": "ride",
                        "mode": None if route is None else route.mode,
                        "route": None if route is None else _route_json(route),
                        "destination": destination_of(schedule, leg.trip_id),
                        "headsign": informative_headsign(trip),
                        "from": _stop_point_json(schedule, leg.board_stop, None),
                        "to": _stop_point_json(schedule, leg.alight_stop, None),
                        "departAt": iso_local(leg.departs_at, zone),
                        "arriveAt": iso_local(leg.arrives_at, zone),
                        "scheduledDepartAt": iso_local(leg.scheduled_departs_at, zone)
                        if leg.scheduled_departs_at != leg.departs_at
                        else None,
                        "scheduledArriveAt": iso_local(leg.scheduled_arrives_at, zone)
                        if leg.scheduled_arrives_at != leg.arrives_at
                        else None,
                        "stopsTravelled": leg.stops_travelled,
                        "transferWaitMinutes": None
                        if previous_ride_arrival is None
                        else max(0, round((leg.departs_at - previous_ride_arrival) / 60)),
                        "live": leg.live,
                        "tripId": leg.trip_id,
                    }
                )
            )
            previous_ride_arrival = leg.arrives_at
    return {
        "departAt": iso_local(itinerary.departs_at, zone),
        "arriveAt": iso_local(itinerary.arrives_at, zone),
        "durationMinutes": round((itinerary.arrives_at - itinerary.departs_at) / 60),
        "transfers": max(0, len(itinerary.rides) - 1),
        "walkMeters": itinerary.walk_meters,
        "legs": legs,
    }


async def plan_trip(
    deps: Dependencies,
    *,
    origin_stop: str | None = None,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    destination_stop: str | None = None,
    destination_latitude: float | None = None,
    destination_longitude: float | None = None,
    date: str | None = None,
    depart_at: str | None = None,
    arrive_by: str | None = None,
    max_transfers: int | None = None,
    max_walk_meters: int | None = None,
) -> ToolResult:
    """Plan trips between two places by bus and train, with walking at either end."""
    if depart_at is not None and arrive_by is not None:
        return {"error": 'Provide "depart_at" or "arrive_by", not both.'}
    depart_seconds, arrive_seconds = _clock(depart_at, "depart_at"), _clock(arrive_by, "arrive_by")
    for parsed in (depart_seconds, arrive_seconds):
        if isinstance(parsed, dict):
            return parsed
    walk_limit = min(max_walk_meters or DEFAULT_WALK_METERS, MAX_WALK_METERS)
    transfers = DEFAULT_TRANSFERS if max_transfers is None else min(max_transfers, MAX_TRANSFERS)

    schedule = (await deps.load_schedule()).value
    now = deps.now()
    zone = schedule.zone
    day = _service_date(schedule, date, now)
    if isinstance(day, dict):
        return day
    is_today = day == local_date(now, zone)
    if not is_today and depart_seconds is None and arrive_seconds is None:
        return {"error": 'For a day other than today, give "depart_at" or "arrive_by".'}

    origin = _endpoint(
        schedule,
        label="starting point",
        stop=origin_stop,
        latitude=origin_latitude,
        longitude=origin_longitude,
        max_walk_meters=walk_limit,
    )
    if isinstance(origin, dict):
        return origin
    destination = _endpoint(
        schedule,
        label="destination",
        stop=destination_stop,
        latitude=destination_latitude,
        longitude=destination_longitude,
        max_walk_meters=walk_limit,
    )
    if isinstance(destination, dict):
        return destination
    origin_stops, origin_point, origin_name = origin
    destination_stops, destination_point, destination_name = destination
    if set(origin_stops) & set(destination_stops) and (origin_stop or destination_stop):
        return {"error": "The starting point and destination are the same stop."}

    service_origin = service_day_origin(day, zone)
    depart_epoch: int | None = None
    arrive_epoch: int | None = None
    if isinstance(arrive_seconds, int):
        arrive_epoch = service_origin + arrive_seconds
    elif isinstance(depart_seconds, int):
        depart_epoch = service_origin + depart_seconds
    else:
        depart_epoch = int(now)

    notes = [
        "Walking distances and times are estimates from straight-line distance; "
        "the CATS feed has no street map."
    ]
    delays: planner.LiveDelays = {}
    searched_at = depart_epoch if depart_epoch is not None else arrive_epoch or 0
    if abs(searched_at - now) <= planner.SEARCH_WINDOW_SECONDS:
        try:
            updates = await deps.feeds.trip_updates()
            delays = planner.live_delays(schedule, updates.value, now)
        except Exception as error:
            _logger.warning("trip updates unavailable for plan_trip: %s", error)
            notes.append("Live predictions are unavailable, so times are from the timetable.")

    itineraries = await asyncio.to_thread(
        planner.plan,
        schedule,
        origin_stops,
        destination_stops,
        depart_at=depart_epoch,
        arrive_by=arrive_epoch,
        max_rides=transfers + 1,
        delays=delays,
    )

    if any(ride.live for itinerary in itineraries for ride in itinerary.rides):
        notes.append('Rides marked "live" include delays predicted from the vehicle\'s position.')

    direct_meters = planner.walk_meters(*origin_point, *destination_point)
    walk_only = (
        {"meters": direct_meters, "minutes": round(planner.walk_seconds(direct_meters) / 60)}
        if direct_meters <= walk_limit
        else None
    )
    message: str | None = None
    if not itineraries:
        window_hours = planner.SEARCH_WINDOW_SECONDS // 3600
        message = (
            f"No trip with at most {transfers} transfer{'s' if transfers != 1 else ''} was found "
            f"within {window_hours} hours of that time. Try another time, more transfers, "
            "or a longer walk."
        )

    return _drop_none(
        {
            "serviceDate": day.isoformat(),
            "searchedFor": {
                "departAt" if depart_epoch is not None else "arriveBy": iso_local(
                    depart_epoch if depart_epoch is not None else arrive_epoch or 0, zone
                )
            },
            "itineraries": [
                _itinerary_json(schedule, itinerary, origin_name, destination_name)
                for itinerary in itineraries
            ],
            "walkingIsAnOption": walk_only,
            "message": message,
            "notes": notes,
            "scheduleCovers": _coverage_json(schedule),
        }
    )
