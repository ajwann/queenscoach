"""The GTFS feeds as MCP resources: what is listed, what reading returns, and errors."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import replace

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError
from mcp.types import InputRequiredResult

from queenscoach.cache import Cached
from queenscoach.realtime import TripUpdate
from queenscoach.server import create_server
from queenscoach.tools import Dependencies

from .conftest import ARCHIVE, VEHICLES, FixtureFeeds, fixture_deps

REALTIME_URIS = {
    "gtfs://realtime/vehicle-positions",
    "gtfs://realtime/trip-updates",
    "gtfs://realtime/alerts",
}


async def _read(deps: Dependencies, uri: str) -> tuple[str, str | None]:
    result = await create_server(deps).read_resource(uri)
    assert not isinstance(result, InputRequiredResult)
    contents = list(result)
    assert len(contents) == 1
    content = contents[0].content
    assert isinstance(content, str)
    return content, contents[0].mime_type


async def test_the_static_index_core_tables_and_realtime_feeds_are_listed(
    deps: Dependencies,
) -> None:
    server = create_server(deps)
    listed = {str(resource.uri) for resource in await server.list_resources()}
    assert listed == {
        "gtfs://static",
        "gtfs://static/routes.txt",
        "gtfs://static/stops.txt",
        "gtfs://static/trips.txt",
        "gtfs://static/stop_times.txt",
        *REALTIME_URIS,
    }
    templates = {template.uri_template for template in await server.list_resource_templates()}
    assert templates == {"gtfs://static/{file}"}


async def test_the_static_index_describes_every_archive_file(deps: Dependencies) -> None:
    text, mime_type = await _read(deps, "gtfs://static")
    assert mime_type == "application/json"
    index = json.loads(text)
    assert index["source"] == "https://feed.test/GTFS.zip"
    assert index["fetchedAt"] == "2026-09-08T21:59:48.000Z"
    names = {entry["name"] for entry in index["files"]}
    assert names == {"routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}
    for entry in index["files"]:
        assert entry["uri"] == f"gtfs://static/{entry['name']}"
        assert entry["bytes"] > 0


async def test_a_static_table_is_served_as_the_published_csv(deps: Dependencies) -> None:
    text, mime_type = await _read(deps, "gtfs://static/routes.txt")
    assert mime_type == "text/csv"
    rows = list(csv.DictReader(io.StringIO(text)))
    assert {row["route_id"] for row in rows} == {"1", "5", "29", "501", "510"}


async def test_the_table_template_serves_files_beyond_the_core_tables() -> None:
    # Rebuild the archive with an extra table only the template can reach.
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(ARCHIVE)) as source, zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info))
        target.writestr("agency.txt", "agency_id,agency_name\nCATS,Charlotte Area Transit\n")
    archive = buffer.getvalue()

    async def load_archive() -> Cached[bytes]:
        return Cached(value=archive, fetched_at=0)

    deps = replace(fixture_deps(), load_archive=load_archive)
    text, mime_type = await _read(deps, "gtfs://static/agency.txt")
    assert mime_type == "text/csv"
    assert "Charlotte Area Transit" in text


@pytest.mark.parametrize(
    "uri", ["gtfs://static/agency.txt", "gtfs://static/..%2Fstops.txt", "gtfs://static/%2Fetc"]
)
async def test_an_unknown_or_unsafe_table_name_is_not_found(deps: Dependencies, uri: str) -> None:
    with pytest.raises(ResourceNotFoundError):
        await create_server(deps).read_resource(uri)


async def test_a_realtime_feed_is_served_as_decoded_json(deps: Dependencies) -> None:
    text, mime_type = await _read(deps, "gtfs://realtime/vehicle-positions")
    assert mime_type == "application/json"
    payload = json.loads(text)
    assert payload["feed"] == "vehicle-positions"
    assert payload["count"] == len(VEHICLES)
    assert payload["entities"][0]["entity_id"] == VEHICLES[0].entity_id

    for uri in REALTIME_URIS:
        text, _ = await _read(deps, uri)
        assert json.loads(text)["count"] >= 0


async def test_a_feed_failure_becomes_a_readable_resource_error() -> None:
    class DownFeeds(FixtureFeeds):
        async def trip_updates(self) -> Cached[list[TripUpdate]]:
            raise RuntimeError("Request to https://feed.test/TripUpdates.pb failed: timed out")

    deps = fixture_deps(DownFeeds())
    with pytest.raises(ResourceError, match=r"CATS feed request failed: .*timed out"):
        await create_server(deps).read_resource("gtfs://realtime/trip-updates")
