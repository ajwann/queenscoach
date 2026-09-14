"""The stdio transport, run the way a PyPI or MCP-registry user runs it.

These start the real ``python -m queenscoach`` in a child process, with every HTTP,
Google, and Firestore setting removed from its environment. Listing tools reads
no feed, so nothing touches the network. CI also runs this file against a plain
``pip install .`` with no optional extras, to prove stdio needs none of them.
"""

from __future__ import annotations

import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

EXPECTED_TOOLS = {"list_vehicles", "list_stops", "get_arrivals"}

#: Modules a stdio server must never load: the HTTP transport and everything
#: behind it, including the optional Firestore dependency.
HTTP_ONLY_MODULES = (
    "queenscoach.http",
    "queenscoach.oauth",
    "queenscoach.token_store",
    "queenscoach.token_store_firestore",
    "google.cloud.firestore",
)


def _unconfigured_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("QUEENSCOACH_", "GOOGLE_", "FIRESTORE_"))
    }


async def test_stdio_serves_the_tools_with_no_configuration() -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "queenscoach"], env=_unconfigured_environment()
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS


def test_the_stdio_path_never_imports_the_http_transport_or_firestore() -> None:
    # A fresh interpreter, because this test process has already imported the
    # HTTP modules for other tests. The probe builds what main.serve builds for
    # stdio, then reports which HTTP-only modules came along.
    probe = "\n".join(
        [
            "import sys",
            "from queenscoach.main import _build_dependencies",
            "from queenscoach.server import create_server",
            "create_server(_build_dependencies())",
            f"print(','.join(m for m in {HTTP_ONLY_MODULES!r} if m in sys.modules))",
        ]
    )
    result = subprocess.run(  # noqa: S603 (a fixed argument vector, no shell)
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=_unconfigured_environment(),
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == ""
