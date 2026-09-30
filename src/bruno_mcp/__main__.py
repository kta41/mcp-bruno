from __future__ import annotations

import anyio
from mcp.server.stdio import stdio_server

from bruno_mcp.server import create_server


async def async_main() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    anyio.run(async_main)


if __name__ == "__main__":
    main()
