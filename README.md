<!-- mcp-name: io.github.ajwann/queenscoach -->

# QueensCoach ♔

[![CI](https://github.com/ajwann/queenscoach/actions/workflows/ci.yml/badge.svg)](https://github.com/ajwann/queenscoach/actions/workflows/ci.yml)

**QueensCoach** is an MCP server for live **Charlotte Area Transit System (CATS)** bus
and light rail data, built on the agency's public GTFS-Realtime feeds. It runs over
**stdio**, launched by the MCP client that uses it, or over **HTTP** with Google OAuth
in front of it, for a hosted server. Both transports serve the same tools.

## Hosted server

A public instance runs on Google Cloud Run. Sign in with any Google account:

```
https://queenscoach.adamwanninger.com/mcp
```

**There is zero guarantee of uptime.** The hosted server is provided as-is. It may be
slow, down, switched off by its spending cap, or retired without notice. For anything
you rely on, run your own: over [stdio](#stdio), or on your own Google Cloud project
with [`deploy/GCP.md`](deploy/GCP.md).

Signing in tells the server your Google account's email address, which is used only
to decide whether to admit you, and is never stored. The tokens it issues record
your account's opaque Google ID and nothing else about you. The
[privacy policy](https://adamwanninger.com/privacy/) and
[terms of service](https://adamwanninger.com/terms/) cover the hosted server.

### Adding it to Claude

Claude calls a remote MCP server a **connector**. Custom connectors are available on
Claude's paid plans.

1. Open **Settings → Connectors**. On the web that's
   [claude.ai/settings/connectors](https://claude.ai/settings/connectors); in the
   desktop app, Settings then Connectors.
2. Click **Add custom connector** at the bottom of the list.
3. Give it a name, `QueensCoach`, and paste the URL above as the remote MCP server
   URL. Leave the advanced OAuth fields empty: this server registers your client
   automatically.
4. Click **Add**, then **Connect** on the connector that appears. A browser window
   opens for the Google sign-in; approve it and it closes itself.
5. In a chat, open the tools menu and check that QueensCoach is enabled. Its three
   tools then appear.

The connector belongs to your Claude account, so it follows you across web, desktop,
and mobile. To disconnect, remove it from that same Connectors page; that revokes the
tokens this server issued.

### Adding it to Claude Code

```bash
claude mcp add --transport http queenscoach https://queenscoach.adamwanninger.com/mcp
```

Then run `/mcp`, pick `queenscoach`, and choose **Authenticate**, which opens the same
Google sign-in. `/mcp` shows the connection's state afterwards. A server added this way
loads when Claude Code next starts.

### Any other client

Any MCP client that supports remote servers over streamable HTTP with OAuth works:
give it the same URL and it discovers the rest.

## Tools

| Tool | Purpose |
| --- | --- |
| `plan_trip` | Trips between two places by bus and train, with walking and transfers, adjusted by live delays. |
| `get_arrivals` | Live predicted arrivals at a named stop, or at the station nearest a location. |
| `get_schedule` | The published timetable at a stop for any date: departures, first and last trips. |
| `get_route` | One route on a date: destinations, stops in order, hours, and how often it runs. |
| `get_service_alerts` | Current and upcoming detours and disruptions, system-wide or for a route, stop, or place. |
| `list_stops` | Stops and stations with the routes serving each, nearest first from a location. |
| `list_vehicles` | GPS positions of buses and trains in service: one by number, a route's, or all of them. |

Ask Claude "what's the closest train station to me?", "when's the next train at my
stop?", or "how do I get to the airport from here?" and it passes your device's
location to the tools. The server never sees a location it isn't handed, so this
needs a client that shares the device's location with the model.

### `plan_trip`

| Argument | Type | Notes |
| --- | --- | --- |
| `origin_stop` | string | Where to start: a stop id, code, or name. |
| `origin_latitude`, `origin_longitude` | number | Or start from a location, walking to nearby stops. |
| `destination_stop` | string | Where to go: a stop id, code, or name. |
| `destination_latitude`, `destination_longitude` | number | Or finish at a location. |
| `date` | `YYYY-MM-DD` | Service date in Charlotte (default today). |
| `depart_at` | `HH:MM` | Leave no earlier than this. Default: now. |
| `arrive_by` | `HH:MM` | Or arrive no later than this. |
| `max_transfers` | integer | 0 to 3 (default 2). |
| `max_walk_meters` | integer | Longest walk to the first stop or from the last, 100 to 2000 (default 800). |

Returns up to three itineraries, each with walking and riding legs: the route, the
vehicle's real destination, where to board and get off, times, the wait at each
transfer, and stops travelled. Journeys with more transfers are offered only when they
arrive sooner (or, for `arrive_by`, leave later). For trips starting within three hours
of now, rides carry live delays and are marked `live`. `walkingIsAnOption` appears when
the two places are within the walking limit of each other.

Walking is an estimate: the straight-line distance stretched by 30%, at 4.5 km/h. The
feed has no street map, so real walks can be longer. Transfers allow up to 400 m on foot
and two minutes of slack.

### `get_schedule`

| Argument | Type | Notes |
| --- | --- | --- |
| `stop` | string | Stop id, code, or name. |
| `latitude`, `longitude` | number | Or the nearest stop serving the route or mode. |
| `route` | string | Optional route filter. |
| `mode` | `bus` \| `train` | Optional filter. |
| `date` | `YYYY-MM-DD` | Service date (default today). |
| `after`, `before` | `HH:MM` | Time window. `after` defaults to now today, or the start of service. |
| `limit` | integer | Max departures (default 20, cap 100). |

Scheduled departures with each trip's route and destination, plus `firstDeparture`
and `lastDeparture` for the day. A service day runs past midnight, as GTFS does: the
last trains of Tuesday leave at 1:31 am on Wednesday, and `25:30` in `after` or `before`
means 1:30 am that night. Use `get_arrivals` for live predictions.

### `get_route`

| Argument | Type | Notes |
| --- | --- | --- |
| `route` | string | **Required.** `9`, `501`, `Blue Line`, `Airport`. |
| `date` | `YYYY-MM-DD` | Service date (default today). |

For each direction: the destination, the stops in order (following the pattern most
trips use, with any others summarized), first and last departure, and the typical
minutes between trips in the early morning, morning rush, midday, afternoon rush,
evening, and late night. Also trips on each of the next seven days and the number of
active alerts. A query matching several routes lists them instead.

### `get_service_alerts`

| Argument | Type | Notes |
| --- | --- | --- |
| `route` | string | Alerts naming the route or any stop it serves. |
| `stop` | string | Alerts naming the station or any route serving it. |
| `latitude`, `longitude` | number | Alerts affecting stops within 800 m. |

Give at most one; with none, every alert. Ended alerts are left out. Each alert has its
headline, description, effect and cause with CATS's detail text, whether it is
`active` or `upcoming`, its periods (`untilFurtherNotice` for open-ended ones), and the
routes and stops it names.

### `list_vehicles`

| Argument | Type | Notes |
| --- | --- | --- |
| `vehicle` | string | Vehicle number as shown on the bus/train, e.g. `2301`, `LRV307`. |
| `route` | string | Route: `9`, `501`, `Blue Line`, `Mt. Holly Road`. |
| `mode` | `bus` \| `train` | Optional filter. |
| `limit` | integer | Max vehicles to return (default and cap: 250). |

All arguments are optional; with none, every vehicle in service is returned. Each
vehicle has position, heading, speed, occupancy, headsign, and the next scheduled
stop. `matches` and `countsByMode` report the total even when the list is truncated.

### `list_stops`

| Argument | Type | Notes |
| --- | --- | --- |
| `latitude`, `longitude` | number | A location; results are then nearest first, with `metersAway`. |
| `query` | string | Stop id, stop code, or part of a stop name. |
| `route` | string | Only stops this route serves. |
| `mode` | `bus` \| `train` | Only stops with that service, e.g. `train` for the nearest rail station. |
| `limit` | integer | Max stations (default 10 with a location, 250 without; cap 250). |

Each station lists its `stopIds`, coordinates, `modes`, and `routes` (id, number, long
name, mode): for example `501 · Light Rail - Lynx Blue Line`, `510 · CityLYNX Gold
Line`, or bus routes like `29`. Stops no scheduled trip calls at are left out, and
nothing farther than 50 km from the location is returned.

### `get_arrivals`

| Argument | Type | Notes |
| --- | --- | --- |
| `stop` | string | Stop id (`02400`), stop code, or part of a stop name (`CTC Station`). |
| `latitude`, `longitude` | number | Instead of `stop`: use the nearest station to this location. |
| `route` | string | Optional route filter. |
| `mode` | `bus` \| `train` | Optional filter. |
| `limit` | integer | Max arrivals (default 10, cap 50). |

Give either `stop` or a location. With a location, the filters choose the station too:
`mode: train` finds the nearest stop a train calls at, not a closer bus stop, and the
response carries its `metersAway`.

Returns minutes away, predicted and scheduled times, schedule deviation, the vehicle
number, the platform (`stopId`), and that vehicle's live position. When a name query
is ambiguous, the best match is used and the runners-up are listed under
`otherStopsMatchingQuery`. Service alerts affecting the stop or its routes are
attached when present.

## Resources

The feeds behind the tools, for clients and models that want the data itself.

| URI | Type | Contents |
| --- | --- | --- |
| `gtfs://static` | JSON | The files in the static GTFS archive, their sizes and URIs, and when it was fetched. |
| `gtfs://static/{file}` | CSV | Any table in the archive as CATS publishes it. `routes.txt`, `stops.txt`, `trips.txt`, and `stop_times.txt` are also listed individually. |
| `gtfs://realtime/vehicle-positions` | JSON | The decoded VehiclePositions feed. |
| `gtfs://realtime/trip-updates` | JSON | The decoded TripUpdates feed. |
| `gtfs://realtime/alerts` | JSON | The decoded Alerts feed. |

Static tables come from the same cached download the tools use. Realtime resources
are the decoded feed entities with their GTFS-Realtime field names, raw ids, and Unix
timestamps, without the schedule joins the tools add. `stop_times.txt` is large
(about 12 MB in the live feed).

## Install

Requires Python 3.11+.

```bash
pip install queenscoach
```

Or run it without installing anything, which is how most MCP clients launch it:

```bash
uvx queenscoach
```

From a clone instead, to hack on it or to run the HTTP transport from source:

```bash
python3 -m venv .venv
.venv/bin/pip install .          # '.[gcp]' adds the Firestore token store
```

## Transports

Pick one with `--transport` or `QUEENSCOACH_TRANSPORT`; the default is `stdio`.

```bash
queenscoach                                  # stdio (default)
queenscoach --transport http --port 8000     # streamable HTTP + Google OAuth
```

### stdio

For a server the client launches itself. No authentication: the client already owns
the process.

Register it with Claude Code:

```bash
claude mcp add queenscoach -- uvx queenscoach
```

Or in an MCP client config file:

```json
{
  "mcpServers": {
    "queenscoach": {
      "command": "uvx",
      "args": ["queenscoach"]
    }
  }
}
```

An installed copy works just as well, given an absolute path
(`/absolute/path/to/.venv/bin/queenscoach`): MCP clients rarely share your shell's
`PATH`. `python -m queenscoach` runs the same server, so any interpreter with the
package installed works as the command.

stdout carries MCP protocol traffic only; all diagnostics go to stderr.

### HTTP with Google OAuth

For a hosted server anyone with the URL can reach. Every request to `/mcp` needs a
bearer token, and the only way to get one is to sign in with a Google account that is
on the allow list.

**How the sign-in works.** MCP clients register themselves dynamically and expect an
authorization server at the MCP server's own origin. Google offers neither dynamic
registration nor tokens audience-restricted to a third-party resource, so this server
is its own OAuth 2.1 authorization server and delegates only the login to Google:

```
MCP client  <--OAuth-->  queenscoach  <--OAuth-->  Google
```

Google's answer is used exactly once, to learn which account signed in. That email is
checked against the allow list, and only then does this server mint its own tokens.
Google's tokens are never handed to the client.

**One-time setup in Google Cloud.** At
[console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients),
create an **OAuth client** of type **Web application** and add one authorized
redirect URI:

```
https://your-public-url/auth/google/callback
```

It must match `QUEENSCOACH_PUBLIC_URL` exactly. The server logs the URI it expects at startup.
Copy the client ID and secret into the environment below.

**Run it.** `.env.example` lists every setting; the shell form is:

```bash
export QUEENSCOACH_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
export QUEENSCOACH_GOOGLE_CLIENT_SECRET=...
export QUEENSCOACH_ALLOWED_EMAILS=you@example.com
export QUEENSCOACH_PUBLIC_URL=https://queenscoach.example.com

queenscoach --transport http --port 8000
```

Then point a client at `https://queenscoach.example.com/mcp`; it discovers the rest and opens
a browser for the Google sign-in. In Claude Code:

```bash
claude mcp add --transport http queenscoach https://queenscoach.example.com/mcp
```

**Access is denied by default.** Startup fails unless `QUEENSCOACH_ALLOWED_EMAILS`,
`QUEENSCOACH_ALLOWED_DOMAINS`, or an explicit `QUEENSCOACH_ALLOW_ANY_GOOGLE_ACCOUNT=true` says who
may get in, so a misconfigured deployment is unreachable rather than open to every
Google account on the internet. Unverified Google addresses are always refused.

**Endpoints.**

| Path | Purpose |
| --- | --- |
| `/mcp` | The MCP endpoint. Requires `Authorization: Bearer <token>`. |
| `/.well-known/oauth-protected-resource/mcp` | Points clients at the authorization server. |
| `/.well-known/oauth-authorization-server` | This server's OAuth metadata. |
| `/register` | Dynamic client registration (RFC 7591). |
| `/authorize`, `/token`, `/revoke` | The OAuth endpoints. |
| `/auth/google/callback` | Where Google returns the user. |

[`scripts/install.sh`](scripts/install.sh) does a whole deployment: a system
user under `/opt`, a Cloudflare tunnel and its DNS record created over the API,
both systemd units, and a verification pass. No port forwarding, so it works
behind CGNAT or a locked router. See [`deploy/`](deploy/README.md).

[`scripts/deploy-gcp.sh`](scripts/deploy-gcp.sh) does the same on **Google Cloud
Run**, in your own GCP project: the project itself, Firestore for sign-ins, the
client secret in Secret Manager, a container built by Cloud Build, a monthly
budget with an optional hard spend cap, and the same verification pass. It scales
to zero, so a personal server costs next to nothing. See
[`deploy/GCP.md`](deploy/GCP.md).

**Deployment notes.**

- By default the server speaks plain HTTP and expects a tunnel or proxy to
  terminate TLS, which is what the install script sets up. Setting
  `QUEENSCOACH_TLS_CERT` and `QUEENSCOACH_TLS_KEY` instead makes it serve HTTPS itself, for a
  deployment with nothing in front of it.
- `QUEENSCOACH_PUBLIC_URL` is what clients dial and is this server's OAuth issuer
  identifier, so it must be the external URL, not the bind address.
- Token state is in memory by default and therefore per-process: restarting
  invalidates outstanding tokens. `QUEENSCOACH_TOKEN_STORE=firestore` keeps it in
  Firestore instead (install the `gcp` extra: `pip install 'queenscoach[gcp]'`), so
  sign-ins survive restarts and every instance shares them. Pair it with
  `QUEENSCOACH_STATELESS_HTTP=true` so that any instance can answer any request.
- Access tokens last an hour and refresh tokens 30 days, both rotated on refresh.

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
- Same-named stops within 200 m are one station: the two platforms of a Gold Line
  stop, or bus stops facing each other across a street. `get_arrivals` reports
  arrivals at all of them.
- Feed responses are capped in size and time-bounded; one slow feed cannot hang a call.
- Concurrent calls share a single in-flight fetch per feed, and one call giving up does
  not abort a fetch the others are awaiting.
- If a refresh fails but cached data exists, the last good data is served rather than
  an error. `feedAgeSeconds` on every response shows how stale it is.
- The alerts feed is supplementary: if it fails, `get_arrivals` still returns arrivals.
- The timetable answers only the dates CATS has published, typically a few weeks ahead.
  Timetable tools report the range as `scheduleCovers` and refuse a date outside it
  rather than guessing.
- Headsigns that name only a direction ("Inbound", used by 61 of 64 CATS routes) are
  replaced by the trip's real destination, its last stop.
- Timetable clock times follow GTFS: counted from noon minus 12 hours on the service
  date in the agency's time zone, so they stay right across daylight-saving changes.
- Trip planning and timetable scans run in a worker thread, so a long search does not
  stall other requests.
- Times are ISO 8601 with a UTC offset: the realtime tools use UTC and the timetable
  tools Charlotte local time. Coordinates are WGS84 decimal degrees.

## Configuration

### Feeds (both transports)

All optional; defaults target the CATS feeds above. Durations are in milliseconds.

| Variable | Default |
| --- | --- |
| `QUEENSCOACH_VEHICLE_POSITIONS_URL` | CATS vehicle positions feed |
| `QUEENSCOACH_TRIP_UPDATES_URL` | CATS trip updates feed |
| `QUEENSCOACH_ALERTS_URL` | CATS alerts feed |
| `QUEENSCOACH_STATIC_GTFS_URL` | CATS static GTFS zip |
| `QUEENSCOACH_REALTIME_TTL_MS` | `20000` |
| `QUEENSCOACH_STATIC_TTL_MS` | `21600000` |
| `QUEENSCOACH_REQUEST_TIMEOUT_MS` | `30000` |
| `QUEENSCOACH_MAX_FEED_BYTES` | `33554432` |
| `QUEENSCOACH_MAX_STATIC_BYTES` | `268435456` |

Feed URLs must be `http` or `https`; anything else is rejected at startup.

### Transport

| Variable | CLI | Default |
| --- | --- | --- |
| `QUEENSCOACH_TRANSPORT` | `--transport` | `stdio` |

### HTTP transport

Read only when `--transport http` is selected.

| Variable | CLI | Default | Notes |
| --- | --- | --- | --- |
| `QUEENSCOACH_HTTP_HOST` | `--host` | `127.0.0.1` | Bind address. |
| `QUEENSCOACH_HTTP_PORT` | `--port` | `8000` | Bind port. |
| `QUEENSCOACH_PUBLIC_URL` | `--public-url` | `http://localhost:<port>` | External origin; the OAuth issuer. |
| `QUEENSCOACH_GOOGLE_CLIENT_ID` | | **required** | From Google Cloud credentials. |
| `QUEENSCOACH_GOOGLE_CLIENT_SECRET` | | **required** | From Google Cloud credentials. |
| `QUEENSCOACH_ALLOWED_EMAILS` | | — | Allowed addresses, comma- or space-separated. |
| `QUEENSCOACH_ALLOWED_DOMAINS` | | — | Allowed bare domains, e.g. `example.com`. |
| `QUEENSCOACH_ALLOW_ANY_GOOGLE_ACCOUNT` | | `false` | Opt in to admitting every Google account. |
| `QUEENSCOACH_TLS_CERT` | `--tls-cert` | — | PEM chain, to serve HTTPS directly. |
| `QUEENSCOACH_TLS_KEY` | `--tls-key` | — | PEM private key. Required with the above. |
| `QUEENSCOACH_ACCESS_TOKEN_TTL_MS` | | `3600000` | Access token lifetime. |
| `QUEENSCOACH_REFRESH_TOKEN_TTL_MS` | | `2592000000` | Refresh token lifetime. |
| `QUEENSCOACH_TOKEN_STORE` | | `memory` | `memory`, or `firestore` (needs the `gcp` extra). |
| `QUEENSCOACH_FIRESTORE_DATABASE` | | `(default)` | Firestore database for the token store. |
| `QUEENSCOACH_STATELESS_HTTP` | | `false` | Serve without MCP sessions, for restarts and multiple instances. |

One of the three allow-list settings is required; see above.

## Layout

| Module | Role |
| --- | --- |
| `config.py` | Environment parsing and validation |
| `feed_http.py` | Bounded, time-limited HTTP fetch |
| `cache.py` | TTL cache with single-flight refresh |
| `gtfs_csv.py` | GTFS-flavored CSV reading |
| `static_gtfs.py` | Static schedule: routes, stops, trips, their timetables, and the service calendar |
| `realtime.py` | GTFS-Realtime protobuf decoding |
| `transit.py` | Domain layer: joins realtime to schedule, resolves queries |
| `timetable.py` | Domain layer for the published timetable: service days, departures, route patterns |
| `planner.py` | Trip planning: a Connection Scan over the timetable, with live delays |
| `tools.py` | The tools' behavior and JSON payloads |
| `resources.py` | The GTFS feeds as MCP resources |
| `server.py` | MCP tool and resource registration, and schemas |
| `oauth.py` | OAuth authorization server, with Google as the login |
| `token_store.py` | Where OAuth state is kept, and the in-memory default |
| `token_store_firestore.py` | The Firestore token store (the `gcp` extra only) |
| `http.py` | Streamable HTTP transport and the Google callback route |
| `main.py` | CLI entry point and transport selection |

Plus [`scripts/install.sh`](scripts/install.sh), which deploys the HTTP
transport onto a Debian host, and [`scripts/deploy-gcp.sh`](scripts/deploy-gcp.sh)
with the [`Dockerfile`](Dockerfile), which deploy it to Google Cloud Run.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest        # offline, against recorded feed fixtures
.venv/bin/mypy          # strict
.venv/bin/ruff check .
.venv/bin/ruff format .
```

Tests run against protobuf and GTFS fixtures captured from the live feeds, so they are
deterministic and make no network calls. `tests/test_feed_http.py` is the exception: it
serves canned responses from a loopback socket so the byte cap and timeout are exercised
for real.

`tests/test_http.py` drives the whole OAuth handshake against the real ASGI app -
registration, `/authorize`, the Google callback, `/token`, then an authenticated
`tools/list` - with Google's token endpoint replaced by a stub, so no account or network
is needed.

`tests/test_token_store.py` runs every token-store test against both stores. The
Firestore half needs the emulator (`gcloud emulators firestore start`, then set
`FIRESTORE_EMULATOR_HOST`) and is skipped without it. `tests/test_stdio.py` starts the
real stdio server in a child process; CI also runs it against a plain `pip install .`,
to prove stdio needs none of the optional extras.

## License

MIT - see [LICENSE](LICENSE).
