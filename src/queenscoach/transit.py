"""Domain layer: joins the realtime feeds to the static schedule and resolves
the human-friendly queries the tools accept ("9", "Blue Line", "CTC").
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .realtime import ServiceAlert, TripUpdate, VehiclePosition
from .static_gtfs import Mode, Route, Schedule, Stop

_METERS_PER_SECOND_TO_MPH = 2.2369363
_EARTH_RADIUS_METERS = 6_371_000

VehicleMode = Literal["bus", "train", "unknown"]


@dataclass(frozen=True, slots=True)
class ResolvedRouteRef:
    route_id: str
    name: str
    long_name: str
    mode: Mode


@dataclass(frozen=True, slots=True)
class NextStop:
    stop_id: str
    name: str | None
    arrival_time: str
    minutes_away: int


@dataclass(frozen=True, slots=True)
class VehicleView:
    #: Vehicle number shown on the bus or train.
    vehicle: str
    mode: VehicleMode
    route: ResolvedRouteRef | None
    headsign: str | None
    trip_id: str | None
    latitude: float
    longitude: float
    bearing_degrees: float | None
    speed_mph: float | None
    occupancy: str | None
    reported_at: str | None
    position_age_seconds: int | None
    next_stop: NextStop | None


@dataclass(frozen=True, slots=True)
class ArrivalView:
    route: ResolvedRouteRef | None
    headsign: str | None
    vehicle: str | None
    trip_id: str | None
    arrival_time: str
    minutes_away: int
    scheduled_arrival_time: str | None
    #: Positive means running late. ``None`` when the feed gives no schedule.
    schedule_deviation_minutes: int | None
    vehicle_position: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class TransitSnapshot:
    schedule: Schedule
    vehicles: Sequence[VehiclePosition]
    trip_updates: Sequence[TripUpdate]
    #: Unix time, in seconds, that this snapshot describes.
    now: float


@dataclass(frozen=True, slots=True)
class _Upcoming:
    stop_id: str
    arrival_time: int


def normalize(text: str) -> str:
    """Casefold and collapse whitespace, for comparing user queries to feed text."""
    return re.sub(r"\s+", " ", text.strip().lower())


def natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key that orders embedded numbers numerically, so 9 precedes 29 and 501."""
    return tuple(
        (1, int(part), "") if part.isdigit() else (0, 0, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


def iso_time(epoch_seconds: float) -> str:
    """Format Unix seconds as an ISO 8601 UTC timestamp, e.g. ``2026-01-09T14:03:00.000Z``."""
    stamp = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_iso_time(epoch_seconds: float | None) -> str | None:
    """Like :func:`iso_time`, but passes ``None`` through."""
    return None if epoch_seconds is None else iso_time(epoch_seconds)


def _minutes_between(epoch_seconds: float, now: float) -> int:
    return round((epoch_seconds - now) / 60)


def distance_meters(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> float:
    """Great-circle distance in meters."""
    delta_lat = math.radians(to_lat - from_lat)
    delta_lon = math.radians(to_lon - from_lon)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(math.radians(from_lat))
        * math.cos(math.radians(to_lat))
        * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(a)))


def _to_route_ref(route: Route | None) -> ResolvedRouteRef | None:
    if route is None:
        return None
    return ResolvedRouteRef(
        route_id=route.route_id,
        name=route.short_name,
        long_name=route.long_name,
        mode=route.mode,
    )


def find_routes(schedule: Schedule, query: str) -> list[Route]:
    """Resolve a user-supplied route query against the schedule.

    Matching is exact-first (``route_id``, then ``route_short_name``) so a query
    of "5" cannot be captured by "Route 501"; substring matching on the long
    name is the last resort.
    """
    needle = normalize(query)
    if not needle:
        return []
    routes = list(schedule.routes.values())

    for exact in (
        [route for route in routes if normalize(route.route_id) == needle],
        [route for route in routes if normalize(route.short_name) == needle],
        [route for route in routes if normalize(route.long_name) == needle],
    ):
        if exact:
            return exact

    return [
        route
        for route in routes
        if needle in normalize(route.long_name) or needle in normalize(route.short_name)
    ]


def find_stops(schedule: Schedule, query: str, limit: int = 10) -> list[Stop]:
    """Resolve a user-supplied stop query.

    Exact ``stop_id``/``stop_code`` first, then exact name, then a substring
    match ranked by name length so the closest match to the query leads.
    """
    needle = normalize(query)
    if not needle:
        return []

    direct = schedule.stops.get(query.strip())
    if direct is not None:
        return [direct]

    stops = list(schedule.stops.values())
    by_code = [stop for stop in stops if stop.code is not None and normalize(stop.code) == needle]
    if by_code:
        return by_code[:limit]

    by_name = [stop for stop in stops if normalize(stop.name) == needle]
    if by_name:
        return by_name[:limit]

    matches = [stop for stop in stops if needle in normalize(stop.name)]
    matches.sort(key=lambda stop: (len(stop.name), stop.name))
    return matches[:limit]


def matches_vehicle_query(vehicle: VehiclePosition, query: str) -> bool:
    """Match a vehicle query against its label, descriptor id, or entity id."""
    needle = normalize(query)
    if not needle:
        return False
    candidates = (vehicle.vehicle_label, vehicle.vehicle_id, vehicle.entity_id)
    return any(value is not None and normalize(value) == needle for value in candidates)


def _build_next_stop_index(trip_updates: Iterable[TripUpdate], now: float) -> dict[str, _Upcoming]:
    """Index the soonest upcoming stop per trip, from the TripUpdates feed.

    This is the only trustworthy source of a vehicle's next stop: the
    VehiclePositions feed's own ``stop_id`` and ``current_stop_sequence`` do not
    match the published schedule (see the note in :mod:`queenscoach.realtime`).
    """
    index: dict[str, _Upcoming] = {}
    for update in trip_updates:
        if update.trip_id is None:
            continue
        best: _Upcoming | None = None
        for stop_time in update.stop_time_updates:
            stop_id = stop_time.stop_id
            arrival_time = stop_time.arrival_time
            if stop_id is None or arrival_time is None or arrival_time < now:
                continue
            if best is None or arrival_time < best.arrival_time:
                best = _Upcoming(stop_id=stop_id, arrival_time=arrival_time)
        if best is not None:
            index[update.trip_id] = best
    return index


def to_vehicle_view(
    vehicle: VehiclePosition,
    snapshot: TransitSnapshot,
    next_stop_index: dict[str, _Upcoming],
) -> VehicleView:
    """Build the enriched, rider-facing view of a single vehicle."""
    schedule = snapshot.schedule
    trip = None if vehicle.trip_id is None else schedule.trips.get(vehicle.trip_id)
    # Prefer the route the vehicle reports; fall back to the one on its trip.
    route_id = vehicle.route_id or (trip.route_id if trip is not None else None)
    route = None if route_id is None else schedule.routes.get(route_id)

    upcoming = None if vehicle.trip_id is None else next_stop_index.get(vehicle.trip_id)
    next_stop = (
        None
        if upcoming is None
        else NextStop(
            stop_id=upcoming.stop_id,
            name=(
                schedule.stops[upcoming.stop_id].name
                if upcoming.stop_id in schedule.stops
                else None
            ),
            arrival_time=iso_time(upcoming.arrival_time),
            minutes_away=_minutes_between(upcoming.arrival_time, snapshot.now),
        )
    )

    speed = vehicle.speed_meters_per_second
    return VehicleView(
        vehicle=vehicle.vehicle_label or vehicle.vehicle_id or vehicle.entity_id,
        mode=route.mode if route is not None else "unknown",
        route=_to_route_ref(route),
        headsign=trip.headsign if trip is not None else None,
        trip_id=vehicle.trip_id,
        latitude=vehicle.latitude,
        longitude=vehicle.longitude,
        bearing_degrees=vehicle.bearing_degrees,
        speed_mph=None if speed is None else round(speed * _METERS_PER_SECOND_TO_MPH, 1),
        occupancy=vehicle.occupancy_status,
        reported_at=to_iso_time(vehicle.timestamp),
        position_age_seconds=(
            None if vehicle.timestamp is None else max(0, round(snapshot.now - vehicle.timestamp))
        ),
        next_stop=next_stop,
    )


def to_vehicle_views(
    vehicles: Iterable[VehiclePosition], snapshot: TransitSnapshot
) -> list[VehicleView]:
    """Build vehicle views for many vehicles, sharing one next-stop index."""
    index = _build_next_stop_index(snapshot.trip_updates, snapshot.now)
    return [to_vehicle_view(vehicle, snapshot, index) for vehicle in vehicles]


def arrivals_at_stop(
    stop: Stop,
    snapshot: TransitSnapshot,
    *,
    route_ids: frozenset[str] | None = None,
    mode: Mode | None = None,
) -> list[ArrivalView]:
    """Predicted arrivals at one stop, soonest first."""
    schedule = snapshot.schedule
    now = snapshot.now

    vehicle_by_trip = {
        vehicle.trip_id: vehicle for vehicle in snapshot.vehicles if vehicle.trip_id is not None
    }

    arrivals: list[ArrivalView] = []
    for update in snapshot.trip_updates:
        trip = None if update.trip_id is None else schedule.trips.get(update.trip_id)
        route_id = update.route_id or (trip.route_id if trip is not None else None)
        route = None if route_id is None else schedule.routes.get(route_id)

        if route_ids is not None and (route_id is None or route_id not in route_ids):
            continue
        if mode is not None and (route is None or route.mode != mode):
            continue

        for stop_time in update.stop_time_updates:
            if stop_time.stop_id != stop.stop_id:
                continue
            if stop_time.schedule_relationship == "SKIPPED":
                continue
            arrival_time = stop_time.arrival_time or stop_time.departure_time
            if arrival_time is None or arrival_time < now:
                continue

            vehicle = None if update.trip_id is None else vehicle_by_trip.get(update.trip_id)
            scheduled = stop_time.scheduled_arrival_time

            arrivals.append(
                ArrivalView(
                    route=_to_route_ref(route),
                    headsign=trip.headsign if trip is not None else None,
                    vehicle=(vehicle.vehicle_label if vehicle is not None else None)
                    or update.vehicle_label,
                    trip_id=update.trip_id,
                    arrival_time=iso_time(arrival_time),
                    minutes_away=_minutes_between(arrival_time, now),
                    scheduled_arrival_time=to_iso_time(scheduled),
                    # The feed omits `delay`, so deviation is derived from the two stamps.
                    schedule_deviation_minutes=(
                        None if scheduled is None else round((arrival_time - scheduled) / 60)
                    ),
                    vehicle_position=(
                        None if vehicle is None else (vehicle.latitude, vehicle.longitude)
                    ),
                )
            )

    arrivals.sort(key=lambda arrival: arrival.arrival_time)
    return arrivals


def alerts_for(
    alerts: Iterable[ServiceAlert], stop_id: str, route_ids: frozenset[str]
) -> list[ServiceAlert]:
    """Select alerts naming this stop or any of the given routes."""
    return [
        alert
        for alert in alerts
        if stop_id in alert.informed_stop_ids
        or any(route_id in route_ids for route_id in alert.informed_route_ids)
    ]
