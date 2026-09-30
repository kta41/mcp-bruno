# Bruno MCP Python

This workspace contains a Python MCP server for running Bruno collections.

Use the official Model Context Protocol Python SDK (`mcp`) for server behavior. The server runs over stdio and exposes the `run-collection` tool, which delegates execution to the external Bruno CLI command `bru`.

Keep the normalized output contract stable: `success`, `summary`, `failures`, and `timings`.
