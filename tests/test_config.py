from __future__ import annotations

import os
from pathlib import Path

import pytest

from queenscoach.config import ConfigError, load_config, load_http_config, load_transport


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


# -- Transport selection ---------------------------------------------------


def test_the_transport_defaults_to_stdio() -> None:
    assert load_transport({}) == "stdio"


def test_the_transport_can_be_set_by_environment_or_overridden() -> None:
    assert load_transport({"CATS_TRANSPORT": "http"}) == "http"
    assert load_transport({"CATS_TRANSPORT": "http"}, override="stdio") == "stdio"


def test_an_unknown_transport_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown transport"):
        load_transport({"CATS_TRANSPORT": "carrier-pigeon"})


# -- HTTP transport --------------------------------------------------------

#: Named so the linter does not read a test string as a real bind address.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104

GOOGLE_ENV = {
    "CATS_GOOGLE_CLIENT_ID": "id.apps.googleusercontent.com",
    "CATS_GOOGLE_CLIENT_SECRET": "secret",
    "CATS_ALLOWED_EMAILS": "rider@example.com",
}


def test_http_defaults_bind_loopback_and_advertise_localhost() -> None:
    config = load_http_config(GOOGLE_ENV)
    assert config.host == "127.0.0.1"
    assert config.port == 8000
    assert config.public_url == "http://localhost:8000"
    assert config.resource_url == "http://localhost:8000/mcp"
    assert config.callback_url == "http://localhost:8000/auth/google/callback"


def test_command_line_arguments_win_over_the_environment() -> None:
    env = {**GOOGLE_ENV, "CATS_HTTP_HOST": ALL_INTERFACES, "CATS_PUBLIC_URL": "https://env.test"}
    config = load_http_config(env, host="127.0.0.1", port=9000, public_url="https://cli.test")
    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.public_url == "https://cli.test"


def test_the_public_url_is_reduced_to_a_bare_origin() -> None:
    # RFC 8414 compares issuers by exact string, so a path or trailing slash
    # would break client discovery.
    config = load_http_config({**GOOGLE_ENV, "CATS_PUBLIC_URL": "https://cats.test/mcp/"})
    assert config.public_url == "https://cats.test"


def test_google_client_credentials_are_required() -> None:
    with pytest.raises(ConfigError, match="CATS_GOOGLE_CLIENT_ID is required"):
        load_http_config({"CATS_ALLOWED_EMAILS": "rider@example.com"})
    with pytest.raises(ConfigError, match="CATS_GOOGLE_CLIENT_SECRET is required"):
        load_http_config(
            {"CATS_ALLOWED_EMAILS": "rider@example.com", "CATS_GOOGLE_CLIENT_ID": "id"}
        )


def test_an_empty_allow_list_fails_closed() -> None:
    env = {k: v for k, v in GOOGLE_ENV.items() if k != "CATS_ALLOWED_EMAILS"}
    with pytest.raises(ConfigError, match="No Google accounts are allowed"):
        load_http_config(env)


def test_every_google_account_is_admitted_only_by_an_explicit_opt_in() -> None:
    env = {k: v for k, v in GOOGLE_ENV.items() if k != "CATS_ALLOWED_EMAILS"}
    config = load_http_config({**env, "CATS_ALLOW_ANY_GOOGLE_ACCOUNT": "true"})
    assert config.google.allow_any_account is True
    assert config.google.permits("anyone@anywhere.test", email_verified=True)


@pytest.mark.parametrize(
    "env",
    [
        {"CATS_ALLOWED_DOMAINS": "@example.com"},
        {"CATS_ALLOWED_DOMAINS": "localhost"},
        {"CATS_ALLOWED_EMAILS": "rider"},
        {"CATS_HTTP_PORT": "70000"},
        {"CATS_PUBLIC_URL": "ftp://cats.test"},
        {"CATS_ALLOW_ANY_GOOGLE_ACCOUNT": "maybe"},
        {"CATS_TOKEN_STORE": "redis"},
        {"CATS_STATELESS_HTTP": "sometimes"},
    ],
)
def test_rejects_malformed_http_settings(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_http_config({**GOOGLE_ENV, **env})


def test_allow_lists_are_case_insensitive_and_split_on_commas_or_spaces() -> None:
    config = load_http_config(
        {
            **GOOGLE_ENV,
            "CATS_ALLOWED_EMAILS": "Rider@Example.com, other@example.org",
            "CATS_ALLOWED_DOMAINS": "Charlotte.test  ridetransit.test",
        }
    )
    google = config.google
    assert google.permits("rider@example.com", email_verified=True)
    assert google.permits("anyone@charlotte.test", email_verified=True)
    assert google.permits("anyone@RIDETRANSIT.test", email_verified=True)
    assert not google.permits("rider@example.com", email_verified=False)
    assert not google.permits("stranger@elsewhere.test", email_verified=True)
    assert not google.permits("not-an-address", email_verified=True)


def test_a_public_url_without_tls_is_refused_outside_loopback() -> None:
    with pytest.raises(ConfigError, match="must be https"):
        load_http_config({**GOOGLE_ENV, "CATS_PUBLIC_URL": "http://cats.example.com"})
    # Loopback stays usable for trying the transport locally.
    assert (
        load_http_config({**GOOGLE_ENV, "CATS_PUBLIC_URL": "http://127.0.0.1:8000"}).public_url
        == "http://127.0.0.1:8000"
    )


# -- Serving TLS directly --------------------------------------------------


def test_tls_is_off_unless_both_files_are_given() -> None:
    config = load_http_config({**GOOGLE_ENV, "CATS_PUBLIC_URL": "https://cats.test"})
    assert config.serves_tls is False
    assert config.tls_certfile is None


def test_tls_needs_a_certificate_and_a_key(tmp_path: Path) -> None:
    cert = tmp_path / "fullchain.pem"
    cert.write_text("not really a certificate")
    env = {**GOOGLE_ENV, "CATS_PUBLIC_URL": "https://cats.test", "CATS_TLS_CERT": str(cert)}

    with pytest.raises(ConfigError, match="CATS_TLS_KEY must be set too"):
        load_http_config(env)

    key = tmp_path / "privkey.pem"
    key.write_text("not really a key")
    config = load_http_config({**env, "CATS_TLS_KEY": str(key)})
    assert config.serves_tls is True
    assert config.tls_certfile == str(cert)
    assert config.tls_keyfile == str(key)


def test_an_unreadable_certificate_is_reported_at_startup(tmp_path: Path) -> None:
    missing = tmp_path / "nope.pem"
    env = {**GOOGLE_ENV, "CATS_PUBLIC_URL": "https://cats.test", "CATS_TLS_CERT": str(missing)}
    with pytest.raises(ConfigError, match="is not a file"):
        load_http_config(env)

    key = tmp_path / "privkey.pem"
    key.write_text("secret")
    key.chmod(0o000)
    cert = tmp_path / "fullchain.pem"
    cert.write_text("cert")
    if os.geteuid() == 0:
        pytest.skip("root reads any file, so an unreadable key cannot be simulated")
    with pytest.raises(ConfigError, match="cannot be read"):
        load_http_config({**env, "CATS_TLS_CERT": str(cert), "CATS_TLS_KEY": str(key)})


# -- Token store and sessions ------------------------------------------------


def test_tokens_stay_in_memory_with_sessions_by_default() -> None:
    config = load_http_config(GOOGLE_ENV)
    assert config.oauth_store == "memory"
    assert config.firestore_database == "(default)"
    assert config.stateless is False


def test_a_hosted_server_can_keep_tokens_in_firestore_and_serve_without_sessions() -> None:
    config = load_http_config(
        {
            **GOOGLE_ENV,
            "CATS_TOKEN_STORE": "Firestore",
            "CATS_FIRESTORE_DATABASE": "cats",
            "CATS_STATELESS_HTTP": "true",
        }
    )
    assert config.oauth_store == "firestore"
    assert config.firestore_database == "cats"
    assert config.stateless is True
