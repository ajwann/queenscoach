from __future__ import annotations

import pytest

from queenscoach.realtime import (
    RealtimeDecodeError,
    ServiceAlert,
    TripUpdate,
    VehiclePosition,
    decode_alerts,
    decode_trip_updates,
    decode_vehicle_positions,
)


def test_vehicle_positions_decode_with_labels_routes_and_coordinates(
    vehicles: list[VehiclePosition],
) -> None:
    assert len(vehicles) == 158
    first = next(vehicle for vehicle in vehicles if vehicle.vehicle_label == "2301")
    assert first.route_id == "29"
    assert first.trip_id == "5438306"
    assert first.latitude == pytest.approx(35.3211, abs=1e-3)
    assert first.bearing_degrees == pytest.approx(45.0)
    assert first.occupancy_status == "NO_DATA_AVAILABLE"


def test_every_decoded_vehicle_has_coordinates(vehicles: list[VehiclePosition]) -> None:
    for vehicle in vehicles:
        assert -90 <= vehicle.latitude <= 90
        assert -180 <= vehicle.longitude <= 180


def test_trip_updates_carry_predicted_and_scheduled_times(
    trip_updates: list[TripUpdate],
) -> None:
    assert trip_updates
    stop_times = [
        stop_time
        for update in trip_updates
        for stop_time in update.stop_time_updates
        if stop_time.arrival_time is not None
    ]
    assert stop_times
    # The feed never populates `delay`, so both stamps must be present instead.
    assert all(stop_time.scheduled_arrival_time is not None for stop_time in stop_times)


def test_alerts_decode_english_text_and_informed_entities(alerts: list[ServiceAlert]) -> None:
    assert len(alerts) == 4
    alert = alerts[0]
    assert alert.header_text == "Construction will create Detour"
    assert alert.cause == "CONSTRUCTION"
    assert alert.effect == "DETOUR"
    assert alert.severity_level == "WARNING"
    assert alert.informed_route_ids == ("35",)
    assert alert.informed_stop_ids == ("45726",)


def test_alerts_decode_when_they_apply_and_their_detail_text(alerts: list[ServiceAlert]) -> None:
    detour = alerts[0]
    assert detour.effect_detail == "Detour"
    assert detour.cause_detail == "Construction"
    (period,) = detour.active_periods
    assert period.start == 1_763_319_660
    assert detour.is_active(1_788_904_788)
    assert not detour.is_active(1_700_000_000), "before its start"
    assert not detour.has_ended(1_788_904_788)

    maintenance = alerts[3]
    (window,) = maintenance.active_periods
    assert window.end == 1_788_907_800
    assert maintenance.has_ended(1_788_907_800)


def test_an_empty_feed_decodes_to_no_entities() -> None:
    assert decode_vehicle_positions(b"") == []
    assert decode_trip_updates(b"") == []
    assert decode_alerts(b"") == []


@pytest.mark.parametrize("decode", [decode_vehicle_positions, decode_trip_updates, decode_alerts])
def test_a_corrupt_payload_raises_a_decode_error(decode: object) -> None:
    with pytest.raises(RealtimeDecodeError):
        decode(b"\xff\xff\xff\xff not a protobuf")  # type: ignore[operator]
