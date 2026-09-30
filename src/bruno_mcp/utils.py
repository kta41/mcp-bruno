from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path


@asynccontextmanager
async def report_file(prefix: str, extension: str) -> AsyncIterator[Path]:
    reports_dir = Path.cwd() / "build" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    output_file = reports_dir / f"{prefix}{int(time.time() * 1000)}{extension}"
    try:
        yield output_file
    finally:
        with suppress(FileNotFoundError):
            output_file.unlink()
