"""Runtime configuration.

Every value has a working default so the server starts with no environment
set; overrides are validated at startup.

Duration environment variables are named ``*_MS`` and are given in
milliseconds, which is the published interface these deployments already use.
They are stored on :class:`Config` as seconds, the unit the rest of the code
works in.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_VEHICLE_POSITIONS_URL = (
    "https://gtfsrealtime.ridetransit.org/GTFSRealTime/Vehicle/VehiclePositions.pb"
)
_TRIP_UPDATES_URL = "https://gtfsrealtime.ridetransit.org/GTFSRealTime/TripUpdate/TripUpdates.pb"
_ALERTS_URL = "https://gtfsrealtime.ridetransit.org/GTFSRealTime/Alert/Alerts.pb"
_STATIC_GTFS_URL = "https://gtfsrealtime.ridetransit.org/GTFSStatic/api/GTFSDownload/GTFS.zip"

_DEFAULT_REALTIME_TTL_MS = 20_000
_DEFAULT_STATIC_TTL_MS = 6 * 60 * 60 * 1000
_DEFAULT_REQUEST_TIMEOUT_MS = 30_000
_DEFAULT_MAX_FEED_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_STATIC_BYTES = 256 * 1024 * 1024


class ConfigError(Exception):
    """Raised when an environment override cannot be used."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime settings."""

    vehicle_positions_url: str
    trip_updates_url: str
    alerts_url: str
    static_gtfs_url: str
    #: How long a decoded realtime feed is reused before refetching.
    realtime_ttl_seconds: float
    #: How long the parsed static schedule is reused before refetching.
    static_ttl_seconds: float
    request_timeout_seconds: float
    #: Guards against a mis-pointed URL streaming an unbounded body at us.
    max_feed_bytes: int
    max_static_bytes: int


def _read_url(env: Mapping[str, str], key: str, fallback: str) -> str:
    raw = env.get(key)
    if not raw:
        return fallback
    parts = urlsplit(raw)
    if not parts.scheme:
        raise ConfigError(f"{key} is not a valid URL")
    # Feeds are fetched by the server itself; refuse anything but plain HTTP(S).
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"{key} must use http or https, got {parts.scheme}:")
    if not parts.netloc:
        raise ConfigError(f"{key} is not a valid URL")
    return urlunsplit(parts)


def _read_positive_int(env: Mapping[str, str], key: str, fallback: int) -> int:
    raw = env.get(key)
    if not raw:
        return fallback
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise ConfigError(f"{key} must be a positive integer, got {raw!r}") from error
    if value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {raw!r}")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build config from the environment.

    Raises:
        ConfigError: if any override is malformed.
    """
    env = os.environ if env is None else env
    return Config(
        vehicle_positions_url=_read_url(env, "CATS_VEHICLE_POSITIONS_URL", _VEHICLE_POSITIONS_URL),
        trip_updates_url=_read_url(env, "CATS_TRIP_UPDATES_URL", _TRIP_UPDATES_URL),
        alerts_url=_read_url(env, "CATS_ALERTS_URL", _ALERTS_URL),
        static_gtfs_url=_read_url(env, "CATS_STATIC_GTFS_URL", _STATIC_GTFS_URL),
        realtime_ttl_seconds=_read_positive_int(
            env, "CATS_REALTIME_TTL_MS", _DEFAULT_REALTIME_TTL_MS
        )
        / 1000,
        static_ttl_seconds=_read_positive_int(env, "CATS_STATIC_TTL_MS", _DEFAULT_STATIC_TTL_MS)
        / 1000,
        request_timeout_seconds=_read_positive_int(
            env, "CATS_REQUEST_TIMEOUT_MS", _DEFAULT_REQUEST_TIMEOUT_MS
        )
        / 1000,
        max_feed_bytes=_read_positive_int(env, "CATS_MAX_FEED_BYTES", _DEFAULT_MAX_FEED_BYTES),
        max_static_bytes=_read_positive_int(
            env, "CATS_MAX_STATIC_BYTES", _DEFAULT_MAX_STATIC_BYTES
        ),
    )
