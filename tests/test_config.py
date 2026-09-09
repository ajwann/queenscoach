from __future__ import annotations

import pytest

from queenscoach.config import ConfigError, load_config


def test_defaults_point_at_the_published_cats_feeds() -> None:
    config = load_config({})
    assert config.vehicle_positions_url.endswith("VehiclePositions.pb")
    assert config.trip_updates_url.endswith("TripUpdates.pb")
    assert config.alerts_url.endswith("Alerts.pb")
    assert config.realtime_ttl_seconds == 20.0
    assert config.static_ttl_seconds == 6 * 60 * 60


def test_accepts_an_http_override() -> None:
    config = load_config({"CATS_ALERTS_URL": "https://example.test/a.pb"})
    assert config.alerts_url == "https://example.test/a.pb"


def test_rejects_a_non_http_scheme() -> None:
    with pytest.raises(ConfigError, match="http or https"):
        load_config({"CATS_ALERTS_URL": "file:///etc/passwd"})


@pytest.mark.parametrize(
    "env",
    [
        {"CATS_ALERTS_URL": "not a url"},
        {"CATS_ALERTS_URL": "https:///no-host"},
        {"CATS_REALTIME_TTL_MS": "0"},
        {"CATS_REALTIME_TTL_MS": "-5"},
        {"CATS_REALTIME_TTL_MS": "soon"},
        {"CATS_REALTIME_TTL_MS": "1.5"},
    ],
)
def test_rejects_malformed_overrides(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_config(env)


def test_an_empty_environment_variable_falls_back_to_the_default() -> None:
    config = load_config({"CATS_ALERTS_URL": "", "CATS_REALTIME_TTL_MS": ""})
    assert config.alerts_url.endswith("Alerts.pb")
    assert config.realtime_ttl_seconds > 0
