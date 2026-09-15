from __future__ import annotations

from datetime import date

import pytest

from queenscoach import planner
from queenscoach.realtime import StopTimeUpdate, TripUpdate
from queenscoach.static_gtfs import Schedule
from queenscoach.timetable import service_day_origin

from .conftest import CAPTURE_TIME

TUESDAY = date(2026, 9, 8)
UNC_CHARLOTTE = "00090"
CTC_BLUE = "00002"
FRENCH_ST_GOLD = "51016"
EVENING = int(CAPTURE_TIME)  # 17:59:48 on Tuesday 8 September


def test_a_direct_ride_needs_no_transfer(schedule: Schedule) -> None:
    (first, *_) = planner.plan(schedule, {UNC_CHARLOTTE: 0}, {CTC_BLUE: 0}, depart_at=EVENING)
    (ride,) = first.rides
    assert schedule.trips[ride.trip_id].route_id == "501"
    assert ride.departs_at >= EVENING
    assert ride.arrives_at - ride.departs_at == 34 * 60


def test_a_transfer_between_lines_walks_to_the_nearest_platform(schedule: Schedule) -> None:
    itineraries = planner.plan(schedule, {UNC_CHARLOTTE: 0}, {FRENCH_ST_GOLD: 0}, depart_at=EVENING)
    assert len(itineraries) == 3
    blue, walk, gold = itineraries[0].legs
    assert isinstance(blue, planner.RideLeg) and isinstance(gold, planner.RideLeg)
    assert isinstance(walk, planner.WalkLeg)
    assert (blue.alight_stop, walk.to_stop) == (CTC_BLUE, gold.board_stop)
    assert walk.meters == 122
    assert gold.departs_at >= walk.arrives_at + planner.TRANSFER_BUFFER_SECONDS - 60
    arrivals = [itinerary.arrives_at for itinerary in itineraries]
    assert arrivals == sorted(arrivals)


def test_limiting_rides_rules_out_journeys_that_need_a_transfer(schedule: Schedule) -> None:
    assert (
        planner.plan(
            schedule, {UNC_CHARLOTTE: 0}, {FRENCH_ST_GOLD: 0}, depart_at=EVENING, max_rides=1
        )
        == []
    )


def test_arrive_by_finds_the_latest_departure_that_still_arrives_in_time(
    schedule: Schedule,
) -> None:
    deadline = EVENING + 2 * 3600
    itineraries = planner.plan(
        schedule, {UNC_CHARLOTTE: 0}, {FRENCH_ST_GOLD: 0}, arrive_by=deadline
    )
    assert itineraries
    assert all(itinerary.arrives_at <= deadline for itinerary in itineraries)
    departures = [itinerary.departs_at for itinerary in itineraries]
    assert departures == sorted(departures, reverse=True)
    # Ties in the backward scan must not add walking: the transfer is still at CTC.
    assert all(itinerary.walk_meters == 122 for itinerary in itineraries)


def test_walks_at_either_end_are_timed_around_the_rides(schedule: Schedule) -> None:
    (itinerary,) = planner.plan(
        schedule, {UNC_CHARLOTTE: 150}, {CTC_BLUE: 80}, depart_at=EVENING, limit=1
    )
    opening, ride, closing = itinerary.legs
    assert isinstance(opening, planner.WalkLeg) and isinstance(closing, planner.WalkLeg)
    assert (opening.from_stop, opening.to_stop, opening.meters) == (None, UNC_CHARLOTTE, 150)
    assert opening.departs_at >= EVENING
    assert opening.arrives_at <= ride.departs_at
    assert closing.departs_at == ride.arrives_at
    assert itinerary.walk_meters == 230


def test_no_itinerary_is_beaten_by_another_on_every_count(schedule: Schedule) -> None:
    itineraries = planner.plan(
        schedule, {UNC_CHARLOTTE: 0}, {FRENCH_ST_GOLD: 0}, arrive_by=EVENING + 2 * 3600
    )
    for first in itineraries:
        for second in itineraries:
            if first is not second:
                assert not (
                    first.departs_at >= second.departs_at
                    and first.arrives_at <= second.arrives_at
                    and len(first.rides) <= len(second.rides)
                    and (first.departs_at, first.arrives_at)
                    != (second.departs_at, second.arrives_at)
                )


def test_unknown_stops_and_a_missing_time_are_handled(schedule: Schedule) -> None:
    assert planner.plan(schedule, {"nope": 0}, {CTC_BLUE: 0}, depart_at=EVENING) == []
    with pytest.raises(ValueError, match="exactly one"):
        planner.plan(schedule, {UNC_CHARLOTTE: 0}, {CTC_BLUE: 0})


def test_live_delays_shift_a_trip_and_mark_it_live(schedule: Schedule) -> None:
    (scheduled,) = planner.plan(
        schedule, {UNC_CHARLOTTE: 0}, {CTC_BLUE: 0}, depart_at=EVENING, limit=1
    )
    (ride,) = scheduled.rides
    timetable = schedule.timetables[ride.trip_id]
    origin = service_day_origin(TUESDAY, schedule.zone)
    late = 5 * 60
    update = TripUpdate(
        trip_id=ride.trip_id,
        route_id="501",
        vehicle_label=None,
        vehicle_id=None,
        stop_time_updates=(
            StopTimeUpdate(
                stop_id=UNC_CHARLOTTE,
                stop_sequence=timetable.stop_sequences[0],
                arrival_time=origin + timetable.arrivals[0] + late,
                scheduled_arrival_time=origin + timetable.arrivals[0],
                departure_time=origin + timetable.departures[0] + late,
                schedule_relationship="SCHEDULED",
            ),
        ),
    )
    delays = planner.live_delays(schedule, [update], EVENING)
    assert set(delays[(ride.trip_id, TUESDAY)]) == {late}, "the delay carries to every later call"

    (live,) = planner.plan(
        schedule, {UNC_CHARLOTTE: 0}, {CTC_BLUE: 0}, depart_at=EVENING, delays=delays, limit=1
    )
    (live_ride,) = live.rides
    assert live_ride.trip_id == ride.trip_id
    assert live_ride.live
    assert live_ride.departs_at == ride.departs_at + late
    assert live_ride.scheduled_departs_at == ride.departs_at


def test_a_prediction_for_a_trip_not_running_now_is_ignored(schedule: Schedule) -> None:
    trip_id, timetable = next(iter(schedule.timetables.items()))
    update = TripUpdate(
        trip_id=trip_id,
        route_id=None,
        vehicle_label=None,
        vehicle_id=None,
        stop_time_updates=(
            StopTimeUpdate(
                stop_id=timetable.stop_ids[0],
                stop_sequence=None,
                arrival_time=1,
                scheduled_arrival_time=None,
                departure_time=1,
                schedule_relationship=None,
            ),
        ),
    )
    # A week after the capture, on a Sunday, no weekday or Saturday trip runs at this hour.
    assert planner.live_delays(schedule, [update], EVENING + 5 * 86_400) == {}


def test_walking_estimates_stretch_straight_lines(schedule: Schedule) -> None:
    ctc, arena = schedule.stops[CTC_BLUE], schedule.stops["51001"]
    meters = planner.walk_meters(ctc.latitude, ctc.longitude, arena.latitude, arena.longitude)
    assert meters == 122
    assert planner.walk_seconds(meters) == 98
