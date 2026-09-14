from __future__ import annotations

import pytest

from queenscoach.realtime import VehiclePosition
from queenscoach.static_gtfs import Schedule
from queenscoach.transit import (
    by_distance,
    distance_meters,
    find_routes,
    find_stops,
    group_into_stations,
    iso_time,
    matches_vehicle_query,
    natural_key,
    normalize,
    routes_by_name,
    serves,
    station_for,
)


def test_normalize_collapses_case_and_whitespace() -> None:
    assert normalize("  Blue   Line ") == "blue line"


def test_natural_key_orders_numbers_numerically() -> None:
    assert sorted(["501", "29", "5", "510", "1"], key=natural_key) == [
        "1",
        "5",
        "29",
        "501",
        "510",
    ]
    assert sorted(["Route 9", "Route 10"], key=natural_key) == ["Route 9", "Route 10"]


def test_iso_time_renders_utc_with_milliseconds() -> None:
    assert iso_time(0) == "1970-01-01T00:00:00.000Z"
    assert iso_time(1_788_904_788) == "2026-09-08T21:59:48.000Z"


def test_distance_between_a_point_and_itself_is_zero() -> None:
    assert distance_meters(35.2, -80.8, 35.2, -80.8) == 0.0


def test_distance_matches_a_known_separation() -> None:
    # One degree of latitude is about 111.2 km anywhere on the globe.
    assert distance_meters(35.0, -80.8, 36.0, -80.8) == pytest.approx(111_195, rel=0.001)


def test_route_matching_is_exact_before_substring(schedule: Schedule) -> None:
    assert [route.route_id for route in find_routes(schedule, "5")] == ["5"]
    assert [route.route_id for route in find_routes(schedule, "501")] == ["501"]


def test_route_matching_falls_back_to_the_long_name(schedule: Schedule) -> None:
    matched = find_routes(schedule, "Blue Line")
    assert [route.route_id for route in matched] == ["501"]


def test_an_empty_or_unknown_route_query_matches_nothing(schedule: Schedule) -> None:
    assert find_routes(schedule, "   ") == []
    assert find_routes(schedule, "no such route") == []


def test_stop_matching_prefers_an_exact_id(schedule: Schedule) -> None:
    assert [stop.stop_id for stop in find_stops(schedule, "00285")] == ["00285"]


def test_stop_matching_ranks_substring_hits_by_name_length(schedule: Schedule) -> None:
    matched = find_stops(schedule, "Central Ave", limit=5)
    assert len(matched) > 1
    names = [stop.name for stop in matched]
    assert names == sorted(names, key=lambda name: (len(name), name))


def test_stop_matching_respects_the_limit(schedule: Schedule) -> None:
    assert len(find_stops(schedule, "Ave", limit=3)) == 3


def test_vehicle_query_matches_label_id_or_entity_id() -> None:
    vehicle = VehiclePosition(
        entity_id="entity-1",
        vehicle_label="2301",
        vehicle_id="4133",
        trip_id=None,
        route_id=None,
        latitude=35.0,
        longitude=-80.0,
        bearing_degrees=None,
        speed_meters_per_second=None,
        occupancy_status=None,
        current_status=None,
        timestamp=None,
    )
    assert matches_vehicle_query(vehicle, " 2301 ")
    assert matches_vehicle_query(vehicle, "4133")
    assert matches_vehicle_query(vehicle, "ENTITY-1")
    assert not matches_vehicle_query(vehicle, "230")
    assert not matches_vehicle_query(vehicle, "")


def test_serves_reads_the_routes_calling_at_a_stop(schedule: Schedule) -> None:
    blue_line_platform = schedule.stops["00015"]
    assert serves(schedule, blue_line_platform)
    assert serves(schedule, blue_line_platform, mode="train")
    assert not serves(schedule, blue_line_platform, mode="bus")
    assert serves(schedule, blue_line_platform, route_ids=frozenset({"501", "29"}))
    assert not serves(schedule, blue_line_platform, route_ids=frozenset({"29"}))


def test_a_stop_no_trip_calls_at_serves_nothing(schedule: Schedule) -> None:
    unserved = next(
        stop for stop in schedule.stops.values() if stop.stop_id not in schedule.stop_routes
    )
    assert not serves(schedule, unserved)


def test_same_named_platforms_close_together_are_one_station(schedule: Schedule) -> None:
    station = station_for(schedule, schedule.stops["51001"])
    assert station.primary.stop_id == "51001"
    assert station.stop_ids == {"51000", "51001"}
    assert station.route_ids == {"510"}


def test_differently_named_neighbours_stay_separate_stations(schedule: Schedule) -> None:
    # CTC Station (Blue Line) and CTC/Arena CityLYNX (Gold Line) are ~100 m apart.
    ctc = [schedule.stops["00002"], schedule.stops["51000"], schedule.stops["51001"]]
    stations = group_into_stations(schedule, ctc)
    assert [sorted(station.stop_ids) for station in stations] == [["00002"], ["51000", "51001"]]


def test_by_distance_orders_nearest_first(schedule: Schedule) -> None:
    seventh_st = schedule.stops["00001"]
    ordered = by_distance(schedule.stops.values(), seventh_st.latitude, seventh_st.longitude)
    assert ordered[0] is seventh_st
    gaps = [
        distance_meters(seventh_st.latitude, seventh_st.longitude, stop.latitude, stop.longitude)
        for stop in ordered
    ]
    assert gaps == sorted(gaps)


def test_routes_by_name_orders_numerically_and_skips_unknown_ids(schedule: Schedule) -> None:
    routes = routes_by_name(schedule, ["510", "29", "missing", "501"])
    assert [route.short_name for route in routes] == ["29", "501", "510"]
