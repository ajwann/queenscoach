from __future__ import annotations

import io
import zipfile

import pytest

from queenscoach.static_gtfs import Schedule, StaticGtfsError, parse_schedule

_ROUTES = (
    "route_id,route_short_name,route_long_name,route_type\n501,501,Blue Line,0\n9,9,Central,3\n"
)
_STOPS = "stop_id,stop_code,stop_name,stop_lat,stop_lon\n1,C1,Main St,35.2,-80.8\n"
_TRIPS = "trip_id,route_id,trip_headsign\nT1,9,Uptown\n"


def _archive(**files: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _default_archive(**overrides: str) -> bytes:
    files = {"routes.txt": _ROUTES, "stops.txt": _STOPS, "trips.txt": _TRIPS}
    files.update(overrides)
    return _archive(**files)


def test_parses_the_three_tables_it_needs() -> None:
    schedule = parse_schedule(_default_archive())
    assert schedule.routes["501"].mode == "train"
    assert schedule.routes["9"].mode == "bus"
    assert schedule.stops["1"].latitude == pytest.approx(35.2)
    assert schedule.trips["T1"].headsign == "Uptown"


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


def test_a_non_zip_payload_is_reported_as_such() -> None:
    with pytest.raises(StaticGtfsError, match="zip file"):
        parse_schedule(b"this is not a zip archive")


def test_the_real_capture_parses_into_all_three_tables(schedule: Schedule) -> None:
    assert len(schedule.routes) == 5
    assert len(schedule.stops) == 300
    assert len(schedule.trips) == 400
