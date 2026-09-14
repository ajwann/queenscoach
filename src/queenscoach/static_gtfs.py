"""Static GTFS schedule: the lookup tables that give the realtime feeds meaning.

The realtime protobufs carry only identifiers (route ``29``, stop ``02400``,
trip ``5438306``). This module downloads the published GTFS zip and keeps the
three small tables needed to turn those into route names, stop names and
coordinates, and trip headsigns. ``stop_times.txt`` is the bulk of the archive
and is streamed rather than held: only the set of routes serving each stop is
kept from it. ``shapes.txt`` is not read.
"""

from __future__ import annotations

import io
import math
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

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
_STOP_TIMES_COLUMNS = frozenset({"trip_id", "stop_id"})

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


@dataclass(frozen=True, slots=True)
class Schedule:
    routes: dict[str, Route]
    stops: dict[str, Stop]
    trips: dict[str, Trip]
    #: Route ids of every scheduled trip calling at each stop, from ``stop_times.txt``.
    stop_routes: dict[str, frozenset[str]]


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


def _read_stop_routes(
    archive: zipfile.ZipFile, trips: dict[str, Trip]
) -> dict[str, frozenset[str]]:
    """Map each stop to the routes whose trips call there.

    ``stop_times.txt`` runs to hundreds of thousands of rows, so it is streamed
    and reduced as it is read rather than parsed into row dicts first.
    """
    routes_by_stop: dict[str, set[str]] = {}
    with (
        archive.open(_table_info(archive, _STOP_TIMES_FILE)) as member,
        io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline="") as text,
    ):
        for row in iter_csv(text, _STOP_TIMES_COLUMNS):
            stop_id = row.get("stop_id")
            trip = trips.get(row.get("trip_id") or "")
            if stop_id is not None and trip is not None:
                routes_by_stop.setdefault(stop_id, set()).add(trip.route_id)
    return {stop_id: frozenset(route_ids) for stop_id, route_ids in routes_by_stop.items()}


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
            stop_routes = _read_stop_routes(archive, trips)
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
    return Schedule(routes=routes, stops=stops, trips=trips, stop_routes=stop_routes)


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
        trips[trip_id] = Trip(trip_id=trip_id, route_id=route_id, headsign=row.get("trip_headsign"))
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
        return StaticGtfs(archive=archive, schedule=parse_schedule(archive))

    cache: TtlCache[StaticGtfs] = TtlCache(config.static_ttl_seconds, load)

    async def load_schedule() -> Cached[Schedule]:
        entry = await cache.get()
        return Cached(value=entry.value.schedule, fetched_at=entry.fetched_at)

    async def load_archive() -> Cached[bytes]:
        entry = await cache.get()
        return Cached(value=entry.value.archive, fetched_at=entry.fetched_at)

    return StaticLoaders(load_schedule=load_schedule, load_archive=load_archive)
