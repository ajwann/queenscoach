from __future__ import annotations

import io

from queenscoach.gtfs_csv import iter_csv, parse_csv


def test_parses_quoted_fields_containing_commas() -> None:
    rows = parse_csv('route_id,route_long_name\n1,"Mt. Holly Road, North"\n')
    assert len(rows) == 1
    assert rows[0]["route_long_name"] == "Mt. Holly Road, North"


def test_handles_escaped_quotes_crlf_and_a_bom() -> None:
    rows = parse_csv('﻿a,b\r\n"say ""hi""",2\r\n')
    assert rows[0]["a"] == 'say "hi"'
    assert rows[0]["b"] == "2"


def test_empty_fields_become_none_and_short_rows_do_not_raise() -> None:
    rows = parse_csv("a,b,c\n1,,\n2\n")
    assert rows[0]["b"] is None
    assert rows[1]["a"] == "2"
    assert rows[1]["c"] is None


def test_surrounding_whitespace_is_trimmed() -> None:
    rows = parse_csv("stop_lat,stop_lon\n  35.2 , -80.7 \n")
    assert rows[0]["stop_lat"] == "35.2"
    assert rows[0]["stop_lon"] == "-80.7"


def test_header_only_input_yields_no_rows() -> None:
    assert parse_csv("a,b\n") == []


def test_empty_input_yields_no_rows() -> None:
    assert parse_csv("") == []


def test_iter_csv_streams_only_the_requested_columns() -> None:
    lines = io.StringIO('﻿trip_id,arrival_time,stop_id\nT1,08:00:00,"00015"\n', newline="")
    assert list(iter_csv(lines, frozenset({"trip_id", "stop_id"}))) == [
        {"trip_id": "T1", "stop_id": "00015"}
    ]
