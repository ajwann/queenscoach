"""Transport selection at the CLI boundary."""

from __future__ import annotations

import pytest

from queenscoach.main import build_parser, main

#: Named so the linter does not read a test string as a real bind address.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104


def test_the_parser_defaults_to_letting_the_environment_decide() -> None:
    args = build_parser().parse_args([])
    assert args.transport is None
    assert args.host is None
    assert args.port is None
    assert args.public_url is None


def test_the_parser_accepts_the_http_options() -> None:
    args = build_parser().parse_args(
        [
            "--transport",
            "http",
            "--host",
            ALL_INTERFACES,
            "--port",
            "9000",
            "--public-url",
            "https://x",
        ]
    )
    assert args.transport == "http"
    assert args.host == ALL_INTERFACES
    assert args.port == 9000
    assert args.public_url == "https://x"


def test_an_unknown_transport_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--transport", "carrier-pigeon"])


def test_a_misconfigured_http_run_exits_with_an_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No Google client credentials in the environment: the server must refuse
    # to start rather than come up unauthenticated.
    for name in (
        "QUEENSCOACH_GOOGLE_CLIENT_ID",
        "QUEENSCOACH_GOOGLE_CLIENT_SECRET",
        "QUEENSCOACH_ALLOWED_EMAILS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert main(["--transport", "http"]) == 1
