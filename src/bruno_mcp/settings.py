"""Runtime settings for Bruno MCP, resolved from env vars and the TOML config file.

Environment variables always win over ``bruno-mcp.toml`` values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bruno_mcp.config import load_config_data

# Pinned Bruno CLI version used for auto-installs and the Docker image.
# Verify against https://www.npmjs.com/package/@usebruno/cli before bumping.
DEFAULT_BRU_CLI_VERSION = "4.2.0"
BRU_CLI_PACKAGE = "@usebruno/cli"

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bru_cli_version: str = DEFAULT_BRU_CLI_VERSION
    bru_auto_install: bool = False
    run_timeout_seconds: float = 300.0
    max_output_bytes: int = 8 * 1024 * 1024
    max_concurrent_runs: int = 2
    artifacts_dir: Path | None = None
    artifact_ttl_hours: float = 24.0
    artifact_max_files: int = 50
    enforce_root_confinement: bool = True
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUE_VALUES


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(name: str) -> int | None:
    value = _env_float(name)
    return int(value) if value is not None else None


def _section_value(section: str, key: str) -> Any:
    for data in load_config_data():
        value = data.get(section, {})
        if isinstance(value, dict) and key in value:
            return value[key]
    return None


def _resolve(explicit_env: Any, section: str, key: str, default: Any) -> Any:
    if explicit_env is not None:
        return explicit_env
    configured = _section_value(section, key)
    if configured is not None:
        return configured
    return default


def load_settings() -> Settings:
    """Build settings from env vars (priority) and the TOML config file."""
    artifacts_dir_raw = os.environ.get("BRUNO_MCP_ARTIFACTS_DIR") or _section_value("artifacts", "dir")
    artifacts_dir = Path(artifacts_dir_raw).expanduser() if isinstance(artifacts_dir_raw, str) else None

    return Settings(
        bru_cli_version=str(
            _resolve(os.environ.get("BRUNO_MCP_BRU_VERSION"), "bruno", "cli_version", DEFAULT_BRU_CLI_VERSION)
        ),
        bru_auto_install=bool(
            _resolve(_env_bool("BRUNO_MCP_AUTO_INSTALL_BRU"), "bruno", "auto_install", False)
        ),
        run_timeout_seconds=float(
            _resolve(_env_float("BRUNO_MCP_RUN_TIMEOUT"), "limits", "run_timeout_seconds", 300.0)
        ),
        max_output_bytes=int(
            _resolve(_env_int("BRUNO_MCP_MAX_OUTPUT_BYTES"), "limits", "max_output_bytes", 8 * 1024 * 1024)
        ),
        max_concurrent_runs=int(
            _resolve(_env_int("BRUNO_MCP_MAX_CONCURRENT_RUNS"), "limits", "max_concurrent_runs", 2)
        ),
        artifacts_dir=artifacts_dir,
        artifact_ttl_hours=float(
            _resolve(_env_float("BRUNO_MCP_ARTIFACT_TTL_HOURS"), "artifacts", "ttl_hours", 24.0)
        ),
        artifact_max_files=int(
            _resolve(_env_int("BRUNO_MCP_ARTIFACT_MAX_FILES"), "artifacts", "max_files", 50)
        ),
        enforce_root_confinement=bool(
            _resolve(
                _env_bool("BRUNO_MCP_ENFORCE_ROOT_CONFINEMENT"),
                "security",
                "enforce_root_confinement",
                True,
            )
        ),
    )
