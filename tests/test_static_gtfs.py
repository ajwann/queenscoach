from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from queenscoach import static_gtfs
from queenscoach.static_gtfs import (
    DEFAULT_TIMEZONE,
    Schedule,
    StaticGtfsError,
    parse_gtfs_time,
    parse_schedule,
)

_ROUTES = (
    "route_id,route_short_name,route_long_name,route_type\n501,501,Blue Line,0\n9,9,Central,3\n"
)
_STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
    "1,C1,Main St,35.2,-80.8\n"
    "2,,Main St Station,35.21,-80.8\n"
)
_TRIPS = (
    "trip_id,route_id,trip_headsign,service_id,direction_id\n"
    "T1,9,Uptown,WEEKDAY,0\n"
    "T2,501,South,WEEKDAY,1\n"
    "T3,9,Outbound,SATURDAY,1\n"
)
_STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
    "T1,08:00:00,08:00:00,1,1,,\n"
    "T1,08:04:00,08:05:00,2,2,,\n"
    "T2,24:50:00,24:50:00,2,1,0,1\n"
    "T2,25:10:00,25:10:00,1,2,1,0\n"
    "T3,08:10:00,08:10:00,1,1,,\n"
    "T3,,,2,2,,\n"
    "T3,08:30:00,08:30:00,1,3,,\n"
    "UNKNOWN,08:20:00,08:20:00,1,3,,\n"
)


def _archive(**files: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _default_archive(**overrides: str) -> bytes:
    files = {
        "routes.txt": _ROUTES,
        "stops.txt": _STOPS,
        "trips.txt": _TRIPS,
        "stop_times.txt": _STOP_TIMES,
    }
    files.update(overrides)
    return _archive(**files)


def test_parses_the_tables_it_needs() -> None:
    schedule = parse_schedule(_default_archive())
    assert schedule.routes["501"].mode == "train"
    assert schedule.routes["9"].mode == "bus"
    assert schedule.stops["1"].latitude == pytest.approx(35.2)
    assert schedule.trips["T1"].headsign == "Uptown"
    assert schedule.trips["T1"].service_id == "WEEKDAY"
    assert schedule.trips["T2"].direction_id == "1"


def test_stop_times_become_the_set_of_routes_serving_each_stop() -> None:
    schedule = parse_schedule(_default_archive())
    # A stop_times row naming an unknown trip contributes nothing.
    assert schedule.stop_routes == {"1": frozenset({"9", "501"}), "2": frozenset({"501", "9"})}


def test_each_trip_becomes_a_timetable_in_stop_order() -> None:
    timetable = parse_schedule(_default_archive()).timetables["T1"]
    assert timetable.stop_ids == ("1", "2")
    assert list(timetable.arrivals) == [8 * 3600, 8 * 3600 + 240]
    assert list(timetable.departures) == [8 * 3600, 8 * 3600 + 300]


def test_times_past_midnight_stay_on_their_service_day() -> None:
    timetable = parse_schedule(_default_archive()).timetables["T2"]
    assert list(timetable.departures) == [24 * 3600 + 3000, 25 * 3600 + 600]


def test_boarding_and_alighting_follow_pickup_and_drop_off_types() -> None:
    timetable = parse_schedule(_default_archive()).timetables["T2"]
    assert timetable.boards_at(0)
    assert not timetable.alights_at(0), "the first call never lets riders off"
    assert timetable.alights_at(1)
    assert not timetable.boards_at(1), "the last call never boards"


def test_an_untimed_call_is_placed_between_its_timed_neighbours() -> None:
    timetable = parse_schedule(_default_archive()).timetables["T3"]
    assert list(timetable.departures) == [8 * 3600 + 600, 8 * 3600 + 1200, 8 * 3600 + 1800]


def test_rows_of_one_trip_split_across_the_file_are_merged() -> None:
    stop_times = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:04:00,08:05:00,2,2\n"
        "T3,08:10:00,08:10:00,1,1\n"
        "T3,08:30:00,08:30:00,2,3\n"
        "T1,08:00:00,08:00:00,1,1\n"
    )
    schedule = parse_schedule(_default_archive(**{"stop_times.txt": stop_times}))
    assert schedule.timetables["T1"].stop_ids == ("1", "2")
    assert list(schedule.timetables["T1"].stop_sequences) == [1, 2]


def test_a_trip_with_fewer_than_two_timed_calls_has_no_timetable() -> None:
    stop_times = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:00:00,1,1\n"
    )
    schedule = parse_schedule(_default_archive(**{"stop_times.txt": stop_times}))
    assert "T1" not in schedule.timetables
    assert "T1" in schedule.trips


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("08:05:30", 29130), ("5:00:00", 18000), ("25:10:00", 90600), ("8:60:00", None), ("", None)],
)
def test_gtfs_clock_times_parse_to_seconds(raw: str, seconds: int | None) -> None:
    assert parse_gtfs_time(raw) == seconds


def test_calendar_patterns_and_date_exceptions_combine() -> None:
    calendar = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WEEKDAY,1,1,1,1,1,0,0,20260907,20260913\n"
    )
    exceptions = (
        "service_id,date,exception_type\n"
        "WEEKDAY,20260907,2\n"  # Labor Day: no weekday service
        "SATURDAY,20260907,1\n"  # ...and Saturday service instead
    )
    schedule = parse_schedule(
        _default_archive(**{"calendar.txt": calendar, "calendar_dates.txt": exceptions})
    )
    assert schedule.calendar.services_on(date(2026, 9, 7)) == {"SATURDAY"}
    assert schedule.calendar.services_on(date(2026, 9, 8)) == {"WEEKDAY"}
    assert schedule.calendar.services_on(date(2026, 9, 12)) == frozenset()
    assert schedule.calendar.first_date == date(2026, 9, 7)
    assert schedule.calendar.last_date == date(2026, 9, 11)


def test_the_timezone_comes_from_agency_txt() -> None:
    agency = "agency_id,agency_name,agency_url,agency_timezone\nX,Transit,http://x.test,America/Chicago\n"
    assert parse_schedule(_default_archive(**{"agency.txt": agency})).timezone == "America/Chicago"
    assert parse_schedule(_default_archive()).timezone == DEFAULT_TIMEZONE
    bogus = "agency_id,agency_name,agency_url,agency_timezone\nX,Transit,http://x.test,Mars/Base\n"
    assert parse_schedule(_default_archive(**{"agency.txt": bogus})).timezone == DEFAULT_TIMEZONE


def test_an_oversized_table_is_refused_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(static_gtfs, "_MAX_TABLE_BYTES", len(_STOP_TIMES) - 1)
    with pytest.raises(StaticGtfsError, match=r"stop_times\.txt .* implausibly large"):
        parse_schedule(_default_archive())


def test_stops_without_usable_coordinates_are_skipped() -> None:
    stops = "stop_id,stop_name,stop_lat,stop_lon\n1,Main St,,-80.8\n2,Elm St,nope,-80.8\n"
    with pytest.raises(StaticGtfsError, match="no routes or stops"):
        parse_schedule(_default_archive(**{"stops.txt": stops}))


def test_a_route_without_a_short_name_falls_back_to_its_id() -> None:
    routes = "route_id,route_long_name,route_type\n77,Airport,3\n"
    schedule = parse_schedule(_default_archive(**{"routes.txt": routes}))
    assert schedule.routes["77"].short_name == "77"


def test_a_missing_table_is_reported_by_name() -> None:
    with pytest.raises(StaticGtfsError, match=r"missing trips\.txt"):
        parse_schedule(_archive(**{"routes.txt": _ROUTES, "stops.txt": _STOPS}))
    with pytest.raises(StaticGtfsError, match=r"missing stop_times\.txt"):
        parse_schedule(
            _archive(**{"routes.txt": _ROUTES, "stops.txt": _STOPS, "trips.txt": _TRIPS})
        )


def test_a_non_zip_payload_is_reported_as_such() -> None:
    with pytest.raises(StaticGtfsError, match="zip file"):
        parse_schedule(b"this is not a zip archive")


def test_the_real_capture_parses_into_every_table(schedule: Schedule) -> None:
    assert len(schedule.routes) == 5
    assert len(schedule.stops) == 324
    assert len(schedule.trips) == 498
    # The fixture keeps only its own stops, which leaves nine trips a single call.
    assert len(schedule.timetables) == 489
    assert schedule.stop_routes["00015"] == {"501"}
    assert schedule.stop_routes["51000"] == {"510"}
    assert schedule.timezone == "America/New_York"
    assert schedule.calendar.first_date == date(2026, 9, 8)
    assert schedule.calendar.last_date == date(2026, 10, 4)
