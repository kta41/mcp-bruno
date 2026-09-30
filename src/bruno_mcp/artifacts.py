"""Artifact storage: confined, permission-hardened, with retention cleanup.

Raw Bruno JSON reports can contain sensitive response data, so artifacts are
written with owner-only permissions and expired automatically.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bruno_mcp.redaction import preview_response_data
from bruno_mcp.responses import extract_response_items
from bruno_mcp.settings import Settings, load_settings
from bruno_mcp.types import (
    ArtifactInfo,
    ArtifactRequestSummary,
    ReadRunArtifactParams,
    ReadRunArtifactResult,
    ResponseDataSummary,
)

logger = logging.getLogger("bruno_mcp.artifacts")

ARTIFACT_FILE_MODE = 0o600


def artifacts_dir(settings: Settings | None = None) -> Path:
    settings = settings or load_settings()
    directory = settings.artifacts_dir or (Path.cwd() / "build" / "artifacts")
    return directory.expanduser().resolve()


def write_artifact(prefix: str, content: str, settings: Settings | None = None) -> ArtifactInfo:
    settings = settings or load_settings()
    directory = artifacts_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    artifact_path = directory / f"{prefix}-{timestamp}.json"
    artifact_path.write_text(content, encoding="utf-8")
    os.chmod(artifact_path, ARTIFACT_FILE_MODE)
    logger.info("artifact_written path=%s bytes=%d", artifact_path, len(content))
    cleanup_old_artifacts(settings)
    return ArtifactInfo(
        path=str(artifact_path),
        description="Full raw Bruno JSON report. Use read-result-artifact for a bounded summary.",
    )


def cleanup_old_artifacts(settings: Settings | None = None) -> None:
    """Delete artifacts older than the TTL and cap the total number of files."""
    settings = settings or load_settings()
    directory = artifacts_dir(settings)
    if not directory.is_dir():
        return

    now = time.time()
    ttl_seconds = settings.artifact_ttl_hours * 3600
    candidates = sorted(
        (path for path in directory.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )

    remaining = candidates
    if ttl_seconds > 0:
        for path in [p for p in candidates if now - p.stat().st_mtime > ttl_seconds]:
            _unlink_quietly(path)
        remaining = [p for p in candidates if p.is_file()]

    if settings.artifact_max_files > 0 and len(remaining) > settings.artifact_max_files:
        for path in remaining[: len(remaining) - settings.artifact_max_files]:
            _unlink_quietly(path)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        logger.warning("artifact_cleanup_failed path=%s", path)


def read_run_artifact(params: ReadRunArtifactParams, settings: Settings | None = None) -> ReadRunArtifactResult:
    """Read a bounded, redacted summary from an artifact confined to the artifacts directory."""
    artifact_path = Path(params.path).expanduser().resolve()
    root = artifacts_dir(settings)
    if root not in [artifact_path, *artifact_path.parents]:
        raise PermissionError("Artifact path is outside the Bruno MCP artifacts directory.")
    if not artifact_path.is_file():
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")

    artifact_data = json.loads(artifact_path.read_text(encoding="utf-8"))
    requests = []
    for report in _artifact_reports(artifact_data):
        first_result = report[0] if isinstance(report, list) and report else {}
        for result in first_result.get("results", []) or []:
            requests.append(_artifact_request_summary(result, params.max_items))

    return ReadRunArtifactResult(
        artifact=ArtifactInfo(path=str(artifact_path), description="Raw Bruno JSON report stored locally."),
        requests=requests,
    )


def _artifact_reports(artifact_data: Any) -> list[Any]:
    if (
        isinstance(artifact_data, list)
        and artifact_data
        and isinstance(artifact_data[0], dict)
        and "report" in artifact_data[0]
    ):
        return [item.get("report") for item in artifact_data if isinstance(item, dict)]
    return [artifact_data]


def _artifact_request_summary(result: dict, max_items: int) -> ArtifactRequestSummary:
    request = result.get("request") or {}
    response = result.get("response") or {}
    response_data = response.get("data")
    return ArtifactRequestSummary(
        name=result.get("name") or result.get("suitename") or "Unknown request",
        path=result.get("path"),
        method=request.get("method"),
        url=request.get("url"),
        status=response.get("status") or result.get("status"),
        status_text=response.get("statusText"),
        response_time=response.get("responseTime") or response.get("duration"),
        response_data=response_data_summary(response_data, max_items),
    )


def response_data_summary(response_data: Any, max_items: int) -> ResponseDataSummary:
    items = extract_response_items(response_data)
    top_level_keys = list(response_data.keys())[:20] if isinstance(response_data, dict) else []
    item_keys: list[str] = []
    sample_items: list[Any] = []

    if items is not None:
        key_names: set[str] = set()
        for item in items[:max_items]:
            if isinstance(item, dict):
                key_names.update(str(key) for key in item)
            sample_items.append(preview_response_data(item))
        item_keys = sorted(key_names)[:30]

    return ResponseDataSummary(
        data_type=type(response_data).__name__ if response_data is not None else None,
        item_count=len(items) if items is not None else None,
        top_level_keys=top_level_keys,
        item_keys=item_keys,
        sample_items=sample_items,
    )
