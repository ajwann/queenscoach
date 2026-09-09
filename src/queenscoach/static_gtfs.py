"""Static GTFS schedule: the lookup tables that give the realtime feeds meaning.

The realtime protobufs carry only identifiers (route ``29``, stop ``02400``,
trip ``5438306``). This module downloads the published GTFS zip and keeps the
three small tables needed to turn those into route names, stop names and
coordinates, and trip headsigns. ``stop_times.txt`` and ``shapes.txt`` are the
bulk of the archive and are deliberately not extracted.
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
from .gtfs_csv import GtfsRow, parse_csv

#: Vehicle mode, derived from GTFS ``route_type``.
Mode = Literal["bus", "train"]

_ROUTES_FILE = "routes.txt"
_STOPS_FILE = "stops.txt"
_TRIPS_FILE = "trips.txt"

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


def _read_table(archive: zipfile.ZipFile, name: str) -> list[GtfsRow]:
    try:
        with archive.open(name) as member:
            raw = member.read(_MAX_TABLE_BYTES + 1)
    except KeyError as error:
        raise StaticGtfsError(f"Static GTFS archive is missing {name}") from error
    if len(raw) > _MAX_TABLE_BYTES:
        raise StaticGtfsError(f"{name} in the static GTFS archive is implausibly large")
    return parse_csv(raw.decode("utf-8", errors="replace"))


def parse_schedule(archive_bytes: bytes) -> Schedule:
    """Parse the three tables this server needs out of a GTFS zip archive.

    Raises:
        StaticGtfsError: if the archive is unreadable, missing a table, or empty.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            route_rows = _read_table(archive, _ROUTES_FILE)
            stop_rows = _read_table(archive, _STOPS_FILE)
            trip_rows = _read_table(archive, _TRIPS_FILE)
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

    trips: dict[str, Trip] = {}
    for row in trip_rows:
        trip_id = row.get("trip_id")
        route_id = row.get("route_id")
        if trip_id is None or route_id is None:
            continue
        trips[trip_id] = Trip(trip_id=trip_id, route_id=route_id, headsign=row.get("trip_headsign"))

    if not routes or not stops:
        raise StaticGtfsError("Static GTFS archive parsed but contained no routes or stops")
    return Schedule(routes=routes, stops=stops, trips=trips)


def create_schedule_loader(config: Config) -> Callable[[], Awaitable[Cached[Schedule]]]:
    """Return a loader that fetches and caches the static schedule."""

    async def load() -> Schedule:
        archive = await fetch_binary(
            config.static_gtfs_url,
            timeout_seconds=config.request_timeout_seconds,
            max_bytes=config.max_static_bytes,
        )
        return parse_schedule(archive)

    cache: TtlCache[Schedule] = TtlCache(config.static_ttl_seconds, load)
    return cache.get
