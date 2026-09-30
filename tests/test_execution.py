"""Execution hardening: timeouts, output caps, concurrency limits, CLI discovery, secret env files."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from bruno_mcp.execution import (
    BruCliNotFoundError,
    BruExecutionError,
    BruExecutor,
    secret_env_file,
)
from bruno_mcp.settings import Settings


def run(coro):
    return asyncio.run(coro)


class EnsureBruCliTests:
    def test_fails_closed_when_bru_missing_and_auto_install_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        executor = BruExecutor(Settings(bru_auto_install=False))
        with pytest.raises(BruCliNotFoundError, match="npm install -g @usebruno/cli@"):
            run(executor.ensure_bru_cli())

    def test_auto_install_requires_npm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        executor = BruExecutor(Settings(bru_auto_install=True))
        with pytest.raises(BruCliNotFoundError, match="npm"):
            run(executor.ensure_bru_cli())

    def test_uses_existing_bru(self, stub_bru: Path) -> None:
        executor = BruExecutor(Settings())
        assert run(executor.ensure_bru_cli()).endswith("bru")


class RunLimitsTests:
    def test_timeout_kills_long_running_process(self, tmp_path: Path) -> None:
        executor = BruExecutor(Settings(run_timeout_seconds=0.5))
        start = time.monotonic()
        with pytest.raises(BruExecutionError, match="timed out"):
            run(executor.run(["sleep", "30"], tmp_path))
        assert time.monotonic() - start < 10

    def test_output_is_capped(self, tmp_path: Path) -> None:
        executor = BruExecutor(Settings(max_output_bytes=256))
        _code, stdout, _stderr = run(
            executor.run([sys.executable, "-c", "print('x' * 100000)"], tmp_path)
        )
        assert len(stdout) < 1000
        assert "truncated" in stdout

    def test_large_output_does_not_deadlock(self, tmp_path: Path) -> None:
        """Output beyond the cap must still be drained so the child can exit."""
        executor = BruExecutor(Settings(max_output_bytes=64, run_timeout_seconds=30))
        code, _stdout, _stderr = run(
            executor.run([sys.executable, "-c", "print('y' * 5000000)"], tmp_path)
        )
        assert code == 0

    def test_concurrency_limit_serializes_runs(self, tmp_path: Path) -> None:
        executor = BruExecutor(Settings(max_concurrent_runs=1, run_timeout_seconds=30))

        async def two_runs() -> float:
            start = time.monotonic()
            await asyncio.gather(
                executor.run(["sleep", "0.4"], tmp_path),
                executor.run(["sleep", "0.4"], tmp_path),
            )
            return time.monotonic() - start

        assert run(two_runs()) >= 0.8

    def test_parallel_when_limit_allows(self, tmp_path: Path) -> None:
        executor = BruExecutor(Settings(max_concurrent_runs=2, run_timeout_seconds=30))

        async def two_runs() -> float:
            start = time.monotonic()
            await asyncio.gather(
                executor.run(["sleep", "0.4"], tmp_path),
                executor.run(["sleep", "0.4"], tmp_path),
            )
            return time.monotonic() - start

        assert run(two_runs()) < 0.8


class SecretEnvFileTests:
    def test_file_is_private_and_valid_bru_json_env(self) -> None:
        with secret_env_file({"bearerToken": "s3cret-value"}) as path:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["variables"] == [
                {"name": "bearerToken", "value": "s3cret-value", "type": "text", "enabled": True, "secret": True}
            ]

    def test_file_is_deleted_after_use(self) -> None:
        with secret_env_file({"a": "b"}) as path:
            pass
        assert not path.exists()

    def test_file_deleted_on_exception(self) -> None:
        with pytest.raises(ValueError), secret_env_file({"a": "b"}) as path:
            raise ValueError("boom")
        assert not path.exists()
