from __future__ import annotations

from typing import Any

from queenscoach.cache import Cached
from queenscoach.realtime import ActivePeriod, ServiceAlert, TripUpdate, VehiclePosition
from queenscoach.static_gtfs import Schedule
from queenscoach.tools import (
    Dependencies,
    get_arrivals,
    get_route,
    get_schedule,
    get_service_alerts,
    list_stops,
    list_vehicles,
    plan_trip,
)

from .conftest import CAPTURE_TIME, FailingAlertsFeeds, FixtureFeeds, fixture_deps

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


# -- get_schedule ------------------------------------------------------------------------


async def test_get_schedule_lists_departures_from_now_with_first_and_last_of_the_day(
    deps: Dependencies,
) -> None:
    result = await get_schedule(deps, stop="7th St Station", limit=4)
    assert result["serviceDate"] == "2026-09-08"
    assert result["weekday"] == "Tuesday"
    assert result["firstDeparture"] == "2026-09-08T04:59:00-04:00"
    assert result["lastDeparture"] == "2026-09-09T01:31:00-04:00", (
        "the last train is after midnight"
    )
    departures = result["departures"]
    assert len(departures) == 4
    assert departures[0]["departsAt"] >= "2026-09-08T17:59"
    assert {departure["destination"] for departure in departures} <= {
        "I-485 Station",
        "UNC Charlotte Station",
    }
    assert result["scheduleCovers"] == {"from": "2026-09-08", "through": "2026-10-04"}


async def test_get_schedule_honours_a_time_window_and_route_filter(deps: Dependencies) -> None:
    result = await get_schedule(
        deps, stop="00001", date="2026-09-12", after="23:00", before="25:00", route="501"
    )
    assert result["weekday"] == "Saturday"
    times = [departure["departsAt"] for departure in result["departures"]]
    assert times
    assert times[0] >= "2026-09-12T23:00"
    assert times[-1] <= "2026-09-13T01:00"
    assert result["totalInWindow"] == len(times)


async def test_get_schedule_includes_the_previous_nights_trips_after_midnight(
    deps: Dependencies,
) -> None:
    result = await get_schedule(
        deps, stop="00001", date="2026-09-09", after="00:00", before="02:00"
    )
    assert result["departures"], "Tuesday's last trains leave after midnight on Wednesday"
    assert all(d["departsAt"].startswith("2026-09-09T0") for d in result["departures"])


async def test_get_schedule_finds_the_nearest_stop_a_mode_serves(deps: Dependencies) -> None:
    latitude, longitude = SEVENTH_ST_STATION
    result = await get_schedule(deps, latitude=latitude, longitude=longitude, mode="train", limit=1)
    assert result["stop"]["name"] == "7th St Station"
    assert result["stop"]["metersAway"] < 50


async def test_get_schedule_says_when_a_date_is_beyond_the_published_schedule(
    deps: Dependencies,
) -> None:
    result = await get_schedule(deps, stop="00001", date="2026-12-24")
    assert "covers 2026-09-08 through 2026-10-04" in result["error"]
    assert "departures" not in result
    bad = await get_schedule(deps, stop="00001", after="half past six")
    assert "HH:MM" in bad["error"]
    neither = await get_schedule(deps)
    assert "Provide" in neither["error"]


async def test_get_schedule_explains_a_stop_with_no_service_that_day(deps: Dependencies) -> None:
    # Route 29's fixture trips run on weekdays only.
    result = await get_schedule(deps, stop="06530", date="2026-09-13")
    assert result["departuresThatDay"] == 0
    assert "No scheduled departures" in result["message"]


# -- get_route ---------------------------------------------------------------------------


async def test_get_route_describes_each_direction_with_stops_in_order(deps: Dependencies) -> None:
    result = await get_route(deps, route="Blue Line")
    assert result["route"]["routeId"] == "501"
    north, south = result["directions"]
    assert north["destination"] == "UNC Charlotte Station"
    assert south["destination"] == "I-485 Station"
    assert north["stops"][0]["name"] == "I-485 Station"
    assert north["stops"][-1]["name"] == "UNC Charlotte Station"
    assert [stop["stopId"] for stop in south["stops"]][:2] == ["00090", "00089"]
    periods = {frequency["period"]: frequency for frequency in north["frequency"]}
    assert periods["morning rush"]["typicalMinutesBetween"] == 15
    assert north["firstDeparture"].startswith("2026-09-08T04:")


async def test_get_route_reports_the_coming_week_and_frequency_on_another_day(
    deps: Dependencies,
) -> None:
    result = await get_route(deps, route="29", date="2026-09-12")
    assert result["tripCount"] == 0
    assert "no scheduled trips" in result["message"]
    week = {day["weekday"]: day["trips"] for day in result["serviceNextSevenDays"]}
    assert week["Saturday"] == 0
    assert week["Monday"] > 0


async def test_get_route_asks_which_route_when_a_query_matches_several(
    deps: Dependencies,
) -> None:
    result = await get_route(deps, route="Line")
    assert "routes matched" in result["error"]
    assert {route["routeId"] for route in result["matchingRoutes"]} == {"501", "510"}


async def test_get_route_counts_active_alerts_on_the_route(schedule: Schedule) -> None:
    alert = _alert(route_ids=("501",), stop_ids=())
    result = await get_route(fixture_deps(FixtureFeeds(alerts=[alert])), route="501")
    assert result["activeAlerts"] == 1
    quiet = await get_route(fixture_deps(FixtureFeeds(alerts=[])), route="501")
    assert "activeAlerts" in quiet
    assert quiet["activeAlerts"] == 0


# -- get_service_alerts ------------------------------------------------------------------


def _alert(
    *,
    route_ids: tuple[str, ...],
    stop_ids: tuple[str, ...],
    start: int | None = 1_700_000_000,
    end: int | None = None,
    header: str = "Road Closed will create Detour",
) -> ServiceAlert:
    return ServiceAlert(
        header_text=header,
        description_text="Buses will detour.",
        cause="CONSTRUCTION",
        effect="DETOUR",
        severity_level="WARNING",
        informed_route_ids=route_ids,
        informed_stop_ids=stop_ids,
        active_periods=(ActivePeriod(start=start, end=end),),
        effect_detail="Detour",
    )


async def test_get_service_alerts_lists_every_current_alert_system_wide(
    deps: Dependencies,
) -> None:
    result = await get_service_alerts(deps)
    assert result["scope"] == "the CATS system"
    assert result["alertCount"] == 4
    detour = result["alerts"][0]
    assert detour["status"] == "active"
    (period,) = detour["activePeriods"]
    assert period["untilFurtherNotice"] is True
    assert "end" not in period
    assert detour["routes"] == [{"routeId": "35"}], "a route missing from the schedule keeps its id"


async def test_get_service_alerts_narrows_to_a_route_or_its_stops(schedule: Schedule) -> None:
    on_route = _alert(route_ids=("501",), stop_ids=(), header="Blue Line single-tracking")
    at_a_platform = _alert(route_ids=(), stop_ids=("00015",), header="I-485 platform closed")
    elsewhere = _alert(route_ids=("29",), stop_ids=("06530",), header="Route 29 detour")
    deps = fixture_deps(FixtureFeeds(alerts=[on_route, at_a_platform, elsewhere]))

    by_route = await get_service_alerts(deps, route="Blue Line")
    assert [alert["headerText"] for alert in by_route["alerts"]] == [
        "Blue Line single-tracking",
        "I-485 platform closed",
    ]
    assert by_route["alerts"][0]["routes"][0]["longName"] == "Light Rail - Lynx Blue Line"

    by_stop = await get_service_alerts(deps, stop="06530")
    assert [alert["headerText"] for alert in by_stop["alerts"]] == ["Route 29 detour"]
    assert by_stop["alerts"][0]["stops"] == [
        {"stopId": "06530", "name": "Cove Creek Dr & Barrington Dr"}
    ]

    latitude, longitude = I_485_STATION
    nearby = await get_service_alerts(deps, latitude=latitude, longitude=longitude)
    assert {alert["headerText"] for alert in nearby["alerts"]} == {
        "Blue Line single-tracking",
        "I-485 platform closed",
    }


async def test_get_service_alerts_leaves_out_ended_alerts_and_flags_upcoming_ones() -> None:
    now = int(CAPTURE_TIME)
    ended = _alert(route_ids=("501",), stop_ids=(), start=now - 7200, end=now - 60, header="Over")
    upcoming = _alert(
        route_ids=("501",), stop_ids=(), start=now + 86_400, end=now + 90_000, header="Soon"
    )
    deps = fixture_deps(FixtureFeeds(alerts=[ended, upcoming]))
    result = await get_service_alerts(deps, route="501")
    (only,) = result["alerts"]
    assert only["headerText"] == "Soon"
    assert only["status"] == "upcoming"
    assert only["activePeriods"][0]["end"] == "2026-09-09T18:59:48-04:00"


async def test_get_service_alerts_rejects_more_than_one_selector_and_far_locations(
    deps: Dependencies,
) -> None:
    both = await get_service_alerts(deps, route="501", stop="00015")
    assert "at most one" in both["error"]
    far = await get_service_alerts(deps, latitude=40.7128, longitude=-74.0060)
    assert "No CATS stop is within 800 m" in far["error"]
    quiet = await get_service_alerts(fixture_deps(FixtureFeeds(alerts=[])), route="29")
    assert quiet["alertCount"] == 0
    assert "No current or upcoming" in quiet["message"]


# -- plan_trip ---------------------------------------------------------------------------

#: A few hundred metres from UNC Charlotte Station.
NEAR_UNC_CHARLOTTE = (35.3082, -80.7337)


async def test_plan_trip_from_a_location_walks_rides_and_transfers(deps: Dependencies) -> None:
    latitude, longitude = NEAR_UNC_CHARLOTTE
    result = await plan_trip(
        deps,
        origin_latitude=latitude,
        origin_longitude=longitude,
        destination_stop="French St CityLYNX",
    )
    first = result["itineraries"][0]
    kinds = [leg["type"] for leg in first["legs"]]
    assert kinds == ["walk", "ride", "walk", "ride"]
    walk, blue, change, gold = first["legs"]
    assert walk["from"] == {"name": "Your location"}
    assert walk["to"]["name"] == "UNC Charlotte Station"
    assert blue["route"]["routeId"] == "501"
    assert blue["destination"] == "I-485 Station"
    assert (change["from"]["name"], change["to"]["name"]) == ("CTC Station", "CTC/Arena CityLYNX")
    assert gold["route"]["routeId"] == "510"
    assert "transferWaitMinutes" not in blue
    assert gold["transferWaitMinutes"] >= 2
    assert first["transfers"] == 1
    assert first["departAt"] >= result["searchedFor"]["departAt"]
    assert any("straight-line" in note for note in result["notes"])


async def test_plan_trip_arrive_by_ends_in_time_on_a_given_date(deps: Dependencies) -> None:
    result = await plan_trip(
        deps,
        origin_stop="UNC Charlotte Station",
        destination_stop="CTC Station",
        date="2026-09-12",
        arrive_by="09:00",
    )
    assert result["searchedFor"] == {"arriveBy": "2026-09-12T09:00:00-04:00"}
    arrivals = [itinerary["arriveAt"] for itinerary in result["itineraries"]]
    assert arrivals
    assert all(arrival <= "2026-09-12T09:00:00-04:00" for arrival in arrivals)
    assert all(itinerary["transfers"] == 0 for itinerary in result["itineraries"])
    assert not any("live" in note for note in result["notes"]), "Saturday is not now"


async def test_plan_trip_offers_walking_when_the_places_are_close(deps: Dependencies) -> None:
    ctc, arena = (35.225336, -80.840975), (35.224767, -80.840221)
    result = await plan_trip(
        deps,
        origin_latitude=ctc[0],
        origin_longitude=ctc[1],
        destination_latitude=arena[0],
        destination_longitude=arena[1],
    )
    assert result["walkingIsAnOption"] == {"meters": 122, "minutes": 2}


async def test_plan_trip_explains_what_is_missing_or_impossible(deps: Dependencies) -> None:
    both_times = await plan_trip(
        deps, origin_stop="00090", destination_stop="00002", depart_at="08:00", arrive_by="09:00"
    )
    assert "not both" in both_times["error"]

    no_destination = await plan_trip(deps, origin_stop="00090")
    assert "destination" in no_destination["error"]

    stranded = await plan_trip(
        deps, origin_latitude=40.7128, origin_longitude=-74.0060, destination_stop="00002"
    )
    assert "within a 800 m walk of the starting point" in stranded["error"]

    later = await plan_trip(deps, origin_stop="00090", destination_stop="00002", date="2026-09-20")
    assert "depart_at" in later["error"]

    same = await plan_trip(deps, origin_stop="CTC/Arena CityLYNX", destination_stop="51001")
    assert "same stop" in same["error"]

    beyond = await plan_trip(
        deps, origin_stop="00090", destination_stop="00002", date="2027-01-05", depart_at="08:00"
    )
    assert "covers" in beyond["error"]


async def test_plan_trip_reports_when_no_trip_fits(deps: Dependencies) -> None:
    result = await plan_trip(deps, origin_stop="00090", destination_stop="51016", max_transfers=0)
    assert result["itineraries"] == []
    assert "No trip with at most 0 transfers" in result["message"]


async def test_plan_trip_falls_back_to_the_timetable_when_live_predictions_fail() -> None:
    class NoTripUpdates(FixtureFeeds):
        async def trip_updates(self) -> Cached[list[TripUpdate]]:
            raise RuntimeError("trip updates down")

    result = await plan_trip(
        fixture_deps(NoTripUpdates()), origin_stop="00090", destination_stop="00002"
    )
    assert result["itineraries"]
    assert any("unavailable" in note for note in result["notes"])


async def test_get_arrivals_alerts_now_name_their_routes_and_period() -> None:
    alert = _alert(route_ids=("501",), stop_ids=("00015",))
    result = await get_arrivals(fixture_deps(FixtureFeeds(alerts=[alert])), stop="00015")
    (attached,) = result["serviceAlerts"]
    assert attached["routes"][0]["routeId"] == "501"
    assert attached["activePeriods"][0]["untilFurtherNotice"] is True
