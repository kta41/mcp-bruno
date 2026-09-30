from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG_PATHS = [
    Path.cwd() / "bruno-mcp.toml",
    Path.home() / ".config" / "bruno-mcp" / "config.toml",
]

DEFAULT_AUTH_VARIABLE_ALIASES = {
    "BRUNO_AUTH_TOKEN": [
        "BRUNO_AUTH_TOKEN",
        "bearerToken",
        "BEARER_TOKEN",
        "AUTH_TOKEN",
        "TOKEN",
        "accessToken",
        "access_token",
    ],
    "BRUNO_BEARER_TOKEN": [
        "bearerToken",
        "BEARER_TOKEN",
        "AUTH_TOKEN",
        "TOKEN",
    ],
    "BRUNO_API_KEY": [
        "BRUNO_API_KEY",
        "apiKey",
        "API_KEY",
        "xApiKey",
        "x-api-key",
    ],
}


def load_bruno_roots() -> list[Path]:
    roots: list[Path] = []
    for data in load_config_data():
        workspace = data.get("workspace", {})
        configured_roots = workspace.get("roots", [])
        if not isinstance(configured_roots, list):
            continue

        roots.extend(Path(root).expanduser() for root in configured_roots if isinstance(root, str))

    return roots


def load_auth_variable_aliases() -> dict[str, list[str]]:
    aliases = {source: list(targets) for source, targets in DEFAULT_AUTH_VARIABLE_ALIASES.items()}

    for data in load_config_data():
        auth = data.get("auth", {})
        configured_aliases = auth.get("variable_aliases", {})
        if not isinstance(configured_aliases, dict):
            continue

        for source, targets in configured_aliases.items():
            if isinstance(source, str) and isinstance(targets, list):
                aliases[source] = [target for target in targets if isinstance(target, str)]

    return aliases


def load_config_data() -> list[dict]:
    config_path = os.environ.get("BRUNO_MCP_CONFIG")
    paths = [Path(config_path).expanduser()] if config_path else DEFAULT_CONFIG_PATHS

    data_items = []
    for path in paths:
        if path.is_file():
            data_items.append(tomllib.loads(path.read_text(encoding="utf-8")))

    return data_items
