from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator


@asynccontextmanager
async def report_file(prefix: str, extension: str) -> AsyncIterator[Path]:
    reports_dir = Path.cwd() / "build" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    output_file = reports_dir / f"{prefix}{int(time.time() * 1000)}{extension}"
    try:
        yield output_file
    finally:
        try:
            output_file.unlink()
        except FileNotFoundError:
            pass
