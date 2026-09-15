"""Domain layer for the published timetable: service days, departures, and routes.

:mod:`queenscoach.transit` joins the realtime feeds to the schedule; this module
answers from the schedule alone. GTFS clock times count from a service day's
reference point (noon minus 12 hours, local time), and a trip may run past
midnight into the next calendar day, so every conversion to a real instant goes
through :func:`service_day_origin`.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from .static_gtfs import Mode, Route, Schedule, Trip, TripTimetable

#: Headsigns that name only a direction. 61 of CATS's 64 routes use them, so the
#: rider-facing destination comes from the trip's last stop instead.
_DIRECTION_ONLY = re.compile(
    r"^(inbound|outbound|north|south|east|west|(north|south|east|west)bound)$", re.IGNORECASE
)

_SECONDS_PER_DAY = 86_400


def service_day_origin(day: date, zone: ZoneInfo) -> int:
    """Unix seconds of ``day``'s reference point: local noon minus 12 hours.

    On most days this is local midnight; on a daylight-saving change it is an
    hour off midnight, which is exactly what GTFS clock times assume.
    """
    noon = datetime.combine(day, time(12), tzinfo=zone)
    return int(noon.timestamp()) - 12 * 3600


def local_date(epoch: float, zone: ZoneInfo) -> date:
    return datetime.fromtimestamp(epoch, tz=zone).date()


def iso_local(epoch: float, zone: ZoneInfo) -> str:
    """ISO 8601 in the agency's local time with its offset, e.g. ``2026-09-08T17:59:00-04:00``."""
    return datetime.fromtimestamp(round(epoch), tz=zone).isoformat()


def parse_clock(raw: str) -> int | None:
    """Seconds after midnight for ``HH:MM``; hours up to 47 reach into the next night."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 47 or minutes > 59:
        return None
    return hours * 3600 + minutes * 60


def parse_service_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def destination_of(schedule: Schedule, trip_id: str) -> str | None:
    """The name of the last stop a trip calls at."""
    timetable = schedule.timetables.get(trip_id)
    if timetable is None:
        return None
    stop = schedule.stops.get(timetable.stop_ids[-1])
    return None if stop is None else stop.name


def informative_headsign(trip: Trip) -> str | None:
    """The trip's headsign, unless it names only a direction such as "Outbound"."""
    headsign = trip.headsign
    if headsign is None or _DIRECTION_ONLY.match(headsign.strip()):
        return None
    return headsign


def trips_running(
    schedule: Schedule,
    day: date,
    *,
    route_ids: frozenset[str] | None = None,
    mode: Mode | None = None,
) -> list[tuple[Trip, TripTimetable]]:
    """Trips whose service runs on ``day``, with their timetables."""
    services = schedule.calendar.services_on(day)
    if not services:
        return []
    running: list[tuple[Trip, TripTimetable]] = []
    for trip_id, timetable in schedule.timetables.items():
        trip = schedule.trips[trip_id]
        if trip.service_id not in services:
            continue
        if route_ids is not None and trip.route_id not in route_ids:
            continue
        if mode is not None:
            route = schedule.routes.get(trip.route_id)
            if route is None or route.mode != mode:
                continue
        running.append((trip, timetable))
    return running


@dataclass(frozen=True, slots=True)
class Departure:
    trip: Trip
    route: Route | None
    stop_id: str
    #: The service date the trip belongs to, which is the day before for a 1 am call.
    service_date: date
    #: Unix seconds.
    departs_at: int
    destination: str | None


def departures_at(
    schedule: Schedule,
    stop_ids: frozenset[str],
    service_dates: Iterable[date],
    *,
    route_ids: frozenset[str] | None = None,
    mode: Mode | None = None,
) -> list[Departure]:
    """Scheduled boardings at any of ``stop_ids`` on the given service dates, soonest first.

    A trip's last call is not a departure, nor is a call that allows no pickup.
    """
    zone = schedule.zone
    departures: list[Departure] = []
    for service_date in service_dates:
        origin = service_day_origin(service_date, zone)
        for trip, timetable in trips_running(
            schedule, service_date, route_ids=route_ids, mode=mode
        ):
            for index, stop_id in enumerate(timetable.stop_ids):
                if stop_id not in stop_ids or not timetable.boards_at(index):
                    continue
                departures.append(
                    Departure(
                        trip=trip,
                        route=schedule.routes.get(trip.route_id),
                        stop_id=stop_id,
                        service_date=service_date,
                        departs_at=origin + timetable.departures[index],
                        destination=destination_of(schedule, trip.trip_id),
                    )
                )
    departures.sort(key=lambda departure: (departure.departs_at, departure.trip.trip_id))
    return departures


@dataclass(frozen=True, slots=True)
class ServicePeriod:
    name: str
    #: Seconds after the service day's reference point: [start, end).
    start: int
    end: int


#: The parts of a service day a rider plans around.
SERVICE_PERIODS = (
    ServicePeriod("early morning", 0, 6 * 3600),
    ServicePeriod("morning rush", 6 * 3600, 9 * 3600),
    ServicePeriod("midday", 9 * 3600, 15 * 3600),
    ServicePeriod("afternoon rush", 15 * 3600, 19 * 3600),
    ServicePeriod("evening", 19 * 3600, 22 * 3600),
    ServicePeriod("late night", 22 * 3600, 2 * _SECONDS_PER_DAY),
)


@dataclass(frozen=True, slots=True)
class Frequency:
    period: ServicePeriod
    trips: int
    #: Median minutes between consecutive departures, or ``None`` with fewer than two.
    typical_minutes_between: int | None


def frequencies(start_times: Sequence[int]) -> list[Frequency]:
    """How many trips leave in each service period, and how far apart they typically are.

    Gaps are measured only between trips leaving in the same period, so a first
    morning trip does not report the overnight wait as its frequency.
    """
    ordered = sorted(start_times)
    result: list[Frequency] = []
    for period in SERVICE_PERIODS:
        starts = [start for start in ordered if period.start <= start < period.end]
        if not starts:
            continue
        gaps = [later - earlier for earlier, later in pairwise(starts)]
        result.append(
            Frequency(
                period=period,
                trips=len(starts),
                typical_minutes_between=round(statistics.median(gaps) / 60) if gaps else None,
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class StopPattern:
    """One sequence of stops that some of a route's trips follow."""

    stop_ids: tuple[str, ...]
    trips: tuple[tuple[Trip, TripTimetable], ...]

    @property
    def start_times(self) -> list[int]:
        return [timetable.departures[0] for _, timetable in self.trips]


@dataclass(frozen=True, slots=True)
class Direction:
    #: GTFS ``direction_id``, or ``None`` when the feed gives none.
    direction_id: str | None
    trips: tuple[tuple[Trip, TripTimetable], ...]
    #: Most-travelled pattern first.
    patterns: tuple[StopPattern, ...]

    @property
    def main_pattern(self) -> StopPattern:
        return self.patterns[0]


def route_directions(trips: Iterable[tuple[Trip, TripTimetable]]) -> list[Direction]:
    """Group a route's trips by direction, and each direction's trips by stop pattern."""
    by_direction: dict[str | None, list[tuple[Trip, TripTimetable]]] = {}
    for trip, timetable in trips:
        by_direction.setdefault(trip.direction_id, []).append((trip, timetable))

    directions: list[Direction] = []
    for direction_id, members in by_direction.items():
        by_pattern: dict[tuple[str, ...], list[tuple[Trip, TripTimetable]]] = {}
        for trip, timetable in members:
            by_pattern.setdefault(timetable.stop_ids, []).append((trip, timetable))
        patterns = sorted(
            (
                StopPattern(stop_ids=stops, trips=tuple(group))
                for stops, group in by_pattern.items()
            ),
            key=lambda pattern: (-len(pattern.trips), -len(pattern.stop_ids)),
        )
        directions.append(
            Direction(direction_id=direction_id, trips=tuple(members), patterns=tuple(patterns))
        )
    directions.sort(key=lambda direction: (direction.direction_id is None, direction.direction_id))
    return directions


def most_common_destination(
    schedule: Schedule, trips: Iterable[tuple[Trip, TripTimetable]]
) -> str | None:
    names = Counter(
        schedule.stops[timetable.stop_ids[-1]].name
        for _, timetable in trips
        if timetable.stop_ids[-1] in schedule.stops
    )
    return names.most_common(1)[0][0] if names else None


def service_dates_from(schedule: Schedule, start: date, days: int) -> list[date]:
    """``start`` and the following days, clipped to the dates the calendar covers."""
    last = schedule.calendar.last_date
    return [
        start + timedelta(days=offset)
        for offset in range(days)
        if last is not None and start + timedelta(days=offset) <= last
    ]
