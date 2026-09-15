from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from queenscoach.static_gtfs import Schedule, Trip
from queenscoach.timetable import (
    departures_at,
    frequencies,
    informative_headsign,
    iso_local,
    most_common_destination,
    parse_clock,
    route_directions,
    service_dates_from,
    service_day_origin,
    trips_running,
)

CHARLOTTE = ZoneInfo("America/New_York")
TUESDAY = date(2026, 9, 8)


def test_a_service_day_starts_at_local_midnight_on_an_ordinary_day() -> None:
    origin = service_day_origin(TUESDAY, CHARLOTTE)
    assert datetime.fromtimestamp(origin, tz=CHARLOTTE).isoformat() == "2026-09-08T00:00:00-04:00"


def test_on_a_daylight_saving_change_clock_times_still_count_from_noon_minus_twelve_hours() -> None:
    # Clocks fall back at 2 am on 1 November 2026, so that day is 25 hours long.
    origin = service_day_origin(date(2026, 11, 1), CHARLOTTE)
    noon = datetime(2026, 11, 1, 12, tzinfo=CHARLOTTE).timestamp()
    assert origin + 12 * 3600 == noon
    # So a GTFS time of 08:00:00 is still 8 am on the clock, even after the change.
    assert iso_local(origin + 8 * 3600, CHARLOTTE) == "2026-11-01T08:00:00-05:00"


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (1_788_904_788, "2026-09-08T17:59:48-04:00"),  # summer: Eastern Daylight Time
        (1_797_897_600, "2026-12-21T19:00:00-05:00"),  # winter: Eastern Standard Time
        # 1 November 2026: 1:30 am happens twice as clocks fall back.
        (1_793_511_000, "2026-11-01T01:30:00-04:00"),
        (1_793_514_600, "2026-11-01T01:30:00-05:00"),
        # 14 March 2027: clocks spring forward from 2:00 straight to 3:00.
        (1_805_007_540, "2027-03-14T01:59:00-05:00"),
        (1_805_007_600, "2027-03-14T03:00:00-04:00"),
    ],
)
def test_times_are_reported_in_eastern_across_daylight_saving_changes(
    epoch: int, expected: str
) -> None:
    assert iso_local(epoch, CHARLOTTE) == expected


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("08:30", 30600), ("8:05", 29100), ("25:15", 90900), ("24:60", None), ("x", None)],
)
def test_clock_times_parse_including_hours_past_midnight(raw: str, seconds: int | None) -> None:
    assert parse_clock(raw) == seconds


@pytest.mark.parametrize(
    ("headsign", "kept"),
    [
        ("Outbound", None),
        ("inbound", None),
        ("Northbound", None),
        ("West", None),
        ("North to UNCC", "North to UNCC"),
    ],
)
def test_headsigns_that_only_name_a_direction_are_not_informative(
    headsign: str, kept: str | None
) -> None:
    trip = Trip(trip_id="T", route_id="5", headsign=headsign)
    assert informative_headsign(trip) == kept


def test_only_trips_whose_service_runs_that_day_are_running(schedule: Schedule) -> None:
    weekday = trips_running(schedule, TUESDAY, route_ids=frozenset({"501"}))
    saturday = trips_running(schedule, date(2026, 9, 12), route_ids=frozenset({"501"}))
    assert weekday and saturday
    assert all("Weekday" in (trip.service_id or "") for trip, _ in weekday)
    assert all("Saturday" in (trip.service_id or "") for trip, _ in saturday)
    assert trips_running(schedule, date(2026, 10, 5)) == [], "after the calendar ends"


def test_departures_are_boardings_in_time_order_with_real_destinations(
    schedule: Schedule,
) -> None:
    departures = departures_at(schedule, frozenset({"00001"}), [TUESDAY])
    assert departures
    times = [departure.departs_at for departure in departures]
    assert times == sorted(times)
    assert {departure.destination for departure in departures} == {
        "I-485 Station",
        "UNC Charlotte Station",
    }
    last = datetime.fromtimestamp(departures[-1].departs_at, tz=CHARLOTTE)
    assert (last.date(), last.hour) == (date(2026, 9, 9), 1), "the last train leaves after midnight"


def test_a_terminal_stop_has_no_departures_toward_itself(schedule: Schedule) -> None:
    departures = departures_at(schedule, frozenset({"00090"}), [TUESDAY])
    assert departures
    assert all(departure.destination != "UNC Charlotte Station" for departure in departures)


def test_frequencies_measure_gaps_within_each_period() -> None:
    minute = 60
    starts = [
        5 * 3600,
        5 * 3600 + 30 * minute,
        7 * 3600,
        7 * 3600 + 10 * minute,
        7 * 3600 + 30 * minute,
    ]
    by_period = {frequency.period.name: frequency for frequency in frequencies(starts)}
    assert by_period["early morning"].typical_minutes_between == 30
    assert by_period["morning rush"].trips == 3
    assert by_period["morning rush"].typical_minutes_between == 15
    assert "midday" not in by_period


def test_a_period_with_one_trip_has_no_typical_gap() -> None:
    (only,) = frequencies([12 * 3600])
    assert only.trips == 1
    assert only.typical_minutes_between is None


def test_route_directions_group_trips_and_lead_with_the_busiest_pattern(
    schedule: Schedule,
) -> None:
    trips = trips_running(schedule, TUESDAY, route_ids=frozenset({"501"}))
    directions = route_directions(trips)
    assert [direction.direction_id for direction in directions] == ["0", "1"]
    for direction in directions:
        counts = [len(pattern.trips) for pattern in direction.patterns]
        assert counts == sorted(counts, reverse=True)
        assert sum(counts) == len(direction.trips)
    assert most_common_destination(schedule, directions[0].trips) == "UNC Charlotte Station"
    assert most_common_destination(schedule, directions[1].trips) == "I-485 Station"


def test_service_dates_stop_where_the_calendar_does(schedule: Schedule) -> None:
    dates = service_dates_from(schedule, date(2026, 10, 1), 7)
    assert dates == [date(2026, 10, day) for day in (1, 2, 3, 4)]
