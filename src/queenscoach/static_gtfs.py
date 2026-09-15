"""Static GTFS schedule: the lookup tables that give the realtime feeds meaning.

The realtime protobufs carry only identifiers (route ``29``, stop ``02400``,
trip ``5438306``). This module downloads the published GTFS zip and keeps what
turns those into route names, stop names and coordinates, and trip headsigns,
plus the timetable itself: every trip's calls and the dates each service runs.
``stop_times.txt`` is the bulk of the archive, so it is streamed and each trip
packed into arrays as it is read. ``shapes.txt`` is not read.
"""

from __future__ import annotations

import asyncio
import io
import math
import zipfile
from array import array
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cache import Cached, TtlCache
from .config import Config
from .feed_http import fetch_binary
from .gtfs_csv import GtfsRow, iter_csv, parse_csv

#: Vehicle mode, derived from GTFS ``route_type``.
Mode = Literal["bus", "train"]

_ROUTES_FILE = "routes.txt"
_STOPS_FILE = "stops.txt"
_TRIPS_FILE = "trips.txt"
_STOP_TIMES_FILE = "stop_times.txt"
_STOP_TIMES_COLUMNS = frozenset(
    {
        "trip_id",
        "stop_id",
        "stop_sequence",
        "arrival_time",
        "departure_time",
        "pickup_type",
        "drop_off_type",
    }
)
_CALENDAR_FILE = "calendar.txt"
_CALENDAR_DATES_FILE = "calendar_dates.txt"
_AGENCY_FILE = "agency.txt"
_WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: CATS's own zone, Eastern time. The timetable uses agency.txt's zone when it names a
#: valid one, and this otherwise; resources report their fetch times in it.
DEFAULT_TIMEZONE = "America/New_York"

#: GTFS pickup_type / drop_off_type 1: no pickup, or no drop-off, at this call.
_NOT_AVAILABLE = 1

# GTFS ``route_type`` values CATS publishes: 0 (tram/streetcar/light rail) for
# the Blue and Gold lines, 3 (bus) for everything else. Other rail-ish types are
# mapped defensively in case the agency adds service.
_RAIL_ROUTE_TYPES = frozenset({"0", "1", "2", "5", "7", "12"})

# A single GTFS table has no business being larger than this once decompressed;
# the ceiling keeps a malicious or corrupt archive from exhausting memory.
_MAX_TABLE_BYTES = 64 * 1024 * 1024


class StaticGtfsError(Exception):
    """The static GTFS archive could not be read."""


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    #: Rider-facing designator, e.g. ``29`` or ``501``.
    short_name: str
    long_name: str
    mode: Mode


@dataclass(frozen=True, slots=True)
class Stop:
    stop_id: str
    code: str | None
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Trip:
    trip_id: str
    route_id: str
    headsign: str | None
    #: Which service calendar the trip runs on; ``None`` means it never runs.
    service_id: str | None = None
    direction_id: str | None = None


@dataclass(frozen=True, slots=True)
class TripTimetable:
    """One trip's calls, in stop order.

    Times are seconds after the service day's reference point, noon minus 12
    hours, the way GTFS writes them: a call at ``25:10:00`` is ``90600``, on the
    calendar day after the service date.
    """

    stop_ids: tuple[str, ...]
    stop_sequences: array[int]
    arrivals: array[int]
    departures: array[int]
    #: GTFS ``pickup_type`` per call: 0 regular, 1 none, 2 phone ahead, 3 ask the driver.
    pickup_types: bytes
    drop_off_types: bytes

    def boards_at(self, index: int) -> bool:
        """Whether riders may board at call ``index``. The last call never boards."""
        return index < len(self.stop_ids) - 1 and self.pickup_types[index] != _NOT_AVAILABLE

    def alights_at(self, index: int) -> bool:
        """Whether riders may get off at call ``index``. The first call never alights."""
        return index > 0 and self.drop_off_types[index] != _NOT_AVAILABLE


@dataclass(frozen=True, slots=True)
class ServiceCalendar:
    """Which service ids run on which dates."""

    by_date: dict[date, frozenset[str]]

    def services_on(self, day: date) -> frozenset[str]:
        return self.by_date.get(day, frozenset())

    @property
    def first_date(self) -> date | None:
        return min(self.by_date, default=None)

    @property
    def last_date(self) -> date | None:
        return max(self.by_date, default=None)


@dataclass(frozen=True, slots=True)
class Schedule:
    routes: dict[str, Route]
    stops: dict[str, Stop]
    trips: dict[str, Trip]
    #: Route ids of every scheduled trip calling at each stop, from ``stop_times.txt``.
    stop_routes: dict[str, frozenset[str]]
    #: Each trip's calls, keyed by trip id. Trips without usable times are absent.
    timetables: dict[str, TripTimetable]
    calendar: ServiceCalendar
    #: IANA zone the timetable's clock times are in, from agency.txt.
    timezone: str = DEFAULT_TIMEZONE

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class StaticGtfs:
    """The downloaded archive, kept alongside what was parsed out of it."""

    archive: bytes
    schedule: Schedule


def _to_mode(route_type: str | None) -> Mode:
    return "train" if route_type in _RAIL_ROUTE_TYPES else "bus"


def _to_coordinate(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        # GTFS files in the wild pad coordinates with spaces; float() tolerates that.
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _table_info(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise StaticGtfsError(f"Static GTFS archive is missing {name}") from error
    # zipfile stops reading a member at its declared size, so this bounds memory.
    if info.file_size > _MAX_TABLE_BYTES:
        raise StaticGtfsError(f"{name} in the static GTFS archive is implausibly large")
    return info


def _read_table(archive: zipfile.ZipFile, name: str) -> list[GtfsRow]:
    with archive.open(_table_info(archive, name)) as member:
        raw = member.read()
    return parse_csv(raw.decode("utf-8", errors="replace"))


def _read_optional_table(archive: zipfile.ZipFile, name: str) -> list[GtfsRow]:
    if name not in archive.namelist():
        return []
    return _read_table(archive, name)


def parse_gtfs_time(raw: str | None) -> int | None:
    """Seconds after the service day's reference point for ``H:MM:SS``, or ``None``.

    Hours may exceed 23: ``25:10:00`` is a call after midnight on a trip that
    belongs to the previous service day.
    """
    if raw is None:
        return None
    parts = raw.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _to_small_int(raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        return 0
    return value if 0 <= value <= 3 else 0


#: One stop_times row, reduced: sequence, stop id, arrival, departure, pickup, drop-off.
_Call = tuple[int, str, int | None, int | None, int, int]


def _fill_missing_times(calls: list[_Call]) -> list[tuple[int, str, int, int, int, int]]:
    """Give every call both times, interpolating untimed calls between timed ones.

    GTFS lets intermediate calls omit times; their position between the nearest
    timed calls is the best estimate. Untimed calls before the first or after the
    last timed call cannot be placed and are dropped.
    """
    known: list[tuple[int, int]] = []
    for index, (_, _, arrival, departure, _, _) in enumerate(calls):
        timed = arrival if arrival is not None else departure
        if timed is not None:
            known.append((index, timed))
    if len(known) < 2:
        return []

    filled: list[tuple[int, str, int, int, int, int]] = []
    cursor = 0
    for index, (sequence, stop_id, arrival, departure, pickup, drop_off) in enumerate(calls):
        if index < known[0][0] or index > known[-1][0]:
            continue
        if arrival is not None and departure is not None:
            times = (arrival, departure)
        elif arrival is not None:
            times = (arrival, arrival)
        elif departure is not None:
            times = (departure, departure)
        else:
            while known[cursor + 1][0] < index:
                cursor += 1
            (before_index, before_time), (after_index, after_time) = (
                known[cursor],
                known[cursor + 1],
            )
            share = (index - before_index) / (after_index - before_index)
            estimate = round(before_time + share * (after_time - before_time))
            times = (estimate, estimate)
        filled.append((sequence, stop_id, times[0], times[1], pickup, drop_off))
    return filled


def _pack(calls: list[_Call]) -> TripTimetable | None:
    calls.sort(key=lambda call: call[0])
    filled = _fill_missing_times(calls)
    if len(filled) < 2:
        return None
    return TripTimetable(
        stop_ids=tuple(call[1] for call in filled),
        stop_sequences=array("i", (call[0] for call in filled)),
        arrivals=array("i", (call[2] for call in filled)),
        departures=array("i", (call[3] for call in filled)),
        pickup_types=bytes(call[4] for call in filled),
        drop_off_types=bytes(call[5] for call in filled),
    )


def _unpack(timetable: TripTimetable) -> list[_Call]:
    return [
        (
            timetable.stop_sequences[index],
            timetable.stop_ids[index],
            timetable.arrivals[index],
            timetable.departures[index],
            timetable.pickup_types[index],
            timetable.drop_off_types[index],
        )
        for index in range(len(timetable.stop_ids))
    ]


def _read_timetables(archive: zipfile.ZipFile, trips: dict[str, Trip]) -> dict[str, TripTimetable]:
    """Pack each trip's calls into arrays while streaming ``stop_times.txt``.

    The table runs to hundreds of thousands of rows. Feeds list a trip's rows
    together, so each trip is packed as soon as the next begins and only one
    trip's rows are ever held as Python objects. A trip whose rows reappear
    later is unpacked and merged, so an unordered file still parses correctly.
    """
    timetables: dict[str, TripTimetable] = {}
    #: Rows of trips too short to pack so far, in case more of their rows follow.
    partial: dict[str, list[_Call]] = {}
    #: One shared string per stop id, rather than one per row.
    interned: dict[str, str] = {}
    current_trip: str | None = None
    current: list[_Call] = []

    def flush() -> None:
        if current_trip is None or not current:
            return
        earlier = timetables.pop(current_trip, None)
        calls = current + partial.pop(current_trip, [])
        if earlier is not None:
            calls += _unpack(earlier)
        packed = _pack(calls)
        if packed is None:
            partial[current_trip] = calls
        else:
            timetables[current_trip] = packed

    with (
        archive.open(_table_info(archive, _STOP_TIMES_FILE)) as member,
        io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline="") as text,
    ):
        for row in iter_csv(text, _STOP_TIMES_COLUMNS):
            trip_id = row.get("trip_id")
            stop_id = row.get("stop_id")
            if trip_id is None or stop_id is None or trip_id not in trips:
                continue
            try:
                sequence = int(row.get("stop_sequence") or "")
            except ValueError:
                continue
            if trip_id != current_trip:
                flush()
                current_trip, current = trip_id, []
            current.append(
                (
                    sequence,
                    interned.setdefault(stop_id, stop_id),
                    parse_gtfs_time(row.get("arrival_time")),
                    parse_gtfs_time(row.get("departure_time")),
                    _to_small_int(row.get("pickup_type")),
                    _to_small_int(row.get("drop_off_type")),
                )
            )
        flush()
    return timetables


def _stop_routes(
    trips: dict[str, Trip], timetables: dict[str, TripTimetable]
) -> dict[str, frozenset[str]]:
    routes_by_stop: dict[str, set[str]] = {}
    for trip_id, timetable in timetables.items():
        route_id = trips[trip_id].route_id
        for stop_id in timetable.stop_ids:
            routes_by_stop.setdefault(stop_id, set()).add(route_id)
    return {stop_id: frozenset(route_ids) for stop_id, route_ids in routes_by_stop.items()}


def _parse_date(raw: str | None) -> date | None:
    if raw is None or len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None


def _parse_calendar(
    calendar_rows: Iterable[GtfsRow], date_rows: Iterable[GtfsRow]
) -> ServiceCalendar:
    """Expand calendar.txt's weekly patterns, then apply calendar_dates.txt's exceptions."""
    running: dict[date, set[str]] = {}
    for row in calendar_rows:
        service_id = row.get("service_id")
        start, end = _parse_date(row.get("start_date")), _parse_date(row.get("end_date"))
        if service_id is None or start is None or end is None or end < start:
            continue
        weekdays = {
            index for index, column in enumerate(_WEEKDAY_COLUMNS) if row.get(column) == "1"
        }
        for offset in range((end - start).days + 1):
            running_day = start + timedelta(days=offset)
            if running_day.weekday() in weekdays:
                running.setdefault(running_day, set()).add(service_id)
    for row in date_rows:
        service_id = row.get("service_id")
        day = _parse_date(row.get("date"))
        if service_id is None or day is None:
            continue
        exception = row.get("exception_type")
        if exception == "1":
            running.setdefault(day, set()).add(service_id)
        elif exception == "2":
            running.get(day, set()).discard(service_id)
    return ServiceCalendar(
        by_date={day: frozenset(services) for day, services in running.items() if services}
    )


def _agency_timezone(rows: list[GtfsRow]) -> str:
    for row in rows:
        name = row.get("agency_timezone")
        if name is None:
            continue
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return name
    return DEFAULT_TIMEZONE


def parse_schedule(archive_bytes: bytes) -> Schedule:
    """Parse the tables this server needs out of a GTFS zip archive.

    Raises:
        StaticGtfsError: if the archive is unreadable, missing a table, or empty.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            route_rows = _read_table(archive, _ROUTES_FILE)
            stop_rows = _read_table(archive, _STOPS_FILE)
            trip_rows = _read_table(archive, _TRIPS_FILE)
            trips = _parse_trips(trip_rows)
            timetables = _read_timetables(archive, trips)
            calendar = _parse_calendar(
                _read_optional_table(archive, _CALENDAR_FILE),
                _read_optional_table(archive, _CALENDAR_DATES_FILE),
            )
            timezone = _agency_timezone(_read_optional_table(archive, _AGENCY_FILE))
    except zipfile.BadZipFile as error:
        raise StaticGtfsError("Static GTFS archive could not be read as a zip file") from error

    routes: dict[str, Route] = {}
    for row in route_rows:
        route_id = row.get("route_id")
        if route_id is None:
            continue
        short_name = row.get("route_short_name") or route_id
        routes[route_id] = Route(
            route_id=route_id,
            short_name=short_name,
            long_name=row.get("route_long_name") or short_name,
            mode=_to_mode(row.get("route_type")),
        )

    stops: dict[str, Stop] = {}
    for row in stop_rows:
        stop_id = row.get("stop_id")
        latitude = _to_coordinate(row.get("stop_lat"))
        longitude = _to_coordinate(row.get("stop_lon"))
        # A stop without coordinates cannot answer a location question; skip it.
        if stop_id is None or latitude is None or longitude is None:
            continue
        stops[stop_id] = Stop(
            stop_id=stop_id,
            code=row.get("stop_code"),
            name=row.get("stop_name") or stop_id,
            latitude=latitude,
            longitude=longitude,
        )

    if not routes or not stops:
        raise StaticGtfsError("Static GTFS archive parsed but contained no routes or stops")
    return Schedule(
        routes=routes,
        stops=stops,
        trips=trips,
        stop_routes=_stop_routes(trips, timetables),
        timetables=timetables,
        calendar=calendar,
        timezone=timezone,
    )


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    name: str
    #: Uncompressed size, in bytes.
    size: int


def list_archive(archive_bytes: bytes) -> list[ArchiveEntry]:
    """The files in a GTFS zip archive, for publishing them individually.

    Raises:
        StaticGtfsError: if the archive is not a readable zip file.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            return [
                ArchiveEntry(name=info.filename, size=info.file_size)
                for info in archive.infolist()
                if not info.is_dir()
            ]
    except zipfile.BadZipFile as error:
        raise StaticGtfsError("Static GTFS archive could not be read as a zip file") from error


def read_archive_text(archive_bytes: bytes, name: str) -> str | None:
    """One table of a GTFS zip archive as text, or ``None`` if there is no such file.

    Raises:
        StaticGtfsError: if the archive is unreadable or the table implausibly large.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            if name not in archive.namelist():
                return None
            with archive.open(_table_info(archive, name)) as member:
                return member.read().decode("utf-8-sig", errors="replace")
    except zipfile.BadZipFile as error:
        raise StaticGtfsError("Static GTFS archive could not be read as a zip file") from error


def _parse_trips(rows: list[GtfsRow]) -> dict[str, Trip]:
    trips: dict[str, Trip] = {}
    for row in rows:
        trip_id = row.get("trip_id")
        route_id = row.get("route_id")
        if trip_id is None or route_id is None:
            continue
        trips[trip_id] = Trip(
            trip_id=trip_id,
            route_id=route_id,
            headsign=row.get("trip_headsign"),
            service_id=row.get("service_id"),
            direction_id=row.get("direction_id"),
        )
    return trips


@dataclass(frozen=True, slots=True)
class StaticLoaders:
    """Two views of one cached download: the parsed schedule and the raw archive."""

    load_schedule: Callable[[], Awaitable[Cached[Schedule]]]
    load_archive: Callable[[], Awaitable[Cached[bytes]]]


def create_static_loaders(config: Config) -> StaticLoaders:
    """Return loaders that fetch, parse, and cache the static GTFS archive once."""

    async def load() -> StaticGtfs:
        archive = await fetch_binary(
            config.static_gtfs_url,
            timeout_seconds=config.request_timeout_seconds,
            max_bytes=config.max_static_bytes,
        )
        # Parsing takes seconds on a full feed; keep it off the event loop.
        schedule = await asyncio.to_thread(parse_schedule, archive)
        return StaticGtfs(archive=archive, schedule=schedule)

    cache: TtlCache[StaticGtfs] = TtlCache(config.static_ttl_seconds, load)

    async def load_schedule() -> Cached[Schedule]:
        entry = await cache.get()
        return Cached(value=entry.value.schedule, fetched_at=entry.fetched_at)

    async def load_archive() -> Cached[bytes]:
        entry = await cache.get()
        return Cached(value=entry.value.archive, fetched_at=entry.fetched_at)

    return StaticLoaders(load_schedule=load_schedule, load_archive=load_archive)
