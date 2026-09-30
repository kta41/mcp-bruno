"""Bruno CLI execution: version pinning, timeouts, output caps, and concurrency limits."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bruno_mcp.settings import BRU_CLI_PACKAGE, Settings, load_settings

logger = logging.getLogger("bruno_mcp.execution")

_STREAM_CHUNK_BYTES = 65536
_PROCESS_EXIT_GRACE_SECONDS = 5.0


class BruExecutionError(RuntimeError):
    pass


class BruCliNotFoundError(BruExecutionError):
    pass


@contextmanager
def secret_env_file(variables: dict[str, str]) -> Iterator[Path]:
    """Write secret variables to a private temp JSON env file accepted by `bru run --env-file`.

    Secrets never appear in the bru argument list (and therefore not in `ps`
    output or in logs), and the file is deleted when the context exits.
    """
    payload = {
        "variables": [
            {"name": name, "value": value, "type": "text", "enabled": True, "secret": True}
            for name, value in variables.items()
        ]
    }
    fd, raw_path = tempfile.mkstemp(prefix="bruno-mcp-env-", suffix=".json")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)


class BruExecutor:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or load_settings()
        self._semaphore = asyncio.Semaphore(self._settings.max_concurrent_runs)
        self._bru_command: str | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    async def ensure_bru_cli(self) -> str:
        if self._bru_command:
            return self._bru_command

        bru_command = shutil.which("bru")
        if bru_command:
            await self._warn_on_version_mismatch(bru_command)
            self._bru_command = bru_command
            return bru_command

        if not self._settings.bru_auto_install:
            raise BruCliNotFoundError(
                "Bruno CLI command `bru` is not installed. "
                f"Install it with `npm install -g {BRU_CLI_PACKAGE}@{self._settings.bru_cli_version}` "
                "or set BRUNO_MCP_AUTO_INSTALL_BRU=1 to let the server install the pinned version."
            )

        await self._install_bru_cli()
        bru_command = shutil.which("bru")
        if not bru_command:
            raise BruCliNotFoundError("Bruno CLI installation completed, but `bru` is still not available in PATH.")
        self._bru_command = bru_command
        return bru_command

    async def _warn_on_version_mismatch(self, bru_command: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                bru_command,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError, TimeoutError):
            return
        installed = stdout_bytes.decode(errors="replace").strip().splitlines()
        if not installed:
            return
        installed_version = installed[-1].strip().lstrip("v")
        pinned_version = self._settings.bru_cli_version
        if installed_version.split(".", 1)[0] != pinned_version.split(".", 1)[0]:
            logger.warning(
                "bru_version_mismatch installed=%s pinned=%s",
                installed_version,
                pinned_version,
            )

    async def _install_bru_cli(self) -> None:
        npm_command = shutil.which("npm")
        if not npm_command:
            raise BruCliNotFoundError(
                "Bruno CLI command `bru` is not installed and `npm` was not found, so it cannot be installed automatically."
            )

        pinned_package = f"{BRU_CLI_PACKAGE}@{self._settings.bru_cli_version}"
        logger.info("bru_auto_install package=%s", pinned_package)
        process = await asyncio.create_subprocess_exec(
            npm_command,
            "install",
            "-g",
            pinned_package,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            stderr = stderr_bytes.decode(errors="replace")
            raise BruExecutionError(
                f"Bruno CLI `bru` is not installed and automatic npm installation of {pinned_package} failed: {stderr}"
            )

    async def run(self, args: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run bru with a concurrency limit, a timeout, and bounded stdout/stderr capture."""
        timeout = self._settings.run_timeout_seconds
        limit = self._settings.max_output_bytes
        async with self._semaphore:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_result, stderr_result = await asyncio.wait_for(
                    asyncio.gather(
                        self._read_stream_limited(process.stdout, limit),
                        self._read_stream_limited(process.stderr, limit),
                    ),
                    timeout=timeout,
                )
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_EXIT_GRACE_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                process.kill()
                await process.wait()
                raise BruExecutionError(
                    f"Bruno CLI timed out after {timeout:.0f}s and was killed. "
                    "Increase BRUNO_MCP_RUN_TIMEOUT if the collection legitimately needs longer."
                ) from None

        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        truncation_note = " [truncated: output exceeded BRUNO_MCP_MAX_OUTPUT_BYTES]"
        return (
            process.returncode or 0,
            stdout.decode(errors="replace") + (truncation_note if stdout_truncated else ""),
            stderr.decode(errors="replace") + (truncation_note if stderr_truncated else ""),
        )

    @staticmethod
    async def _read_stream_limited(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
        """Drain the stream fully (so the child never blocks on a full pipe) but keep at most `limit` bytes."""
        if stream is None:
            return b"", False
        chunks: list[bytes] = []
        kept = 0
        truncated = False
        while True:
            chunk = await stream.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            if kept < limit:
                take = chunk[: limit - kept]
                chunks.append(take)
                kept += len(take)
                truncated = truncated or len(take) < len(chunk)
            else:
                truncated = True
        return b"".join(chunks), truncated

    def masked_args_for_log(self, args: list[str]) -> list[str]:
        from bruno_mcp.redaction import mask_bru_args

        return mask_bru_args(args)
