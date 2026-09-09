from __future__ import annotations

from typing import Any

from queenscoach.realtime import VehiclePosition
from queenscoach.static_gtfs import Schedule
from queenscoach.tools import Dependencies, find_vehicle, get_arrivals, list_vehicles

from .conftest import FailingAlertsFeeds, fixture_deps

CHARLOTTE_LAT = (34.9, 35.7)
CHARLOTTE_LON = (-81.2, -80.4)


def test_static_schedule_classifies_light_rail_as_train_and_buses_as_bus(
    schedule: Schedule,
) -> None:
    assert schedule.routes["501"].mode == "train"
    assert schedule.routes["510"].mode == "train"
    assert schedule.routes["29"].mode == "bus"
    assert schedule.routes["501"].long_name == "Light Rail - Lynx Blue Line"


async def test_find_vehicle_locates_one_vehicle_in_the_charlotte_area(
    deps: Dependencies,
) -> None:
    result = await find_vehicle(deps, vehicle="2301")
    assert result["matches"] == 1
    found: dict[str, Any] = result["vehicles"][0]
    assert found["vehicle"] == "2301"
    assert CHARLOTTE_LAT[0] < found["latitude"] < CHARLOTTE_LAT[1]
    assert CHARLOTTE_LON[0] < found["longitude"] < CHARLOTTE_LON[1]
    assert found["route"]["name"] == "29"
    assert found["mode"] == "bus"


async def test_find_vehicle_by_route_returns_only_that_route(deps: Dependencies) -> None:
    result = await find_vehicle(deps, route="501")
    assert result["vehicles"], "expected Blue Line trains in the fixture"
    for found in result["vehicles"]:
        assert found["route"]["name"] == "501"
        assert found["mode"] == "train"


async def test_find_vehicle_route_query_5_does_not_match_501_or_510(deps: Dependencies) -> None:
    result = await find_vehicle(deps, route="5")
    assert result["matchedRoutes"] == ["5"]


async def test_find_vehicle_requires_an_argument(deps: Dependencies) -> None:
    result = await find_vehicle(deps)
    assert "Provide" in result["error"]
    assert "vehicles" not in result


async def test_find_vehicle_reports_a_clear_miss_for_an_out_of_service_vehicle(
    deps: Dependencies,
) -> None:
    result = await find_vehicle(deps, vehicle="000-not-real")
    assert result["matches"] == 0
    assert "not reporting a position" in result["message"]


async def test_find_vehicle_lists_available_routes_when_the_route_is_unknown(
    deps: Dependencies,
) -> None:
    result = await find_vehicle(deps, route="zzz no such route")
    assert "No route matched" in result["error"]
    # Natural ordering: 5 before 29, and both before 501.
    assert result["availableRoutes"] == ["1", "5", "29", "501", "510"]


async def test_find_vehicle_honors_the_mode_filter(deps: Dependencies) -> None:
    result = await find_vehicle(deps, vehicle="2301", mode="train")
    assert result["matches"] == 0


async def test_list_vehicles_returns_every_reporting_vehicle(
    deps: Dependencies, vehicles: list[VehiclePosition]
) -> None:
    result = await list_vehicles(deps)
    assert result["totalInService"] == len(vehicles)
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
    assert limited["totalInService"] == len(vehicles)


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


async def test_every_response_reports_feed_age(deps: Dependencies) -> None:
    for result in (
        await find_vehicle(deps, vehicle="2301"),
        await list_vehicles(deps),
        await get_arrivals(deps, stop="00285"),
    ):
        assert result["feedAgeSeconds"] == 0
