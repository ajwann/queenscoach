"""Entry point: a stdio MCP server, launched by the client that uses it
(Claude Code, Claude Desktop).

stdout carries protocol traffic only; all diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import ConfigError, load_config
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


async def serve() -> None:
    """Run the stdio server until the client disconnects."""
    config = load_config()
    deps = Dependencies(
        load_schedule=create_schedule_loader(config), feeds=create_realtime_feeds(config)
    )
    _logger.info("starting on stdio")
    await create_server(deps).run_stdio_async()


def main() -> int:
    """CLI entry point. Returns the process exit code."""
    configure_logging()
    try:
        asyncio.run(serve())
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
