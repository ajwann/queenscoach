"""Entry point: selects the transport and runs the server.

``stdio`` (the default) is for a server the client launches itself (Claude
Code, Claude Desktop); ``http`` is for a hosted server, and authenticates its
callers with Google OAuth.

On stdio, stdout carries protocol traffic only; all diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import SERVER_NAME, SERVER_VERSION
from .config import (
    TRANSPORTS,
    ConfigError,
    Transport,
    load_config,
    load_http_config,
    load_transport,
)
from .realtime import create_realtime_feeds
from .server import create_server
from .static_gtfs import create_schedule_loader
from .tools import Dependencies

_logger = logging.getLogger("queenscoach")


def configure_logging() -> None:
    """Send this server's diagnostics to stderr, keeping stdout free for MCP traffic.

    The root logger stays at WARNING so third-party per-request chatter (httpx
    logs every feed fetch at INFO) does not reach the launching client's log.
    """
    logging.basicConfig(
        level=logging.WARNING, format="[queenscoach] %(message)s", stream=sys.stderr, force=True
    )
    _logger.setLevel(logging.INFO)
    logging.getLogger("queenscoach.oauth").setLevel(logging.INFO)
    logging.getLogger("queenscoach.http").setLevel(logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Every option also has an environment variable, for hosted runs."""
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME, description="MCP server for live CATS transit data."
    )
    parser.add_argument("--version", action="version", version=f"{SERVER_NAME} {SERVER_VERSION}")
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=None,
        help="Transport to serve on (env QUEENSCOACH_TRANSPORT; default stdio).",
    )
    http_options = parser.add_argument_group("http transport")
    http_options.add_argument(
        "--host",
        default=None,
        help="Interface to bind (env QUEENSCOACH_HTTP_HOST; default 127.0.0.1).",
    )
    http_options.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind (env QUEENSCOACH_HTTP_PORT; default 8000).",
    )
    http_options.add_argument(
        "--tls-cert",
        default=None,
        help=(
            "PEM certificate chain, to serve HTTPS directly with no proxy in "
            "front (env QUEENSCOACH_TLS_CERT). Requires --tls-key."
        ),
    )
    http_options.add_argument(
        "--tls-key",
        default=None,
        help="PEM private key (env QUEENSCOACH_TLS_KEY). Requires --tls-cert.",
    )
    http_options.add_argument(
        "--public-url",
        default=None,
        help=(
            "Externally reachable origin, e.g. https://queenscoach.example.com "
            "(env QUEENSCOACH_PUBLIC_URL). It is this server's OAuth issuer, so it must "
            "match the URL clients dial."
        ),
    )
    return parser


def _build_dependencies() -> Dependencies:
    config = load_config()
    return Dependencies(
        load_schedule=create_schedule_loader(config), feeds=create_realtime_feeds(config)
    )


async def serve(args: argparse.Namespace, transport: Transport) -> None:
    """Run the selected transport until the client disconnects or the process stops."""
    deps = _build_dependencies()
    if transport == "http":
        # Imported here so a stdio-only run never pays for starlette/uvicorn.
        from .http import serve_http

        http_config = load_http_config(
            host=args.host,
            port=args.port,
            public_url=args.public_url,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
        await serve_http(deps, http_config)
        return

    _logger.info("starting on stdio")
    await create_server(deps).run_stdio_async()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        transport = load_transport(override=args.transport)
        asyncio.run(serve(args, transport))
    except ConfigError as error:
        _logger.error("configuration error: %s", error)
        return 1
    except KeyboardInterrupt:
        _logger.info("received interrupt, shutting down")
    except Exception:
        _logger.exception("fatal error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
