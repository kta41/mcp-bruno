"""Shared fixtures: a stub `bru` executable and an isolated Bruno workspace layout."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

STUB_BRU_SOURCE = """#!{python}
import json
import os
import sys

args = sys.argv[1:]

if "--version" in args:
    print(os.environ.get("BRU_VERSION_OUTPUT", "4.2.0"))
    sys.exit(0)

capture_path = os.environ.get("BRU_CAPTURE")
capture = {{"argv": args, "cwd": os.getcwd(), "env_file": None, "process_env": {{}}}}
if "--env-file" in args:
    env_file_path = args[args.index("--env-file") + 1]
    with open(env_file_path, encoding="utf-8") as handle:
        capture["env_file"] = handle.read()
for name in ("BRUNO_AUTH_TOKEN", "bearerToken", "BRUNO_API_KEY"):
    if name in os.environ:
        capture["process_env"][name] = os.environ[name]
if capture_path:
    with open(capture_path, "w", encoding="utf-8") as handle:
        json.dump(capture, handle)

sleep_seconds = float(os.environ.get("BRU_SLEEP", "0"))
if sleep_seconds:
    import time

    time.sleep(sleep_seconds)

report_path = os.environ.get("BRU_REPORT")
if "--reporter-json" in args:
    output_path = args[args.index("--reporter-json") + 1]
    if report_path:
        with open(report_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = [{{"summary": {{"totalRequests": 1}}, "results": []}}]
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

sys.exit(int(os.environ.get("BRU_EXIT", "0")))
"""


@pytest.fixture
def stub_bru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a stub `bru` on PATH and return the directory containing it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bru_path = bin_dir / "bru"
    bru_path.write_text(STUB_BRU_SOURCE.format(python=sys.executable), encoding="utf-8")
    bru_path.chmod(bru_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


@pytest.fixture
def bruno_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal Bruno workspace and point BRUNO_MCP_CONFIG at it."""
    root = tmp_path / "bruno"
    collection = root / "collections" / "project1"
    environments = root / "environments"
    collection.mkdir(parents=True)
    environments.mkdir(parents=True)

    (collection / "opencollection.yml").write_text(
        "opencollection: 1\ninfo:\n  name: project1\n", encoding="utf-8"
    )
    (environments / "dev.yml").write_text(
        "variables:\n"
        "  - name: baseUrl\n"
        "    value: https://example.test\n"
        "  - name: bearerToken\n"
        "    value: placeholder-in-env-file\n",
        encoding="utf-8",
    )

    config = tmp_path / "bruno-mcp.toml"
    config.write_text(f'[workspace]\nroots = ["{root}"]\n', encoding="utf-8")
    monkeypatch.setenv("BRUNO_MCP_CONFIG", str(config))
    monkeypatch.chdir(tmp_path)
    return root


@pytest.fixture
def sample_report(tmp_path: Path) -> Path:
    """Write a bru JSON report with one passing request and return its path."""
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            [
                {
                    "summary": {"totalRequests": 1},
                    "results": [
                        {
                            "name": "List items",
                            "path": "requests/list.yml",
                            "request": {"method": "GET", "url": "https://example.test/items"},
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "responseTime": 12,
                                "data": {"items": [{"id": 1, "token": "secret-value"}], "total": 1},
                            },
                            "testResults": [{"status": "pass"}],
                            "assertionResults": [],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return report
