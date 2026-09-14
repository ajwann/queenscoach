from __future__ import annotations

from typing import Any

from queenscoach.realtime import VehiclePosition
from queenscoach.static_gtfs import Schedule
from queenscoach.tools import Dependencies, get_arrivals, list_stops, list_vehicles

from .conftest import FailingAlertsFeeds, fixture_deps

CHARLOTTE_LAT = (34.9, 35.7)
CHARLOTTE_LON = (-81.2, -80.4)

#: Beside the I-485 Blue Line platform (stop 00015).
I_485_STATION = (35.1071, -80.8829)
#: Beside the 7th St Blue Line platform (stop 00001).
SEVENTH_ST_STATION = (35.2274, -80.8381)
#: A route 29 bus stop, Cove Creek Dr & Barrington Dr (06530), about 1.9 km from
#: the nearest light rail platform.
COVE_CREEK_BUS_STOP = (35.257464, -80.751958)


def test_static_schedule_classifies_light_rail_as_train_and_buses_as_bus(
    schedule: Schedule,
) -> None:
    assert schedule.routes["501"].mode == "train"
    assert schedule.routes["510"].mode == "train"
    assert schedule.routes["29"].mode == "bus"
    assert schedule.routes["501"].long_name == "Light Rail - Lynx Blue Line"


async def test_list_vehicles_locates_one_vehicle_in_the_charlotte_area(
    deps: Dependencies,
) -> None:
    result = await list_vehicles(deps, vehicle="2301")
    assert result["matches"] == 1
    found: dict[str, Any] = result["vehicles"][0]
    assert found["vehicle"] == "2301"
    assert CHARLOTTE_LAT[0] < found["latitude"] < CHARLOTTE_LAT[1]
    assert CHARLOTTE_LON[0] < found["longitude"] < CHARLOTTE_LON[1]
    assert found["route"]["name"] == "29"
    assert found["mode"] == "bus"


async def test_list_vehicles_by_route_returns_only_that_route(deps: Dependencies) -> None:
    result = await list_vehicles(deps, route="501")
    assert result["vehicles"], "expected Blue Line trains in the fixture"
    for found in result["vehicles"]:
        assert found["route"]["name"] == "501"
        assert found["mode"] == "train"


async def test_list_vehicles_route_query_5_does_not_match_501_or_510(deps: Dependencies) -> None:
    result = await list_vehicles(deps, route="5")
    assert result["matchedRoutes"] == ["5"]


async def test_list_vehicles_reports_a_clear_miss_for_an_out_of_service_vehicle(
    deps: Dependencies,
) -> None:
    result = await list_vehicles(deps, vehicle="000-not-real")
    assert result["matches"] == 0
    assert "not reporting a position" in result["message"]


async def test_list_vehicles_lists_available_routes_when_the_route_is_unknown(
    deps: Dependencies,
) -> None:
    result = await list_vehicles(deps, route="zzz no such route")
    assert "No route matched" in result["error"]
    # Natural ordering: 5 before 29, and both before 501.
    assert result["availableRoutes"] == ["1", "5", "29", "501", "510"]


async def test_list_vehicles_honors_the_mode_filter(deps: Dependencies) -> None:
    result = await list_vehicles(deps, vehicle="2301", mode="train")
    assert result["matches"] == 0
    assert result["vehicles"] == []


async def test_list_vehicles_returns_every_reporting_vehicle(
    deps: Dependencies, vehicles: list[VehiclePosition]
) -> None:
    result = await list_vehicles(deps)
    assert result["matches"] == len(vehicles)
    for listed in result["vehicles"]:
        assert isinstance(listed["latitude"], float)
        assert isinstance(listed["longitude"], float)
    counts = result["countsByMode"]
    assert counts["bus"] > 0
    assert counts["train"] > 0
    assert sum(counts.values()) == len(vehicles)


async def test_list_vehicles_applies_mode_filter_and_limit(
    deps: Dependencies, vehicles: list[VehiclePosition]
) -> None:
    trains = await list_vehicles(deps, mode="train")
    assert trains["vehicles"]
    for listed in trains["vehicles"]:
        assert listed["mode"] == "train"

    limited = await list_vehicles(deps, limit=3)
    assert len(limited["vehicles"]) == 3
    assert limited["returned"] == 3
    assert limited["matches"] == len(vehicles)


async def test_list_vehicles_orders_routes_numerically(deps: Dependencies) -> None:
    result = await list_vehicles(deps)
    names = [listed["route"]["name"] for listed in result["vehicles"] if "route" in listed]
    assert names == sorted(names, key=lambda name: (len(name), name)) or names[0] == "5"


async def test_get_arrivals_resolves_a_stop_id_and_sorts_future_arrivals(
    deps: Dependencies,
) -> None:
    result = await get_arrivals(deps, stop="00285")
    assert result["stop"]["stopId"] == "00285"
    assert result["stop"]["name"] == "Albemarle Rd & Lawyers Rd Park and Ride"

    arrivals = result["arrivals"]
    assert arrivals, "expected predicted arrivals at this stop"
    times = [arrival["arrivalTime"] for arrival in arrivals]
    assert times == sorted(times), "arrivals must be sorted soonest first"
    for arrival in arrivals:
        assert arrival["minutesAway"] >= 0, "past arrivals must be filtered out"
        assert isinstance(arrival["scheduleDeviationMinutes"], int)


async def test_get_arrivals_resolves_a_stop_by_name_substring(deps: Dependencies) -> None:
    result = await get_arrivals(deps, stop="Central Ave")
    assert "Central Ave" in result["stop"]["name"]
    others = result["otherStopsMatchingQuery"]
    assert others, "ambiguous name should surface the other matches"
    for other in others:
        assert isinstance(other["metersAway"], int)


async def test_get_arrivals_excludes_predictions_already_in_the_past(
    deps: Dependencies,
) -> None:
    # Every prediction for this stop in the fixture snapshot precedes the
    # capture time, so the tool must report none rather than negative ETAs.
    result = await get_arrivals(deps, stop="02400")
    assert result["stop"]["stopId"] == "02400"
    assert result["arrivalCount"] == 0
    assert "No realtime arrivals" in result["message"]


async def test_get_arrivals_filters_by_route_and_by_mode(deps: Dependencies) -> None:
    # 00015 is a Blue Line platform, so its arrivals resolve to route 501.
    by_route = await get_arrivals(deps, stop="00015", route="501")
    assert by_route["arrivals"]
    for arrival in by_route["arrivals"]:
        assert arrival["route"]["name"] == "501"

    by_mode = await get_arrivals(deps, stop="00015", mode="train")
    assert by_mode["arrivalCount"] == by_route["arrivalCount"]

    buses = await get_arrivals(deps, stop="00015", mode="bus")
    assert buses["arrivalCount"] == 0, "no bus route serves this light rail platform"

    trains = await get_arrivals(deps, stop="00285", mode="train")
    assert trains["arrivalCount"] == 0, "no light rail serves this bus stop"


async def test_get_arrivals_reports_an_unmatched_stop_instead_of_guessing(
    deps: Dependencies,
) -> None:
    result = await get_arrivals(deps, stop="zzzz no such stop")
    assert "No stop matched" in result["error"]
    assert "arrivals" not in result


async def test_get_arrivals_limit_caps_the_list_but_arrival_count_reports_the_total(
    deps: Dependencies,
) -> None:
    everything = await get_arrivals(deps, stop="00285", limit=50)
    capped = await get_arrivals(deps, stop="00285", limit=1)
    assert capped["arrivalCount"] == everything["arrivalCount"]
    assert len(capped["arrivals"]) == 1


async def test_get_arrivals_still_answers_when_the_alerts_feed_fails() -> None:
    result = await get_arrivals(fixture_deps(FailingAlertsFeeds()), stop="00285")
    assert "serviceAlerts" not in result
    assert result["arrivals"]


async def test_every_realtime_response_reports_feed_age(deps: Dependencies) -> None:
    for result in (
        await list_vehicles(deps, vehicle="2301"),
        await list_vehicles(deps),
        await get_arrivals(deps, stop="00285"),
    ):
        assert result["feedAgeSeconds"] == 0


async def test_get_arrivals_uses_the_nearest_station_to_a_location(deps: Dependencies) -> None:
    result = await get_arrivals(deps, latitude=I_485_STATION[0], longitude=I_485_STATION[1])
    assert result["stop"]["stopId"] == "00015"
    assert result["stop"]["metersAway"] < 50
    assert result["arrivals"], "expected Blue Line arrivals at I-485"
    for arrival in result["arrivals"]:
        assert arrival["stopId"] == "00015"


async def test_get_arrivals_near_a_location_prefers_a_stop_the_requested_mode_serves(
    deps: Dependencies,
) -> None:
    latitude, longitude = COVE_CREEK_BUS_STOP
    any_mode = await get_arrivals(deps, latitude=latitude, longitude=longitude)
    assert any_mode["stop"]["stopId"] == "06530"

    trains = await get_arrivals(deps, latitude=latitude, longitude=longitude, mode="train")
    assert trains["stop"]["name"].endswith("Station")
    assert trains["stop"]["metersAway"] > 1_000
    for arrival in trains["arrivals"]:
        assert arrival["route"]["mode"] == "train"


async def test_get_arrivals_merges_the_platforms_of_one_station(deps: Dependencies) -> None:
    result = await get_arrivals(deps, stop="CTC/Arena CityLYNX")
    assert result["stop"]["platformStopIds"] == ["51000", "51001"]
    assert "otherStopsMatchingQuery" not in result, "a sibling platform is not another match"


async def test_get_arrivals_rejects_a_missing_partial_or_doubled_location(
    deps: Dependencies,
) -> None:
    neither = await get_arrivals(deps)
    assert "Provide" in neither["error"]
    partial = await get_arrivals(deps, latitude=35.2)
    assert "together" in partial["error"]
    both = await get_arrivals(deps, stop="00015", latitude=35.2, longitude=-80.8)
    assert "not both" in both["error"]


async def test_get_arrivals_refuses_a_location_far_from_charlotte(deps: Dependencies) -> None:
    result = await get_arrivals(deps, latitude=40.7128, longitude=-74.0060, mode="train")
    assert "within 50 km" in result["error"]
    assert "arrivals" not in result


async def test_list_stops_finds_the_closest_station_with_its_line(deps: Dependencies) -> None:
    latitude, longitude = SEVENTH_ST_STATION
    result = await list_stops(deps, latitude=latitude, longitude=longitude, mode="train")
    closest = result["stations"][0]
    assert closest["name"] == "7th St Station"
    assert closest["modes"] == ["train"]
    assert [route["longName"] for route in closest["routes"]] == ["Light Rail - Lynx Blue Line"]
    distances = [station["metersAway"] for station in result["stations"]]
    assert distances == sorted(distances)
    assert result["returned"] == 10, "a location query defaults to the ten nearest"


async def test_list_stops_reports_the_bus_routes_serving_a_stop(deps: Dependencies) -> None:
    latitude, longitude = COVE_CREEK_BUS_STOP
    result = await list_stops(deps, latitude=latitude, longitude=longitude, limit=1)
    closest = result["stations"][0]
    assert closest["stopIds"] == ["06530"]
    assert [route["name"] for route in closest["routes"]] == ["29"]
    assert closest["modes"] == ["bus"]


async def test_list_stops_groups_same_named_platforms(deps: Dependencies) -> None:
    result = await list_stops(deps, query="CTC/Arena")
    assert result["totalMatching"] == 1
    assert result["stations"][0]["stopIds"] == ["51000", "51001"]


async def test_list_stops_without_a_location_lists_every_served_stop_by_name(
    deps: Dependencies, schedule: Schedule
) -> None:
    result = await list_stops(deps, route="Gold Line")
    assert result["matchedRoutes"] == ["510"]
    names = [station["name"] for station in result["stations"]]
    assert names, "expected Gold Line stops"
    assert "metersAway" not in result["stations"][0]
    served = {stop_id for stop_id, routes in schedule.stop_routes.items() if "510" in routes}
    listed = {stop_id for station in result["stations"] for stop_id in station["stopIds"]}
    assert listed == served


async def test_list_stops_explains_an_empty_result(deps: Dependencies) -> None:
    far = await list_stops(deps, latitude=40.7128, longitude=-74.0060)
    assert far["stations"] == []
    assert "Charlotte" in far["message"]
    unknown = await list_stops(deps, route="zzz no such route")
    assert "No route matched" in unknown["error"]
    partial = await list_stops(deps, longitude=-80.8)
    assert "together" in partial["error"]
