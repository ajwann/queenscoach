"""Shared fixtures, built from feed captures so tests make no network calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from queenscoach.cache import Cached
from queenscoach.realtime import (
    ServiceAlert,
    TripUpdate,
    VehiclePosition,
    decode_alerts,
    decode_trip_updates,
    decode_vehicle_positions,
)
from queenscoach.static_gtfs import Schedule, parse_schedule
from queenscoach.tools import Dependencies

FIXTURES = Path(__file__).parent / "fixtures"

#: Fixture capture time, in Unix seconds, so relative-time assertions are deterministic.
CAPTURE_TIME = 1_788_904_788.0

SCHEDULE: Schedule = parse_schedule((FIXTURES / "gtfs-static.zip").read_bytes())
VEHICLES: list[VehiclePosition] = decode_vehicle_positions(
    (FIXTURES / "VehiclePositions.pb").read_bytes()
)
TRIP_UPDATES: list[TripUpdate] = decode_trip_updates((FIXTURES / "TripUpdates.pb").read_bytes())
ALERTS: list[ServiceAlert] = decode_alerts((FIXTURES / "Alerts.pb").read_bytes())


class FixtureFeeds:
    """Realtime feeds served from the recorded captures."""

    def __init__(self, alerts: list[ServiceAlert] | None = None) -> None:
        self._alerts = ALERTS if alerts is None else alerts

    async def vehicle_positions(self) -> Cached[list[VehiclePosition]]:
        return Cached(value=VEHICLES, fetched_at=CAPTURE_TIME)

    async def trip_updates(self) -> Cached[list[TripUpdate]]:
        return Cached(value=TRIP_UPDATES, fetched_at=CAPTURE_TIME)

    async def alerts(self) -> Cached[list[ServiceAlert]]:
        return Cached(value=self._alerts, fetched_at=CAPTURE_TIME)


class FailingAlertsFeeds(FixtureFeeds):
    """Everything works except the alerts feed."""

    async def alerts(self) -> Cached[list[ServiceAlert]]:
        raise RuntimeError("alerts feed down")


async def _load_schedule() -> Cached[Schedule]:
    return Cached(value=SCHEDULE, fetched_at=CAPTURE_TIME)


def fixture_deps(feeds: FixtureFeeds | None = None) -> Dependencies:
    """Build tool dependencies backed by the fixtures and a frozen clock."""
    return Dependencies(
        load_schedule=_load_schedule,
        feeds=feeds or FixtureFeeds(),
        now=lambda: CAPTURE_TIME,
    )


@pytest.fixture
def deps() -> Dependencies:
    return fixture_deps()


@pytest.fixture
def schedule() -> Schedule:
    return SCHEDULE


@pytest.fixture
def vehicles() -> list[VehiclePosition]:
    return VEHICLES


@pytest.fixture
def trip_updates() -> list[TripUpdate]:
    return TRIP_UPDATES


@pytest.fixture
def alerts() -> list[ServiceAlert]:
    return ALERTS
