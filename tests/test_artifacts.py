"""Artifact storage hardening: permissions, confinement, and retention."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from bruno_mcp.artifacts import artifacts_dir, cleanup_old_artifacts, read_run_artifact, write_artifact
from bruno_mcp.settings import Settings
from bruno_mcp.types import ReadRunArtifactParams


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(artifacts_dir=tmp_path / "artifacts")


class WriteArtifactTests:
    def test_artifact_is_written_with_owner_only_permissions(self, settings: Settings) -> None:
        info = write_artifact("bruno-run", "[]", settings)
        path = Path(info.path)
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_artifact_uses_configured_directory(self, settings: Settings) -> None:
        info = write_artifact("bruno-run", "[]", settings)
        assert Path(info.path).parent == artifacts_dir(settings)


class RetentionTests:
    @staticmethod
    def _make_artifact(settings: Settings, name: str, mtime: float) -> Path:
        directory = artifacts_dir(settings)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("[]", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_artifacts_older_than_ttl_are_removed(self, settings: Settings) -> None:
        old_path = self._make_artifact(settings, "bruno-run-old.json", time.time() - 3600 * 48)
        fresh = self._make_artifact(settings, "bruno-run-fresh.json", time.time())

        cleanup_old_artifacts(Settings(artifacts_dir=settings.artifacts_dir, artifact_ttl_hours=24))

        assert not old_path.exists()
        assert fresh.exists()

    def test_artifact_count_is_capped(self, settings: Settings) -> None:
        paths = [
            self._make_artifact(settings, f"bruno-run-{index}.json", time.time() - (5 - index))
            for index in range(5)
        ]

        cleanup_old_artifacts(Settings(artifacts_dir=settings.artifacts_dir, artifact_max_files=2))

        remaining = sorted(p.name for p in artifacts_dir(settings).glob("*.json"))
        assert remaining == sorted(p.name for p in paths[-2:])


class ReadArtifactTests:
    def test_reads_bounded_redacted_summary(self, settings: Settings, sample_report: Path) -> None:
        artifact_path = artifacts_dir(settings)
        artifact_path.mkdir(parents=True)
        target = artifact_path / "bruno-run-test.json"
        target.write_text(sample_report.read_text(encoding="utf-8"), encoding="utf-8")

        result = read_run_artifact(ReadRunArtifactParams(path=str(target)), settings)

        assert len(result.requests) == 1
        request = result.requests[0]
        assert request.status == 200
        assert "secret-value" not in json.dumps(result.model_dump())

    def test_rejects_paths_outside_artifacts_dir(self, settings: Settings, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere.json"
        outside.write_text("[]", encoding="utf-8")
        with pytest.raises(PermissionError):
            read_run_artifact(ReadRunArtifactParams(path=str(outside)), settings)

    def test_rejects_traversal_attempts(self, settings: Settings) -> None:
        traversal = str(artifacts_dir(settings) / ".." / ".." / "etc-passwd")
        with pytest.raises(PermissionError):
            read_run_artifact(ReadRunArtifactParams(path=traversal), settings)

    def test_missing_artifact_raises_not_found(self, settings: Settings) -> None:
        missing = artifacts_dir(settings) / "nope.json"
        with pytest.raises(FileNotFoundError):
            read_run_artifact(ReadRunArtifactParams(path=str(missing)), settings)
