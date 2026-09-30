"""Output contract stability: the normalized result shape must not drift."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bruno_mcp.runner import BrunoRunner
from bruno_mcp.settings import Settings
from bruno_mcp.types import (
    ReadRunArtifactParams,
    RunCollectionParams,
    RunFilterScenariosParams,
    RunFullValidationParams,
)


def run(coro):
    return asyncio.run(coro)


class NormalizedContractTests:
    def test_run_collection_result_exposes_stable_contract(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_bru: Path,
        bruno_workspace: Path,
        sample_report: Path,
    ) -> None:
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        runner = BrunoRunner(Settings(artifacts_dir=tmp_path / "artifacts"))
        collection = bruno_workspace / "collections" / "project1"

        result = run(runner.run_collection(RunCollectionParams(collection=str(collection))))
        payload = result.model_dump(exclude_none=True)

        # The normalized contract: success, summary, failures, timings.
        assert {"success", "summary", "failures", "timings"} <= set(payload)
        assert isinstance(payload["success"], bool)
        assert {"total", "failed", "passed"} <= set(payload["summary"])
        assert isinstance(payload["failures"], list)
        assert {"started", "completed", "duration"} <= set(payload["timings"])

    def test_run_collection_serializes_to_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bru: Path, bruno_workspace: Path, sample_report: Path
    ) -> None:
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        runner = BrunoRunner(Settings(artifacts_dir=tmp_path / "artifacts"))
        collection = bruno_workspace / "collections" / "project1"

        result = run(runner.run_collection(RunCollectionParams(collection=str(collection))))
        json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False)

    def test_failed_requests_populate_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bru: Path, bruno_workspace: Path
    ) -> None:
        report = tmp_path / "report.json"
        report.write_text(
            json.dumps(
                [
                    {
                        "summary": {"totalRequests": 1},
                        "results": [
                            {
                                "name": "Broken",
                                "response": {"status": 500, "statusText": "Server Error"},
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BRU_REPORT", str(report))
        runner = BrunoRunner(Settings(artifacts_dir=tmp_path / "artifacts"))
        collection = bruno_workspace / "collections" / "project1"

        result = run(runner.run_collection(RunCollectionParams(collection=str(collection))))

        assert not result.success
        assert result.summary.failed == 1
        assert len(result.failures) == 1


class InputValidationTests:
    def test_variables_must_be_key_value(self) -> None:
        with pytest.raises(ValidationError):
            RunCollectionParams(collection="/tmp/x", variables=["NOEQUALSSIGN"])

    def test_variable_names_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            RunCollectionParams(collection="/tmp/x", variables=["1BAD=value"])

    def test_inherited_variable_names_must_be_identifiers(self) -> None:
        with pytest.raises(ValidationError):
            RunCollectionParams(collection="/tmp/x", inherited_variables=["bad name;rm -rf"])

    def test_empty_collection_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunCollectionParams(collection="")

    def test_max_items_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ReadRunArtifactParams(path="/tmp/a.json", max_items=0)
        with pytest.raises(ValidationError):
            ReadRunArtifactParams(path="/tmp/a.json", max_items=1000)

    def test_max_scenarios_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RunFilterScenariosParams(collection="/tmp/x", max_scenarios=0)
        with pytest.raises(ValidationError):
            RunFullValidationParams(collection="/tmp/x", max_scenarios=501)

    def test_oversized_strings_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RunCollectionParams(collection="/tmp/x", environment="e" * 300)
