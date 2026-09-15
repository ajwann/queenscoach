"""Trip planning over the published timetable, adjusted by live delays where known.

This is the Connection Scan Algorithm: every hop a trip makes between two
consecutive stops is a *connection*, and scanning connections in departure
order finds the earliest arrival at every stop in one pass. Labels are kept per
number of rides taken, so one scan also yields the fastest journey with one
ride, with two, and so on, and a rider can trade a transfer for a few minutes.

"Arrive by" runs the same scan backwards in time: connections reversed and
ordered by descending arrival, which finds the latest departure instead.

Walking is estimated, not routed. The feed has no street network, so a walk is
the straight-line distance stretched by :data:`WALK_DETOUR`, at
:data:`WALK_SPEED_MPS`.
"""

from __future__ import annotations

import bisect
import math
import threading
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from .realtime import TripUpdate
from .static_gtfs import Schedule
from .timetable import local_date, service_day_origin, trips_running
from .transit import distance_meters

#: Typical walking speed, about 4.5 km/h.
WALK_SPEED_MPS = 1.25
#: Streets are not straight lines; this stretches a crow-flies distance to a walk.
WALK_DETOUR = 1.3
#: Farthest walk between two stops considered as a transfer.
TRANSFER_WALK_METERS = 400
#: Slack added to every transfer, for finding the stop and a vehicle leaving on time.
TRANSFER_BUFFER_SECONDS = 120
#: How far past the requested time a search looks.
SEARCH_WINDOW_SECONDS = 3 * 3600
#: A live prediction only applies to a trip scheduled within this long of now.
_LIVE_TRIP_SLACK_SECONDS = 2 * 3600

_UNREACHED = 1 << 62

_ACCESS, _WALK, _RIDE = 0, 1, 2

#: How a stop was reached: (_ACCESS, 0, 0), (_WALK, from stop, meters), or
#: (_RIDE, boarding connection, alighting connection).
_Parent = tuple[int, int, int]

#: One step of a traced journey: (_WALK, from stop, to stop, meters) or
#: (_RIDE, boarding connection, alighting connection, 0).
_Step = tuple[int, int, int, int]

#: A connection as scanned: (departs, arrives, from stop, to stop, trip key, boardable, alightable).
_Scanned = tuple[int, int, int, int, int, int, int]


def walk_meters(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> int:
    """Estimated walking distance between two points."""
    return math.ceil(distance_meters(from_lat, from_lon, to_lat, to_lon) * WALK_DETOUR)


def walk_seconds(meters: int) -> int:
    return max(1, math.ceil(meters / WALK_SPEED_MPS))


@dataclass(frozen=True, slots=True)
class WalkLeg:
    #: ``None`` for the rider's own starting point or destination.
    from_stop: str | None
    to_stop: str | None
    meters: int
    departs_at: int
    arrives_at: int


@dataclass(frozen=True, slots=True)
class RideLeg:
    trip_id: str
    service_date: date
    board_stop: str
    alight_stop: str
    #: How many stops the rider travels, counting the one they get off at.
    stops_travelled: int
    #: Unix seconds, including any live delay.
    departs_at: int
    arrives_at: int
    scheduled_departs_at: int
    scheduled_arrives_at: int
    #: Whether a live prediction adjusted these times.
    live: bool


Leg = WalkLeg | RideLeg


@dataclass(frozen=True, slots=True)
class Itinerary:
    legs: tuple[Leg, ...]

    @property
    def departs_at(self) -> int:
        return self.legs[0].departs_at

    @property
    def arrives_at(self) -> int:
        return self.legs[-1].arrives_at

    @property
    def rides(self) -> tuple[RideLeg, ...]:
        return tuple(leg for leg in self.legs if isinstance(leg, RideLeg))

    @property
    def walk_meters(self) -> int:
        return sum(leg.meters for leg in self.legs if isinstance(leg, WalkLeg))

    def key(self) -> tuple[tuple[str, date, str, str], ...]:
        return tuple(
            (ride.trip_id, ride.service_date, ride.board_stop, ride.alight_stop)
            for ride in self.rides
        )


#: Per-call live delay in seconds for a trip on a service date.
LiveDelays = dict[tuple[str, date], array[int]]


def live_delays(schedule: Schedule, updates: Iterable[TripUpdate], now: float) -> LiveDelays:
    """Turn TripUpdates into a delay for every call of each predicted trip.

    A prediction's delay carries forward to later calls until the next
    prediction; calls before the first prediction keep the schedule. The
    service date is the one on which the trip is scheduled to be running now,
    since the same trip id runs on many dates.
    """
    zone = schedule.zone
    today = local_date(now, zone)
    delays: LiveDelays = {}
    for update in updates:
        if update.trip_id is None:
            continue
        timetable = schedule.timetables.get(update.trip_id)
        trip = schedule.trips.get(update.trip_id)
        if timetable is None or trip is None:
            continue
        service_date = None
        for candidate in (today, today - timedelta(days=1)):
            if trip.service_id not in schedule.calendar.services_on(candidate):
                continue
            origin = service_day_origin(candidate, zone)
            if (
                origin + timetable.departures[0] - _LIVE_TRIP_SLACK_SECONDS
                <= now
                <= origin + timetable.arrivals[-1] + _LIVE_TRIP_SLACK_SECONDS
            ):
                service_date = candidate
                break
        if service_date is None:
            continue
        origin = service_day_origin(service_date, zone)

        predicted: dict[int, int] = {}
        for stop_time in update.stop_time_updates:
            index = _call_index(timetable.stop_sequences, timetable.stop_ids, stop_time)
            live_time = stop_time.departure_time or stop_time.arrival_time
            if index is None or live_time is None:
                continue
            predicted[index] = live_time - (origin + timetable.departures[index])
        if not predicted:
            continue

        per_call = array("i", bytes(4 * len(timetable.stop_ids)))
        carried = 0
        for index in range(len(timetable.stop_ids)):
            carried = predicted.get(index, carried)
            per_call[index] = carried
        delays[(update.trip_id, service_date)] = per_call
    return delays


def _call_index(sequences: array[int], stop_ids: tuple[str, ...], stop_time: object) -> int | None:
    sequence = getattr(stop_time, "stop_sequence", None)
    if sequence is not None:
        position = bisect.bisect_left(sequences, sequence)
        if position < len(sequences) and sequences[position] == sequence:
            return position
    stop_id = getattr(stop_time, "stop_id", None)
    if stop_id is not None and stop_id in stop_ids:
        return stop_ids.index(stop_id)
    return None


@dataclass(frozen=True, slots=True)
class _DayConnections:
    """One service day's connections, sorted by scheduled departure."""

    #: Seconds after the service day's reference point.
    departures: array[int]
    arrivals: array[int]
    from_stops: array[int]
    to_stops: array[int]
    trips: array[int]
    #: Index of the departing call within its trip.
    calls: array[int]
    boardable: bytes
    alightable: bytes


class _Network:
    """What planning needs from one schedule, built once and reused across searches."""

    def __init__(self, schedule: Schedule) -> None:
        self.schedule = schedule
        self.stop_ids = sorted(schedule.stop_routes)
        self.stop_index = {stop_id: index for index, stop_id in enumerate(self.stop_ids)}
        self.trip_ids = sorted(schedule.timetables)
        self.trip_index = {trip_id: index for index, trip_id in enumerate(self.trip_ids)}
        self.transfers = self._transfers()
        self._days: dict[date, _DayConnections] = {}
        self._lock = threading.Lock()

    def _transfers(self) -> list[list[tuple[int, int, int]]]:
        """Walkable stop pairs, as (to stop, seconds, meters), bucketed on a grid."""
        stops = self.schedule.stops
        cell = 0.005  # degrees; about 550 m of latitude, wider than the transfer radius
        grid: dict[tuple[int, int], list[int]] = {}
        located: list[tuple[float, float] | None] = []
        for index, stop_id in enumerate(self.stop_ids):
            stop = stops.get(stop_id)
            if stop is None:
                located.append(None)
                continue
            located.append((stop.latitude, stop.longitude))
            key = (math.floor(stop.latitude / cell), math.floor(stop.longitude / cell))
            grid.setdefault(key, []).append(index)

        transfers: list[list[tuple[int, int, int]]] = [[] for _ in self.stop_ids]
        for index, position in enumerate(located):
            if position is None:
                continue
            row, column = math.floor(position[0] / cell), math.floor(position[1] / cell)
            for d_row in (-1, 0, 1):
                for d_column in (-1, 0, 1):
                    for other in grid.get((row + d_row, column + d_column), ()):
                        other_position = located[other]
                        if other == index or other_position is None:
                            continue
                        meters = walk_meters(*position, *other_position)
                        if meters <= TRANSFER_WALK_METERS:
                            transfers[index].append((other, walk_seconds(meters), meters))
        return transfers

    def day(self, service_date: date) -> _DayConnections:
        with self._lock:
            cached = self._days.get(service_date)
            if cached is not None:
                return cached
        rows: list[tuple[int, int, int, int, int, int, int, int]] = []
        for trip, timetable in trips_running(self.schedule, service_date):
            trip_number = self.trip_index[trip.trip_id]
            stops = timetable.stop_ids
            for call in range(len(stops) - 1):
                rows.append(
                    (
                        timetable.departures[call],
                        max(timetable.arrivals[call + 1], timetable.departures[call]),
                        self.stop_index[stops[call]],
                        self.stop_index[stops[call + 1]],
                        trip_number,
                        call,
                        1 if timetable.boards_at(call) else 0,
                        1 if timetable.alights_at(call + 1) else 0,
                    )
                )
        rows.sort()
        built = _DayConnections(
            departures=array("i", (row[0] for row in rows)),
            arrivals=array("i", (row[1] for row in rows)),
            from_stops=array("i", (row[2] for row in rows)),
            to_stops=array("i", (row[3] for row in rows)),
            trips=array("i", (row[4] for row in rows)),
            calls=array("i", (row[5] for row in rows)),
            boardable=bytes(row[6] for row in rows),
            alightable=bytes(row[7] for row in rows),
        )
        with self._lock:
            # Keep a few days: a search touches today and yesterday's late trips.
            if len(self._days) >= 4:
                self._days.pop(next(iter(self._days)))
            self._days[service_date] = built
        return built


_network_lock = threading.Lock()
_network: _Network | None = None


def _network_for(schedule: Schedule) -> _Network:
    global _network
    with _network_lock:
        if _network is None or _network.schedule is not schedule:
            _network = _Network(schedule)
        return _network


@dataclass(frozen=True, slots=True)
class _Source:
    """Where a prepared connection came from, to rebuild legs afterwards."""

    service_date: date
    trip: int
    call: int
    live: bool


def _prepare(
    network: _Network,
    window_start: int,
    window_end: int,
    delays: LiveDelays,
) -> tuple[list[_Scanned], list[_Source]]:
    """Connections departing within the window, across the service days that reach it."""
    schedule = network.schedule
    zone = schedule.zone
    first = local_date(window_start, zone) - timedelta(days=1)
    last = local_date(window_end, zone)
    connections: list[_Scanned] = []
    sources: list[_Source] = []
    service_date = first
    day_number = 0
    while service_date <= last:
        if schedule.calendar.services_on(service_date):
            origin = service_day_origin(service_date, zone)
            day = network.day(service_date)
            # Delays can pull a departure earlier, so look a little before the window.
            lo = bisect.bisect_left(day.departures, window_start - origin - 1800)
            hi = bisect.bisect_right(day.departures, window_end - origin)
            for position in range(lo, hi):
                trip = day.trips[position]
                call = day.calls[position]
                shift = delays.get((network.trip_ids[trip], service_date))
                departs = origin + day.departures[position]
                arrives = origin + day.arrivals[position]
                if shift is not None:
                    departs += shift[call]
                    arrives = max(departs, arrives + shift[call + 1])
                if departs < window_start or departs > window_end:
                    continue
                sources.append(
                    _Source(service_date=service_date, trip=trip, call=call, live=shift is not None)
                )
                connections.append(
                    (
                        departs,
                        arrives,
                        day.from_stops[position],
                        day.to_stops[position],
                        # The same trip id runs on many dates; each date is its own vehicle.
                        trip * 8 + day_number,
                        day.boardable[position],
                        day.alightable[position],
                    )
                )
        service_date += timedelta(days=1)
        day_number += 1
    return connections, sources


@dataclass(slots=True)
class _Labels:
    #: Per ride count, per stop: the earliest time reached (in scan time).
    best: list[list[int]]
    parents: list[dict[int, _Parent]]


def _scan(
    order: Sequence[int],
    connections: Sequence[_Scanned],
    stop_count: int,
    access: dict[int, int],
    transfers: list[list[tuple[int, int, int]]],
    max_rides: int,
) -> _Labels:
    """Earliest arrival at every stop, for each number of rides up to ``max_rides``."""
    best = [[_UNREACHED] * stop_count for _ in range(max_rides + 1)]
    parents: list[dict[int, _Parent]] = [{} for _ in range(max_rides + 1)]
    for stop, reached in access.items():
        best[0][stop] = reached
        parents[0][stop] = (_ACCESS, 0, 0)
    #: Per ride count, the connection each trip was boarded at.
    boarded: list[dict[int, int]] = [{} for _ in range(max_rides + 1)]

    for position in order:
        departs, arrives, from_stop, to_stop, trip, boardable, alightable = connections[position]
        for rides in range(1, max_rides + 1):
            entered = boarded[rides].get(trip)
            if entered is None:
                ready = best[rides - 1][from_stop]
                if not boardable or ready == _UNREACHED:
                    continue
                if rides > 1:
                    ready += TRANSFER_BUFFER_SECONDS
                if ready > departs:
                    continue
                entered = position
                boarded[rides][trip] = position
            if not alightable or arrives >= best[rides][to_stop]:
                continue
            best[rides][to_stop] = arrives
            parents[rides][to_stop] = (_RIDE, entered, position)
            for neighbour, seconds, meters in transfers[to_stop]:
                walked = arrives + seconds
                if walked < best[rides][neighbour]:
                    best[rides][neighbour] = walked
                    parents[rides][neighbour] = (_WALK, to_stop, meters)
    return _Labels(best=best, parents=parents)


def plan(
    schedule: Schedule,
    origin: dict[str, int],
    destination: dict[str, int],
    *,
    depart_at: int | None = None,
    arrive_by: int | None = None,
    max_rides: int = 3,
    delays: LiveDelays | None = None,
    limit: int = 3,
) -> list[Itinerary]:
    """Find up to ``limit`` itineraries between two sets of stops.

    Args:
        origin: Stop ids the rider can start from, with the walk to each in meters.
        destination: Stop ids the rider can finish at, with the walk from each.
        depart_at: Leave no earlier than this (Unix seconds). Exactly one of
            ``depart_at`` and ``arrive_by`` is required.
        arrive_by: Arrive no later than this (Unix seconds).
        max_rides: At most this many vehicles, so ``max_rides - 1`` transfers.
        delays: Live per-call delays from :func:`live_delays`.

    Raises:
        ValueError: unless exactly one of ``depart_at`` and ``arrive_by`` is given.
    """
    if depart_at is not None and arrive_by is None:
        forward, cursor = True, depart_at
    elif arrive_by is not None and depart_at is None:
        forward, cursor = False, arrive_by
    else:
        raise ValueError("give exactly one of depart_at and arrive_by")

    network = _network_for(schedule)
    starts = {network.stop_index[s]: m for s, m in origin.items() if s in network.stop_index}
    ends = {network.stop_index[s]: m for s, m in destination.items() if s in network.stop_index}
    if not starts or not ends:
        return []

    search = _search_forward if forward else _search_backward
    found: dict[tuple[tuple[str, date, str, str], ...], Itinerary] = {}
    # Each search yields the best journeys from one point in time; nudging that
    # point finds alternatives, some of which the others will turn out to beat.
    for _ in range(2 * limit):
        fresh = [
            itinerary
            for itinerary in search(network, starts, ends, cursor, max_rides, delays or {})
            if itinerary.key() not in found
        ]
        if not fresh:
            break
        found.update((itinerary.key(), itinerary) for itinerary in fresh)
        if len(_undominated(found.values())) >= limit:
            break
        if forward:
            cursor = min(itinerary.rides[0].departs_at for itinerary in fresh) + 60
        else:
            cursor = max(itinerary.rides[-1].arrives_at for itinerary in fresh) - 60

    kept = _undominated(found.values())
    if forward:
        kept.sort(key=lambda it: (it.arrives_at, len(it.rides)))
    else:
        kept.sort(key=lambda it: (-it.departs_at, len(it.rides)))
    return kept[:limit]


def _beats(first: Itinerary, second: Itinerary) -> bool:
    """Whether ``first`` leaves no earlier, arrives no later, and rides no more, and differs."""
    first_shape = (first.departs_at, first.arrives_at, len(first.rides))
    second_shape = (second.departs_at, second.arrives_at, len(second.rides))
    return (
        first.departs_at >= second.departs_at
        and first.arrives_at <= second.arrives_at
        and len(first.rides) <= len(second.rides)
        and first_shape != second_shape
    )


def _undominated(itineraries: Iterable[Itinerary]) -> list[Itinerary]:
    candidates = list(itineraries)
    return [it for it in candidates if not any(_beats(other, it) for other in candidates)]


def _search_forward(
    network: _Network,
    origin: dict[int, int],
    destination: dict[int, int],
    depart_at: int,
    max_rides: int,
    delays: LiveDelays,
) -> list[Itinerary]:
    connections, sources = _prepare(network, depart_at, depart_at + SEARCH_WINDOW_SECONDS, delays)
    order = sorted(range(len(connections)), key=lambda i: connections[i][0])
    access = {stop: depart_at + walk_seconds(meters) for stop, meters in origin.items()}
    labels = _scan(order, connections, len(network.stop_ids), access, network.transfers, max_rides)

    itineraries: list[Itinerary] = []
    best_so_far = _UNREACHED
    for rides in range(1, max_rides + 1):
        arrival, stop = min(
            (
                (labels.best[rides][stop] + walk_seconds(meters), stop)
                for stop, meters in destination.items()
                if labels.best[rides][stop] != _UNREACHED
            ),
            default=(_UNREACHED, -1),
        )
        # A journey with more rides is only worth offering if it arrives sooner.
        if arrival >= best_so_far:
            continue
        best_so_far = arrival
        steps = _trace(labels, connections, rides, stop)
        # Traced from the destination back; riders read it the other way.
        steps.reverse()
        itineraries.append(
            _itinerary(
                network,
                steps,
                sources,
                delays,
                origin=origin,
                destination=destination,
                not_before=depart_at,
            )
        )
    return itineraries


def _search_backward(
    network: _Network,
    origin: dict[int, int],
    destination: dict[int, int],
    arrive_by: int,
    max_rides: int,
    delays: LiveDelays,
) -> list[Itinerary]:
    connections, sources = _prepare(network, arrive_by - SEARCH_WINDOW_SECONDS, arrive_by, delays)
    # Reverse time: an arrival becomes a departure, negated so the latest scans first,
    # and boarding and alighting swap places.
    reversed_connections: list[_Scanned] = [
        (-arrives, -departs, to_stop, from_stop, trip, alightable, boardable)
        for departs, arrives, from_stop, to_stop, trip, boardable, alightable in connections
    ]
    order = sorted(
        (i for i, connection in enumerate(connections) if connection[1] <= arrive_by),
        key=lambda i: reversed_connections[i][0],
    )
    access = {stop: walk_seconds(meters) - arrive_by for stop, meters in destination.items()}
    labels = _scan(
        order, reversed_connections, len(network.stop_ids), access, network.transfers, max_rides
    )

    itineraries: list[Itinerary] = []
    best_so_far = _UNREACHED
    for rides in range(1, max_rides + 1):
        negated_departure, stop = min(
            (
                (labels.best[rides][stop] + walk_seconds(meters), stop)
                for stop, meters in origin.items()
                if labels.best[rides][stop] != _UNREACHED
            ),
            default=(_UNREACHED, -1),
        )
        # A journey with more rides is only worth offering if it leaves later.
        if negated_departure >= best_so_far:
            continue
        best_so_far = negated_departure
        steps = _trace(labels, reversed_connections, rides, stop)
        # Traced from the origin toward the destination already, but each step was
        # scanned backwards: swapping its ends puts it the right way round.
        steps = [(kind, second, first, meters) for kind, first, second, meters in steps]
        itineraries.append(
            _itinerary(
                network,
                steps,
                sources,
                delays,
                origin=origin,
                destination=destination,
                not_after=arrive_by,
            )
        )
    return itineraries


def _trace(labels: _Labels, connections: Sequence[_Scanned], rides: int, stop: int) -> list[_Step]:
    """Follow parents from a label back to the stop its search started from.

    Returns the steps in the order followed, oriented as scanned.
    Walks only ever follow a ride and strictly improve a label, so the chain
    cannot loop; the bound is a guard, not an expected limit.
    """
    steps: list[_Step] = []
    level, current = rides, stop
    for _ in range(len(labels.best[0]) + 2 * rides + 2):
        kind, first, second = labels.parents[level][current]
        if kind == _ACCESS:
            return steps
        if kind == _WALK:
            steps.append((_WALK, first, current, second))
            current = first
        else:
            steps.append((_RIDE, first, second, 0))
            current = connections[first][2]
            level -= 1
    raise RuntimeError("itinerary trace did not reach the starting stop")


@dataclass(slots=True)
class _Ride:
    """A trip the rider takes, between two of its calls."""

    trip_id: str
    service_date: date
    board: int
    alight: int
    #: Unix seconds of the trip's reference point, and its live per-call delays.
    origin: int
    shift: array[int] | None

    def departs(self, schedule: Schedule, call: int) -> int:
        delay = 0 if self.shift is None else self.shift[call]
        return self.origin + schedule.timetables[self.trip_id].departures[call] + delay

    def arrives(self, schedule: Schedule, call: int) -> int:
        delay = 0 if self.shift is None else self.shift[call]
        return self.origin + schedule.timetables[self.trip_id].arrivals[call] + delay


def _stop_walk(schedule: Schedule, from_stop: str, to_stop: str) -> int | None:
    """Estimated meters between two stops, or ``None`` if too far to transfer on foot."""
    if from_stop == to_stop:
        return 0
    first, second = schedule.stops.get(from_stop), schedule.stops.get(to_stop)
    if first is None or second is None:
        return None
    meters = walk_meters(first.latitude, first.longitude, second.latitude, second.longitude)
    return meters if meters <= TRANSFER_WALK_METERS else None


def _refine(
    schedule: Schedule,
    rides: list[_Ride],
    origin: dict[str, int],
    destination: dict[str, int],
    not_before: int | None,
    not_after: int | None,
) -> None:
    """Re-pick where to board, change, and get off, keeping the same trips.

    The scan settles ties arbitrarily: running backwards it happily rides one
    stop further and walks five minutes back. On the same vehicles, the rider is
    better served by the least walking that still makes every connection.
    """
    for before, after in pairwise(rides):
        stops_before = schedule.timetables[before.trip_id].stop_ids
        stops_after = schedule.timetables[after.trip_id].stop_ids
        timetable_before = schedule.timetables[before.trip_id]
        timetable_after = schedule.timetables[after.trip_id]
        best: tuple[int, int, int, int] | None = None  # (meters, -slack, alight, board)
        for alight in range(before.board + 1, len(stops_before)):
            if not timetable_before.alights_at(alight):
                continue
            reached = before.arrives(schedule, alight)
            for board in range(after.alight):
                if not timetable_after.boards_at(board):
                    continue
                meters = _stop_walk(schedule, stops_before[alight], stops_after[board])
                if meters is None:
                    continue
                slack = after.departs(schedule, board) - (
                    reached + (walk_seconds(meters) if meters else 0) + TRANSFER_BUFFER_SECONDS
                )
                if slack < 0:
                    continue
                candidate = (meters, -slack, alight, board)
                if best is None or candidate < best:
                    best = candidate
        if best is not None:
            before.alight, after.board = best[2], best[3]

    first = rides[0]
    first_timetable = schedule.timetables[first.trip_id]
    boarding_options = [
        (origin[stop_id], -first.departs(schedule, call), call)
        for call, stop_id in enumerate(first_timetable.stop_ids[: first.alight])
        if stop_id in origin
        and first_timetable.boards_at(call)
        and (
            not_before is None
            or first.departs(schedule, call) - walk_seconds(origin[stop_id]) - 60 >= not_before
        )
    ]
    if boarding_options:
        first.board = min(boarding_options)[2]

    last = rides[-1]
    last_timetable = schedule.timetables[last.trip_id]
    alighting_options = [
        (
            last.arrives(schedule, call) + walk_seconds(destination[stop_id]),
            destination[stop_id],
            call,
        )
        for call in range(last.board + 1, len(last_timetable.stop_ids))
        for stop_id in [last_timetable.stop_ids[call]]
        if stop_id in destination
        and last_timetable.alights_at(call)
        and (
            not_after is None
            or last.arrives(schedule, call) + walk_seconds(destination[stop_id]) <= not_after
        )
    ]
    if alighting_options:
        last.alight = min(alighting_options)[2]


def _minute_floor(epoch: int) -> int:
    return epoch - epoch % 60


def _minute_ceil(epoch: int) -> int:
    return -(-epoch // 60) * 60


def _walk(
    from_stop: str | None,
    to_stop: str | None,
    meters: int,
    leaves: int,
    latest_arrival: int | None = None,
) -> WalkLeg:
    """A walk leg, its arrival rounded up to the minute a rider would read."""
    arrives = _minute_ceil(leaves + walk_seconds(meters))
    if latest_arrival is not None:
        arrives = min(arrives, latest_arrival)
    return WalkLeg(from_stop, to_stop, meters, leaves, max(arrives, leaves))


def _itinerary(
    network: _Network,
    steps: list[_Step],
    sources: Sequence[_Source],
    delays: LiveDelays,
    *,
    origin: dict[int, int],
    destination: dict[int, int],
    not_before: int | None = None,
    not_after: int | None = None,
) -> Itinerary:
    """Turn a traced journey into timed legs a rider can follow."""
    schedule = network.schedule
    zone = schedule.zone
    rides = [
        _Ride(
            trip_id=network.trip_ids[sources[first].trip],
            service_date=sources[first].service_date,
            board=sources[first].call,
            alight=sources[second].call + 1,
            origin=service_day_origin(sources[first].service_date, zone),
            shift=delays.get((network.trip_ids[sources[first].trip], sources[first].service_date)),
        )
        for kind, first, second, _ in steps
        if kind == _RIDE
    ]
    origin_by_id = {network.stop_ids[stop]: meters for stop, meters in origin.items()}
    destination_by_id = {network.stop_ids[stop]: meters for stop, meters in destination.items()}
    _refine(schedule, rides, origin_by_id, destination_by_id, not_before, not_after)

    legs: list[Leg] = []
    previous: RideLeg | None = None
    for ride in rides:
        timetable = schedule.timetables[ride.trip_id]
        board_stop, alight_stop = timetable.stop_ids[ride.board], timetable.stop_ids[ride.alight]
        departs, arrives = ride.departs(schedule, ride.board), ride.arrives(schedule, ride.alight)
        if previous is None:
            meters = origin_by_id.get(board_stop, 0)
            if meters:
                # Reach the stop a minute early, but never leave before the rider asked to.
                leaves = _minute_floor(departs - walk_seconds(meters) - 60)
                if not_before is not None:
                    leaves = max(leaves, not_before)
                legs.append(_walk(None, board_stop, meters, leaves, latest_arrival=departs))
        else:
            meters = _stop_walk(schedule, previous.alight_stop, board_stop) or 0
            if meters:
                legs.append(
                    _walk(previous.alight_stop, board_stop, meters, previous.arrives_at, departs)
                )
        previous = RideLeg(
            trip_id=ride.trip_id,
            service_date=ride.service_date,
            board_stop=board_stop,
            alight_stop=alight_stop,
            stops_travelled=ride.alight - ride.board,
            departs_at=departs,
            arrives_at=max(arrives, departs),
            scheduled_departs_at=ride.origin + timetable.departures[ride.board],
            scheduled_arrives_at=ride.origin + timetable.arrivals[ride.alight],
            live=ride.shift is not None,
        )
        legs.append(previous)
    if previous is not None:
        meters = destination_by_id.get(previous.alight_stop, 0)
        if meters:
            legs.append(_walk(previous.alight_stop, None, meters, previous.arrives_at))
    return Itinerary(legs=tuple(legs))
