# Bruno MCP Python

This workspace contains a Python MCP server for running Bruno collections.

Use the official Model Context Protocol Python SDK (`mcp`) for server behavior. The server runs over stdio and exposes the `run-collection` tool, which delegates execution to the external Bruno CLI command `bru`.

Keep the normalized output contract stable: `success`, `summary`, `failures`, and `timings`.

Module layout under `src/bruno_mcp/`:
- `server.py`: MCP tool registration and dispatch (thin layer).
- `runner.py`: orchestration of runs, collection resolution, workspace root confinement.
- `execution.py`: `bru` CLI discovery (pinned version, opt-in auto-install), timeouts, output caps, concurrency limits, and the temporary `--env-file` used to inject secrets without exposing them in CLI arguments.
- `filters.py`: filter scenario parsing, request rewriting, and validation checks.
- `artifacts.py`: confined artifact storage with owner-only permissions and retention cleanup.
- `redaction.py`: secret masking and bounded response previews.
- `settings.py`: runtime limits/security knobs from env vars and `bruno-mcp.toml`.

Run `pytest tests/`, `ruff check src tests scripts`, and `mypy src/bruno_mcp` before committing.
