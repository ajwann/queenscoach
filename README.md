# queenscoach

An MCP server for live **Charlotte Area Transit System (CATS)** bus and light rail data,
built on the agency's public GTFS-Realtime feeds.

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

```bash
npm install
npm run build
```

## Two ways to run it

The tools are identical either way; only the transport differs.

| Entry point | Transport | Use for |
| --- | --- | --- |
| `dist/index.js` | stdio | Claude Code / Claude Desktop on the same machine |
| `dist/http-server.js` | Streamable HTTP | A hosted server, e.g. a Claude custom connector (works on phones) |

### Local (stdio)

```bash
claude mcp add cats -- node /absolute/path/to/queenscoach/dist/index.js
```

Or in an MCP client config file:

```json
{
  "mcpServers": {
    "cats": {
      "command": "node",
      "args": ["/absolute/path/to/queenscoach/dist/index.js"]
    }
  }
}
```

### Remote (HTTP)

```bash
export CATS_AUTH_TOKEN=$(openssl rand -hex 32)
npm run start:http          # listens on :8080, MCP endpoint at /mcp
```

Endpoints:

| Path | Auth | Purpose |
| --- | --- | --- |
| `POST /mcp` | `Authorization: Bearer <token>` | The MCP endpoint |
| `POST /mcp/<token>` | Token in the URL | Same endpoint, for clients that send only a URL |
| `GET /healthz` | None | Liveness check for the hosting platform |

The two authenticated forms are equivalent. The URL form exists because Claude's
custom-connector dialog accepts only a URL — it has no field for a static
header — so it is the way to give a hosted connector a shared secret without
implementing OAuth. Treat that URL as the secret: it is as sensitive as a
password, and it will appear in proxy and platform access logs.

The server runs **stateless** — no session ids, a fresh MCP server per request,
with the feed caches shared across requests. It restarts and scales horizontally
without losing anything.

**Authentication fails closed.** The process refuses to start unless
`CATS_AUTH_TOKEN` is set (minimum 24 characters). To run it deliberately open,
set `CATS_ALLOW_ANONYMOUS=true`. Tokens are compared in constant time.

#### Deploying

A `Dockerfile` (multi-stage, non-root, with a healthcheck) and a `fly.toml` are
included.

```bash
fly launch --no-deploy --copy-config
fly secrets set CATS_AUTH_TOKEN=$(openssl rand -hex 32)
fly deploy
```

Any container host works — the image only needs `PORT` and `CATS_AUTH_TOKEN`.

#### Using it as a Claude custom connector

1. Deploy so the server has a public HTTPS URL.
2. At **claude.ai → Settings → Connectors → Add custom connector**, enter
   `https://<your-host>/mcp`.
3. It syncs to the iOS and Android apps. Connectors cannot be *added* from a
   phone — add it on the web, then use it anywhere.

Use the URL form so the connector can authenticate:
`https://<your-host>/mcp/<your-token>`

Claude cannot *add* a connector from a phone — add it once on the web and it
syncs to iOS and Android.

## Data sources

Realtime (GTFS-Realtime protobuf, refreshed every 20s):

- `https://gtfsrealtime.ridetransit.org/GTFSRealTime/Vehicle/VehiclePositions.pb`
- `https://gtfsrealtime.ridetransit.org/GTFSRealTime/TripUpdate/TripUpdates.pb`
- `https://gtfsrealtime.ridetransit.org/GTFSRealTime/Alert/Alerts.pb`

Static schedule (cached 6h), used to turn feed identifiers into route names, stop
names, and coordinates:

- `https://gtfsrealtime.ridetransit.org/GTFSStatic/api/GTFSDownload/GTFS.zip`

Only `routes.txt`, `stops.txt`, and `trips.txt` are extracted; `stop_times.txt` and
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
- Concurrent calls share a single in-flight fetch per feed.
- If a refresh fails but cached data exists, the last good data is served rather than
  an error. `feedAgeSeconds` on every response shows how stale it is.
- The alerts feed is supplementary: if it fails, `get_arrivals` still returns arrivals.
- Times are ISO 8601 UTC; coordinates are WGS84 decimal degrees.

## Configuration

All optional; defaults target the CATS feeds above.

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
| `CATS_AUTH_TOKEN` | *(none — required for HTTP)* |
| `CATS_ALLOW_ANONYMOUS` | `false` |
| `CATS_HTTP_PORT` / `PORT` | `8080` |
| `CATS_HTTP_HOST` | `0.0.0.0` |
| `CATS_MAX_BODY_BYTES` | `4194304` |

## Auth and hosting caveats

- **Claude's connector dialog takes a URL plus optional OAuth client
  credentials — there is no static-header field.** That is why the token can be
  carried in the path. A secret in a URL is weaker than a header: it is logged
  by proxies and platforms and is easy to leak by pasting the link. It is the
  right trade for this server (read-only public data) but would not be for one
  holding private data — that case wants a real OAuth 2.1 layer.
- **Exposure.** All three tools are read-only over public transit data, so the
  risk of an open endpoint is abuse of your hosting and of the CATS feeds
  rather than disclosure of anything private. Rate limiting is not built in;
  add it at the proxy or platform layer if the endpoint is open.

## Development

```bash
npm test        # 44 tests, offline against recorded feed fixtures
npm run typecheck
npm run build
```

Tests run against protobuf and GTFS fixtures captured from the live feeds, so they are
deterministic and make no network calls.
