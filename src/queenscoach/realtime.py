"""GTFS-Realtime feed access: vehicle positions, trip updates, service alerts.

Feed quirks observed in the live CATS data, which the shapes below reflect:

- ``VehiclePosition.stop_id`` and ``current_stop_sequence`` do not correspond
  to any stop or sequence in the published static schedule (0% of stop ids
  resolved, and sequences exceed the trip's own stop count). They are therefore
  never surfaced as stop references; next-stop information comes from the
  TripUpdates feed, whose stop ids resolve completely.
- ``StopTimeEvent.delay`` is never populated, but ``time`` and
  ``scheduled_time`` both are, so schedule deviation is computed from those.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as gtfs

from .cache import Cached, TtlCache
from .config import Config
from .feed_http import fetch_binary


class RealtimeDecodeError(Exception):
    """A realtime feed was not a decodable GTFS-Realtime message."""


@dataclass(frozen=True, slots=True)
class VehiclePosition:
    entity_id: str
    #: Agency vehicle number as shown on the bus or train, e.g. ``2301``.
    vehicle_label: str | None
    vehicle_id: str | None
    trip_id: str | None
    route_id: str | None
    latitude: float
    longitude: float
    bearing_degrees: float | None
    speed_meters_per_second: float | None
    occupancy_status: str | None
    current_status: str | None
    #: Unix time, in seconds, the position was reported.
    timestamp: int | None


@dataclass(frozen=True, slots=True)
class StopTimeUpdate:
    stop_id: str | None
    stop_sequence: int | None
    #: Predicted arrival, Unix time in seconds.
    arrival_time: int | None
    scheduled_arrival_time: int | None
    departure_time: int | None
    schedule_relationship: str | None


@dataclass(frozen=True, slots=True)
class TripUpdate:
    trip_id: str | None
    route_id: str | None
    vehicle_label: str | None
    vehicle_id: str | None
    stop_time_updates: tuple[StopTimeUpdate, ...]


@dataclass(frozen=True, slots=True)
class ServiceAlert:
    header_text: str | None
    description_text: str | None
    cause: str | None
    effect: str | None
    severity_level: str | None
    informed_route_ids: tuple[str, ...]
    informed_stop_ids: tuple[str, ...]


def _optional_str(message: Any, name: str) -> str | None:
    """Read a proto2 optional string, distinguishing unset from empty."""
    return getattr(message, name) if message.HasField(name) else None


def _optional_int(message: Any, name: str) -> int | None:
    return int(getattr(message, name)) if message.HasField(name) else None


def _optional_float(message: Any, name: str) -> float | None:
    if not message.HasField(name):
        return None
    value = float(getattr(message, name))
    return value if math.isfinite(value) else None


def _enum_name(enum_type: Any, message: Any, name: str) -> str | None:
    if not message.HasField(name):
        return None
    try:
        return str(enum_type.Name(getattr(message, name)))
    except ValueError:
        # An unrecognized value is feed data we do not model; report it as absent.
        return None


def _translated_text(translated: Any) -> str | None:
    """Pick the English translation of a GTFS-RT ``TranslatedString``."""
    translations = list(translated.translation)
    if not translations:
        return None
    chosen = next(
        (entry for entry in translations if entry.language.startswith("en")), translations[0]
    )
    return chosen.text or None


def _decode_feed(data: bytes) -> gtfs.FeedMessage:
    """Parse a GTFS-Realtime protobuf.

    Raises:
        RealtimeDecodeError: if the payload is not a valid FeedMessage.
    """
    feed = gtfs.FeedMessage()
    try:
        feed.ParseFromString(data)
    except (DecodeError, ValueError) as error:
        raise RealtimeDecodeError("Realtime feed was not a decodable protobuf") from error
    return feed


def decode_vehicle_positions(data: bytes) -> list[VehiclePosition]:
    """Decode the VehiclePositions feed, dropping entries without coordinates."""
    vehicles: list[VehiclePosition] = []
    for entity in _decode_feed(data).entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle

        position = vehicle.position
        latitude = _optional_float(position, "latitude") if vehicle.HasField("position") else None
        longitude = _optional_float(position, "longitude") if vehicle.HasField("position") else None
        # Without coordinates the entry cannot answer a location question.
        if latitude is None or longitude is None:
            continue

        trip = vehicle.trip
        descriptor = vehicle.vehicle
        vehicles.append(
            VehiclePosition(
                entity_id=entity.id,
                vehicle_label=_optional_str(descriptor, "label"),
                vehicle_id=_optional_str(descriptor, "id"),
                trip_id=_optional_str(trip, "trip_id"),
                route_id=_optional_str(trip, "route_id"),
                latitude=latitude,
                longitude=longitude,
                bearing_degrees=_optional_float(position, "bearing"),
                speed_meters_per_second=_optional_float(position, "speed"),
                occupancy_status=_enum_name(
                    gtfs.VehiclePosition.OccupancyStatus, vehicle, "occupancy_status"
                ),
                current_status=_enum_name(
                    gtfs.VehiclePosition.VehicleStopStatus, vehicle, "current_status"
                ),
                timestamp=_optional_int(vehicle, "timestamp"),
            )
        )
    return vehicles


def decode_trip_updates(data: bytes) -> list[TripUpdate]:
    """Decode the TripUpdates feed."""
    updates: list[TripUpdate] = []
    for entity in _decode_feed(data).entity:
        if not entity.HasField("trip_update"):
            continue
        update = entity.trip_update

        stop_times = tuple(
            StopTimeUpdate(
                stop_id=_optional_str(stop_time, "stop_id"),
                stop_sequence=_optional_int(stop_time, "stop_sequence"),
                arrival_time=_optional_int(stop_time.arrival, "time")
                if stop_time.HasField("arrival")
                else None,
                scheduled_arrival_time=_optional_int(stop_time.arrival, "scheduled_time")
                if stop_time.HasField("arrival")
                else None,
                departure_time=_optional_int(stop_time.departure, "time")
                if stop_time.HasField("departure")
                else None,
                schedule_relationship=_enum_name(
                    gtfs.TripUpdate.StopTimeUpdate.ScheduleRelationship,
                    stop_time,
                    "schedule_relationship",
                ),
            )
            for stop_time in update.stop_time_update
        )

        updates.append(
            TripUpdate(
                trip_id=_optional_str(update.trip, "trip_id"),
                route_id=_optional_str(update.trip, "route_id"),
                vehicle_label=_optional_str(update.vehicle, "label")
                if update.HasField("vehicle")
                else None,
                vehicle_id=_optional_str(update.vehicle, "id")
                if update.HasField("vehicle")
                else None,
                stop_time_updates=stop_times,
            )
        )
    return updates


def decode_alerts(data: bytes) -> list[ServiceAlert]:
    """Decode the Alerts feed."""
    alerts: list[ServiceAlert] = []
    for entity in _decode_feed(data).entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert

        route_ids: dict[str, None] = {}
        stop_ids: dict[str, None] = {}
        for selector in alert.informed_entity:
            if selector.HasField("route_id"):
                route_ids[selector.route_id] = None
            if selector.HasField("stop_id"):
                stop_ids[selector.stop_id] = None

        alerts.append(
            ServiceAlert(
                header_text=_translated_text(alert.header_text),
                description_text=_translated_text(alert.description_text),
                cause=_enum_name(gtfs.Alert.Cause, alert, "cause"),
                effect=_enum_name(gtfs.Alert.Effect, alert, "effect"),
                severity_level=_enum_name(gtfs.Alert.SeverityLevel, alert, "severity_level"),
                informed_route_ids=tuple(route_ids),
                informed_stop_ids=tuple(stop_ids),
            )
        )
    return alerts


class RealtimeFeeds(Protocol):
    """The three realtime feeds, each cached independently."""

    def vehicle_positions(self) -> Awaitable[Cached[list[VehiclePosition]]]: ...

    def trip_updates(self) -> Awaitable[Cached[list[TripUpdate]]]: ...

    def alerts(self) -> Awaitable[Cached[list[ServiceAlert]]]: ...


@dataclass(frozen=True, slots=True)
class CachedRealtimeFeeds:
    """Feeds backed by TTL caches over the configured feed URLs."""

    _vehicles: TtlCache[list[VehiclePosition]]
    _trips: TtlCache[list[TripUpdate]]
    _alerts: TtlCache[list[ServiceAlert]]

    def vehicle_positions(self) -> Awaitable[Cached[list[VehiclePosition]]]:
        return self._vehicles.get()

    def trip_updates(self) -> Awaitable[Cached[list[TripUpdate]]]:
        return self._trips.get()

    def alerts(self) -> Awaitable[Cached[list[ServiceAlert]]]:
        return self._alerts.get()


_FeedT = TypeVar("_FeedT")


def _feed_cache(config: Config, url: str, decode: Callable[[bytes], _FeedT]) -> TtlCache[_FeedT]:
    async def load() -> _FeedT:
        data = await fetch_binary(
            url,
            timeout_seconds=config.request_timeout_seconds,
            max_bytes=config.max_feed_bytes,
        )
        return decode(data)

    return TtlCache(config.realtime_ttl_seconds, load)


def create_realtime_feeds(config: Config) -> CachedRealtimeFeeds:
    """Build the three cached realtime feeds for ``config``."""
    return CachedRealtimeFeeds(
        _vehicles=_feed_cache(config, config.vehicle_positions_url, decode_vehicle_positions),
        _trips=_feed_cache(config, config.trip_updates_url, decode_trip_updates),
        _alerts=_feed_cache(config, config.alerts_url, decode_alerts),
    )
