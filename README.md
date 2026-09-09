# queenscoach

An MCP server for live **Charlotte Area Transit System (CATS)** bus and light rail data,
built on the agency's public GTFS-Realtime feeds. It runs over stdio, launched by the
MCP client that uses it.

## Tools

| Tool | Purpose |
| --- | --- |
| `find_vehicle` | Locate one bus/train by vehicle number, or every vehicle on a route, and return GPS coordinates. |
| `list_vehicles` | Current GPS coordinates of every bus and train in service. |
| `get_arrivals` | Estimated arrival times at a specific stop or station. |

### `find_vehicle`

| Argument | Type | Notes |
| --- | --- | --- |
| `vehicle` | string | Vehicle number as shown on the bus/train, e.g. `2301`, `LRV307`. |
| `route` | string | Route to locate: `9`, `501`, `Blue Line`, `Mt. Holly Road`. |
| `mode` | `bus` \| `train` | Optional filter. |

At least one of `vehicle` or `route` is required. Returns position, heading, speed,
occupancy, headsign, and the next scheduled stop.

### `list_vehicles`

| Argument | Type | Notes |
| --- | --- | --- |
| `mode` | `bus` \| `train` | Optional filter. |
| `route` | string | Optional single-route filter. |
| `limit` | integer | Max vehicles to return (default and cap: 250). |

Includes `countsByMode` and `totalInService` so the total is visible even when the
list is truncated.

### `get_arrivals`

| Argument | Type | Notes |
| --- | --- | --- |
| `stop` | string | **Required.** Stop id (`02400`), stop code, or part of a stop name (`CTC Station`). |
| `route` | string | Optional route filter. |
| `mode` | `bus` \| `train` | Optional filter. |
| `limit` | integer | Max arrivals (default 10, cap 50). |

Returns minutes away, predicted and scheduled times, schedule deviation, the vehicle
number, and that vehicle's live position. When a name query is ambiguous, the best
match is used and the runners-up are listed under `otherStopsMatchingQuery`. Service
alerts affecting the stop or its routes are attached when present.

## Install

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

## Run it

Register it with Claude Code:

```bash
claude mcp add cats -- /absolute/path/to/queenscoach/.venv/bin/queenscoach
```

Or in an MCP client config file:

```json
{
  "mcpServers": {
    "cats": {
      "command": "/absolute/path/to/queenscoach/.venv/bin/queenscoach"
    }
  }
}
```

`python -m queenscoach` runs the same server, so any interpreter with the package
installed works as the command.

stdout carries MCP protocol traffic only; all diagnostics go to stderr.

## Data sources

Realtime (GTFS-Realtime protobuf, refreshed every 20s):

- `https://gtfsrealtime.ridetransit.org/GTFSRealTime/Vehicle/VehiclePositions.pb`
- `https://gtfsrealtime.ridetransit.org/GTFSRealTime/TripUpdate/TripUpdates.pb`
- `https://gtfsrealtime.ridetransit.org/GTFSRealTime/Alert/Alerts.pb`

Static schedule (cached 6h), used to turn feed identifiers into route names, stop
names, and coordinates:

- `https://gtfsrealtime.ridetransit.org/GTFSStatic/api/GTFSDownload/GTFS.zip`

Only `routes.txt`, `stops.txt`, and `trips.txt` are read; `stop_times.txt` and
`shapes.txt` are the bulk of the archive and are not needed.

## Feed quirks this server works around

Verified against live feed captures:

- **`VehiclePosition.stop_id` and `current_stop_sequence` are unusable.** None of the
  158 vehicle stop ids in a sample capture matched any stop in the published schedule,
  and reported sequence numbers exceeded the trip's own stop count (e.g. sequence 192
  on a 52-stop trip). This server never surfaces them; next-stop data comes from the
  TripUpdates feed instead, whose stop ids resolve 100%.
- **`StopTimeEvent.delay` is never populated.** Schedule deviation is computed from
  `time` minus `scheduled_time`, which are both present.
- **TripUpdates cover ~83% of active vehicles**, so `nextStop` is omitted rather than
  guessed for the remainder.
- **Route matching is exact-first**, so a query of `5` returns route 5, not 501 or 510.

## Behavior notes

- Arrival predictions already in the past are filtered out; no negative ETAs.
- Feed responses are capped in size and time-bounded; one slow feed cannot hang a call.
- Concurrent calls share a single in-flight fetch per feed, and one call giving up does
  not abort a fetch the others are awaiting.
- If a refresh fails but cached data exists, the last good data is served rather than
  an error. `feedAgeSeconds` on every response shows how stale it is.
- The alerts feed is supplementary: if it fails, `get_arrivals` still returns arrivals.
- Times are ISO 8601 UTC; coordinates are WGS84 decimal degrees.

## Configuration

All optional; defaults target the CATS feeds above. Durations are in milliseconds.

| Variable | Default |
| --- | --- |
| `CATS_VEHICLE_POSITIONS_URL` | CATS vehicle positions feed |
| `CATS_TRIP_UPDATES_URL` | CATS trip updates feed |
| `CATS_ALERTS_URL` | CATS alerts feed |
| `CATS_STATIC_GTFS_URL` | CATS static GTFS zip |
| `CATS_REALTIME_TTL_MS` | `20000` |
| `CATS_STATIC_TTL_MS` | `21600000` |
| `CATS_REQUEST_TIMEOUT_MS` | `30000` |
| `CATS_MAX_FEED_BYTES` | `33554432` |
| `CATS_MAX_STATIC_BYTES` | `268435456` |

Feed URLs must be `http` or `https`; anything else is rejected at startup.

## Layout

| Module | Role |
| --- | --- |
| `config.py` | Environment parsing and validation |
| `feed_http.py` | Bounded, time-limited HTTP fetch |
| `cache.py` | TTL cache with single-flight refresh |
| `gtfs_csv.py` | GTFS-flavored CSV reading |
| `static_gtfs.py` | Static schedule: routes, stops, trips |
| `realtime.py` | GTFS-Realtime protobuf decoding |
| `transit.py` | Domain layer: joins realtime to schedule, resolves queries |
| `tools.py` | The three tools' behavior and JSON payloads |
| `server.py` | MCP tool registration and schemas |
| `main.py` | stdio entry point |

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest        # 80 tests, offline against recorded feed fixtures
.venv/bin/mypy          # strict
.venv/bin/ruff check .
.venv/bin/ruff format .
```

Tests run against protobuf and GTFS fixtures captured from the live feeds, so they are
deterministic and make no network calls. `tests/test_feed_http.py` is the exception: it
serves canned responses from a loopback socket so the byte cap and timeout are exercised
for real.
