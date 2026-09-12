<!-- mcp-name: io.github.ajwann/queenscoach -->

# QueensCoach ♔

[![CI](https://github.com/ajwann/queenscoach/actions/workflows/ci.yml/badge.svg)](https://github.com/ajwann/queenscoach/actions/workflows/ci.yml)

**QueensCoach** is an MCP server for live **Charlotte Area Transit System (CATS)** bus
and light rail data, built on the agency's public GTFS-Realtime feeds. It runs over
**stdio**, launched by the MCP client that uses it, or over **HTTP** with Google OAuth
in front of it, for a hosted server. Both transports serve the same three tools.

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
claude mcp add queenscoach -- /absolute/path/to/queenscoach/.venv/bin/queenscoach
```

Or in an MCP client config file:

```json
{
  "mcpServers": {
    "queenscoach": {
      "command": "/absolute/path/to/queenscoach/.venv/bin/queenscoach"
    }
  }
}
```

`python -m queenscoach` runs the same server, so any interpreter with the package
installed works as the command.

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
- Feed responses are capped in size and time-bounded; one slow feed cannot hang a call.
- Concurrent calls share a single in-flight fetch per feed, and one call giving up does
  not abort a fetch the others are awaiting.
- If a refresh fails but cached data exists, the last good data is served rather than
  an error. `feedAgeSeconds` on every response shows how stale it is.
- The alerts feed is supplementary: if it fails, `get_arrivals` still returns arrivals.
- Times are ISO 8601 UTC; coordinates are WGS84 decimal degrees.

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
| `static_gtfs.py` | Static schedule: routes, stops, trips |
| `realtime.py` | GTFS-Realtime protobuf decoding |
| `transit.py` | Domain layer: joins realtime to schedule, resolves queries |
| `tools.py` | The three tools' behavior and JSON payloads |
| `server.py` | MCP tool registration and schemas |
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
