"""Secret injection: values must travel via a private --env-file and process env, never via argv."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bruno_mcp.runner import BrunoRunner
from bruno_mcp.settings import Settings
from bruno_mcp.types import RunCollectionParams

SECRET_VALUE = "super-secret-token-value-12345"


def run(coro):
    return asyncio.run(coro)


def make_runner(tmp_path: Path) -> BrunoRunner:
    return BrunoRunner(Settings(artifacts_dir=tmp_path / "artifacts"))


def read_capture(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))


class SecretInjectionTests:
    def test_secret_is_never_in_bru_argv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_bru: Path,
        bruno_workspace: Path,
        sample_report: Path,
    ) -> None:
        monkeypatch.setenv("BRUNO_AUTH_TOKEN", SECRET_VALUE)
        monkeypatch.setenv("BRU_CAPTURE", str(tmp_path / "capture.json"))
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        runner = make_runner(tmp_path)
        collection = bruno_workspace / "collections" / "project1"

        result = run(
            runner.run_collection(
                RunCollectionParams(collection=str(collection), inherited_variables=["BRUNO_AUTH_TOKEN"])
            )
        )

        assert result.success
        capture = read_capture(tmp_path)
        assert SECRET_VALUE not in " ".join(capture["argv"])
        assert "--env-file" in capture["argv"]
        env_file_payload = json.loads(capture["env_file"])
        names = {variable["name"] for variable in env_file_payload["variables"]}
        # Aliases are expanded so {{bearerToken}}-style references resolve too.
        assert "BRUNO_AUTH_TOKEN" in names
        assert "bearerToken" in names
        assert all(variable["value"] == SECRET_VALUE for variable in env_file_payload["variables"])
        # Secrets also reach bru as process env vars for {{process.env.NAME}} lookups.
        assert capture["process_env"]["BRUNO_AUTH_TOKEN"] == SECRET_VALUE
        # Diagnostics never contain the secret value either.
        assert SECRET_VALUE not in json.dumps(result.model_dump())

    def test_secret_env_file_is_deleted_after_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_bru: Path,
        bruno_workspace: Path,
        sample_report: Path,
    ) -> None:
        monkeypatch.setenv("BRUNO_AUTH_TOKEN", SECRET_VALUE)
        monkeypatch.setenv("BRU_CAPTURE", str(tmp_path / "capture.json"))
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        runner = make_runner(tmp_path)
        collection = bruno_workspace / "collections" / "project1"

        run(
            runner.run_collection(
                RunCollectionParams(collection=str(collection), inherited_variables=["BRUNO_AUTH_TOKEN"])
            )
        )

        capture = read_capture(tmp_path)
        env_file_path = Path(capture["argv"][capture["argv"].index("--env-file") + 1])
        assert not env_file_path.exists()

    def test_conflicting_env_variable_is_neutralized_in_sanitized_copy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_bru: Path,
        bruno_workspace: Path,
        sample_report: Path,
    ) -> None:
        """The dev.yml env file defines bearerToken; the injected secret must win, so the
        run must happen in a sanitized copy where the conflicting entry is removed."""
        monkeypatch.setenv("BRUNO_AUTH_TOKEN", SECRET_VALUE)
        monkeypatch.setenv("BRU_CAPTURE", str(tmp_path / "capture.json"))
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        runner = make_runner(tmp_path)
        collection = bruno_workspace / "collections" / "project1"

        run(
            runner.run_collection(
                RunCollectionParams(
                    collection=str(collection),
                    environment="dev",
                    inherited_variables=["BRUNO_AUTH_TOKEN"],
                )
            )
        )

        capture = read_capture(tmp_path)
        run_cwd = Path(capture["cwd"])
        # bru ran in a temporary sanitized copy, not the real collection directory.
        assert run_cwd != collection
        env_copy = run_cwd / "environments" / "dev.yml"
        if env_copy.is_file():
            content = env_copy.read_text(encoding="utf-8")
            assert "bearerToken" not in content
            assert "baseUrl" in content
        # The real environment file is untouched.
        original = (bruno_workspace / "environments" / "dev.yml").read_text(encoding="utf-8")
        assert "bearerToken" in original

    def test_missing_secret_raises_clear_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_bru: Path,
        bruno_workspace: Path,
    ) -> None:
        monkeypatch.delenv("BRUNO_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("BRUNO_BEARER_TOKEN", raising=False)
        runner = make_runner(tmp_path)
        collection = bruno_workspace / "collections" / "project1"

        with pytest.raises(RuntimeError, match="Missing inherited environment variable"):
            run(
                runner.run_collection(
                    RunCollectionParams(collection=str(collection), inherited_variables=["BRUNO_AUTH_TOKEN"])
                )
            )


class RootConfinementTests:
    def test_rejects_collection_outside_configured_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bruno_workspace: Path
    ) -> None:
        outside = tmp_path / "elsewhere" / "collection"
        outside.mkdir(parents=True)
        (outside / "opencollection.yml").write_text("opencollection: 1\n", encoding="utf-8")
        runner = make_runner(tmp_path)

        with pytest.raises(PermissionError, match="outside the configured workspace roots"):
            run(runner.run_collection(RunCollectionParams(collection=str(outside))))

    def test_allows_collection_inside_configured_roots(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_bru: Path,
        bruno_workspace: Path,
        sample_report: Path,
    ) -> None:
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        runner = make_runner(tmp_path)
        collection = bruno_workspace / "collections" / "project1"

        result = run(runner.run_collection(RunCollectionParams(collection=str(collection))))
        assert result.success

    def test_allows_any_path_when_no_root_exists_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bru: Path, sample_report: Path
    ) -> None:
        config = tmp_path / "bruno-mcp.toml"
        config.write_text('[workspace]\nroots = ["/nonexistent/dummy/root"]\n', encoding="utf-8")
        monkeypatch.setenv("BRUNO_MCP_CONFIG", str(config))
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        monkeypatch.chdir(tmp_path)
        collection = tmp_path / "anywhere" / "proj"
        collection.mkdir(parents=True)
        (collection / "opencollection.yml").write_text("opencollection: 1\n", encoding="utf-8")

        runner = make_runner(tmp_path)
        result = run(runner.run_collection(RunCollectionParams(collection=str(collection))))
        assert result.success

    def test_confinement_can_be_disabled_explicitly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bru: Path, bruno_workspace: Path, sample_report: Path
    ) -> None:
        monkeypatch.setenv("BRU_REPORT", str(sample_report))
        outside = tmp_path / "elsewhere" / "collection"
        outside.mkdir(parents=True)
        (outside / "opencollection.yml").write_text("opencollection: 1\n", encoding="utf-8")
        runner = BrunoRunner(Settings(artifacts_dir=tmp_path / "artifacts", enforce_root_confinement=False))

        result = run(runner.run_collection(RunCollectionParams(collection=str(outside))))
        assert result.success
