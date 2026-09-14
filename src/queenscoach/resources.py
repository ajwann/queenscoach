"""The GTFS feeds exposed as MCP resources.

The tools answer questions; these hand over the data itself. The static
archive's tables are served as the CSV the agency publishes, and each realtime
feed as its decoded entities in JSON. Like :mod:`queenscoach.tools`, every
function here reads through :class:`~queenscoach.tools.Dependencies` so tests
can supply fixtures.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal

from .cache import Cached
from .static_gtfs import list_archive, read_archive_text
from .tools import Dependencies
from .transit import iso_time

STATIC_INDEX_URI = "gtfs://static"
STATIC_TABLE_URI_TEMPLATE = "gtfs://static/{file}"
#: Tables this server itself reads, published as concrete resources so clients
#: that browse a resource list (rather than fill in templates) can find them.
CORE_STATIC_TABLES = ("routes.txt", "stops.txt", "trips.txt", "stop_times.txt")

RealtimeFeed = Literal["vehicle-positions", "trip-updates", "alerts"]
REALTIME_FEEDS: tuple[RealtimeFeed, ...] = ("vehicle-positions", "trip-updates", "alerts")


def static_table_uri(name: str) -> str:
    return f"{STATIC_INDEX_URI}/{name}"


def realtime_uri(feed: RealtimeFeed) -> str:
    return f"gtfs://realtime/{feed}"


class UnknownResourceError(LookupError):
    """The requested resource does not exist, as opposed to failing to load."""


async def static_index(deps: Dependencies) -> str:
    """List the static archive's files, with where and when it was fetched."""
    cached = await deps.load_archive()
    return json.dumps(
        {
            "source": deps.static_gtfs_url or None,
            "fetchedAt": iso_time(cached.fetched_at),
            "files": [
                {"name": entry.name, "bytes": entry.size, "uri": static_table_uri(entry.name)}
                for entry in list_archive(cached.value)
            ],
        },
        indent=2,
    )


async def static_table(deps: Dependencies, name: str) -> str:
    """One static GTFS table, as CSV.

    Raises:
        UnknownResourceError: if the archive has no file of exactly that name.
    """
    cached = await deps.load_archive()
    # Only exact archive member names match, so no path in ``name`` reaches the zip.
    text = read_archive_text(cached.value, name)
    if text is None:
        raise UnknownResourceError(
            f"The static GTFS archive has no {name!r}; read {STATIC_INDEX_URI} for its files."
        )
    return text


async def realtime_feed(deps: Dependencies, feed: RealtimeFeed) -> str:
    """One realtime feed's decoded entities, as JSON. Times are Unix seconds, as in GTFS-RT."""
    cached: Cached[Any]
    if feed == "vehicle-positions":
        cached = await deps.feeds.vehicle_positions()
    elif feed == "trip-updates":
        cached = await deps.feeds.trip_updates()
    else:
        cached = await deps.feeds.alerts()
    entities = [asdict(entity) for entity in cached.value]
    return json.dumps(
        {
            "feed": feed,
            "fetchedAt": iso_time(cached.fetched_at),
            "count": len(entities),
            "entities": entities,
        }
    )
