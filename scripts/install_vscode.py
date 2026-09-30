from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "bruno-runner"
DEFAULT_BRUNO_CONFIG_PATH = Path.home() / ".config" / "bruno-mcp" / "config.toml"

# Keep in sync with bruno_mcp.settings.DEFAULT_BRU_CLI_VERSION.
DEFAULT_BRU_CLI_VERSION = "4.2.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Bruno MCP for VS Code.")
    parser.add_argument(
        "--with-copilot-customizations",
        action="store_true",
        help="Also install the reusable Copilot prompt and agent in the user's global VS Code customization folders.",
    )
    parser.add_argument(
        "--vscode-user-dir",
        type=Path,
        default=default_vscode_user_dir(),
        help="VS Code user profile directory containing mcp.json.",
    )
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        default=Path.home() / ".config" / "github-copilot-prompts",
        help="Global Copilot prompts directory.",
    )
    parser.add_argument(
        "--agent-dir",
        type=Path,
        default=Path.home() / ".config" / "github-copilot-agents",
        help="Global Copilot agents directory.",
    )
    parser.add_argument(
        "--vscode-prompt-dir",
        type=Path,
        default=Path.home() / ".vscode-server" / "data" / "User" / "prompts",
        help="VS Code user-level prompt/customization directory.",
    )
    parser.add_argument(
        "--vscode-agent-dir",
        type=Path,
        default=Path.home() / ".vscode-server" / "data" / "User" / "agents",
        help="VS Code user-level agents directory.",
    )
    args = parser.parse_args()

    ensure_bru_cli()
    install_mcp_config(args.vscode_user_dir)
    install_remote_wsl_mcp_config()
    install_bruno_config(DEFAULT_BRUNO_CONFIG_PATH)

    if args.with_copilot_customizations:
        install_copilot_customizations(args.prompt_dir, args.agent_dir, args.vscode_prompt_dir, args.vscode_agent_dir)

    print("Bruno MCP VS Code installation completed.")


def default_vscode_user_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Code" / "User"

    windows_user_dir = Path("/mnt/c/Users")
    username = os.environ.get("USER")
    if username and (windows_user_dir / username / "AppData" / "Roaming" / "Code" / "User").exists():
        return windows_user_dir / username / "AppData" / "Roaming" / "Code" / "User"

    return Path.home() / ".config" / "Code" / "User"


def install_mcp_config(vscode_user_dir: Path) -> None:
    vscode_user_dir.mkdir(parents=True, exist_ok=True)
    mcp_file = vscode_user_dir / "mcp.json"
    config = read_json_object(mcp_file)
    config.setdefault("servers", {})[SERVER_NAME] = build_server_config()
    ensure_input(config, "BRUNO_AUTH_TOKEN", "Optional auth token exposed to Bruno as bearerToken via aliases")
    ensure_input(config, "BRUNO_BEARER_TOKEN", "Optional bearer token exposed to Bruno as bearerToken")
    ensure_input(config, "BRUNO_API_KEY", "Optional API key exposed to Bruno as BRUNO_API_KEY")
    write_json(mcp_file, config)
    print(f"Installed MCP server '{SERVER_NAME}' in {mcp_file}")


def install_remote_wsl_mcp_config() -> None:
    remote_user_dir = Path.home() / ".vscode-server" / "data" / "User"
    if not remote_user_dir.exists():
        return

    remote_user_dir.mkdir(parents=True, exist_ok=True)
    mcp_file = remote_user_dir / "mcp.json"
    config = read_json_object(mcp_file)
    config.setdefault("servers", {})[SERVER_NAME] = build_remote_server_config()
    ensure_input(config, "BRUNO_AUTH_TOKEN", "Optional auth token exposed to Bruno as bearerToken via aliases")
    ensure_input(config, "BRUNO_BEARER_TOKEN", "Optional bearer token exposed to Bruno as bearerToken")
    ensure_input(config, "BRUNO_API_KEY", "Optional API key exposed to Bruno as BRUNO_API_KEY")
    write_json(mcp_file, config)
    print(f"Installed Remote-WSL MCP server '{SERVER_NAME}' in {mcp_file}")


def build_server_config() -> dict[str, Any]:
    uv_path = find_uv()
    wsl_distro = detect_wsl_distro()

    if wsl_distro:
        return {
            "type": "stdio",
            "command": "wsl.exe",
            "args": [
                "-d",
                wsl_distro,
                "--",
                "env",
                "UV_DEFAULT_INDEX=https://pypi.org/simple",
                "BRUNO_AUTH_TOKEN=${input:BRUNO_AUTH_TOKEN}",
                "BRUNO_BEARER_TOKEN=${input:BRUNO_BEARER_TOKEN}",
                "BRUNO_API_KEY=${input:BRUNO_API_KEY}",
                uv_path,
                "--directory",
                str(PROJECT_ROOT),
                "run",
                "bruno-mcp",
            ],
        }

    return {
        "type": "stdio",
        "command": uv_path,
        "args": ["--directory", str(PROJECT_ROOT), "run", "bruno-mcp"],
        "env": {
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "BRUNO_AUTH_TOKEN": "${input:BRUNO_AUTH_TOKEN}",
            "BRUNO_BEARER_TOKEN": "${input:BRUNO_BEARER_TOKEN}",
            "BRUNO_API_KEY": "${input:BRUNO_API_KEY}",
        },
    }


def build_remote_server_config() -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": find_uv(),
        "args": ["--directory", str(PROJECT_ROOT), "run", "bruno-mcp"],
        "env": {
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "BRUNO_AUTH_TOKEN": "${input:BRUNO_AUTH_TOKEN}",
            "BRUNO_BEARER_TOKEN": "${input:BRUNO_BEARER_TOKEN}",
            "BRUNO_API_KEY": "${input:BRUNO_API_KEY}",
        },
    }


def find_uv() -> str:
    local_uv = Path.home() / ".local" / "bin" / "uv"
    if local_uv.exists():
        return str(local_uv)

    uv_path = shutil.which("uv")
    if uv_path:
        return uv_path

    return "uv"


def detect_wsl_distro() -> str | None:
    if not Path("/proc/version").exists():
        return None

    try:
        version = Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return None

    if "microsoft" not in version and "wsl" not in version:
        return None

    try:
        result = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    distros = [line.strip().replace("\x00", "") for line in result.stdout.splitlines()]
    distros = [distro for distro in distros if distro]
    return distros[0] if distros else None


def install_copilot_customizations(
    prompt_dir: Path,
    agent_dir: Path,
    vscode_prompt_dir: Path,
    vscode_agent_dir: Path,
) -> None:
    prompt_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    vscode_prompt_dir.mkdir(parents=True, exist_ok=True)
    vscode_agent_dir.mkdir(parents=True, exist_ok=True)

    copy_file(
        PROJECT_ROOT / "copilot" / "prompts" / "run-bruno-collection.prompt.md",
        prompt_dir / "run-bruno-collection.prompt.md",
    )
    copy_file(
        PROJECT_ROOT / "copilot" / "agents" / "bruno-runner.agent.md",
        agent_dir / "bruno-runner.agent.md",
    )
    copy_file(
        PROJECT_ROOT / "copilot" / "prompts" / "run-bruno-collection.prompt.md",
        vscode_prompt_dir / "run-bruno-collection.prompt.md",
    )
    copy_file(
        PROJECT_ROOT / "copilot" / "agents" / "bruno-runner.agent.md",
        vscode_agent_dir / "bruno-runner.agent.md",
    )
    print(f"Installed Copilot prompt in {prompt_dir}")
    print(f"Installed Copilot agent in {agent_dir}")
    print(f"Installed VS Code user prompt in {vscode_prompt_dir}")
    print(f"Installed VS Code user agent in {vscode_agent_dir}")


def install_bruno_config(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        print(f"Bruno MCP config already exists, leaving it unchanged: {config_path}")
        return

    config_path.write_text(default_bruno_config(), encoding="utf-8")
    print(f"Created starter Bruno MCP config: {config_path}")
    print("Edit this file and replace the dummy roots with real Bruno project paths.")


def default_bruno_config() -> str:
    return """# Bruno MCP configuration
# Replace the dummy root below with one or more real Bruno roots.
# A Bruno root is usually the directory that contains both `collections/` and `environments/`.
#
# Example:
# roots = [
#   \"/home/your-user/work/my-service/bruno\"
# ]

[workspace]
roots = [
    \"/path/to/your/project/bruno\"
]

[auth]
# These are variable names only. Values must be provided by VS Code MCP secure inputs,
# shell environment variables, or your Bruno environment files. Do not write secret values here.
inherited_variables = [
    \"BRUNO_AUTH_TOKEN\",
    \"BRUNO_API_KEY\"
]

# Map secure MCP input names to the variable names used inside Bruno collections.
# For example, if requests use {{bearerToken}}, the value entered as BRUNO_AUTH_TOKEN
# will be passed to Bruno as bearerToken.
[auth.variable_aliases]
BRUNO_AUTH_TOKEN = ["BRUNO_AUTH_TOKEN", "bearerToken", "BEARER_TOKEN", "AUTH_TOKEN", "TOKEN", "accessToken", "access_token"]
BRUNO_API_KEY = ["BRUNO_API_KEY", "apiKey", "API_KEY", "xApiKey", "x-api-key"]

# Execution hardening defaults (see bruno-mcp.example.toml for the full reference):
# [bruno]     cli_version pins the Bruno CLI; auto_install defaults to false (fail closed).
# [limits]    run_timeout_seconds, max_output_bytes, max_concurrent_runs.
# [artifacts] ttl_hours / max_files control retention of raw run reports.
# [security]  enforce_root_confinement (default true) rejects collection paths outside
#             [workspace] roots once at least one configured root exists on disk.

[defaults]
# Change this to the environment you use most often, for example: local, dev, des, pre.
environment = \"des\"
"""


def ensure_bru_cli() -> None:
    if shutil.which("bru"):
        print("Bruno CLI `bru` is already installed.")
        return

    npm_command = shutil.which("npm")
    if not npm_command:
        raise RuntimeError(
            "Bruno CLI `bru` is not installed and `npm` was not found. Install Node.js/npm or install Bruno CLI manually."
        )

    print("Bruno CLI `bru` was not found. Installing @usebruno/cli with npm...")
    subprocess.run([npm_command, "install", "-g", f"@usebruno/cli@{DEFAULT_BRU_CLI_VERSION}"], check=True)

    if not shutil.which("bru"):
        raise RuntimeError("npm installation completed, but `bru` is still not available in PATH.")

    print("Bruno CLI `bru` installed successfully.")


def ensure_input(config: dict[str, Any], input_id: str, description: str) -> None:
    inputs = config.setdefault("inputs", [])
    if not isinstance(inputs, list):
        raise ValueError("Expected 'inputs' to be a JSON array")

    if any(isinstance(item, dict) and item.get("id") == input_id for item in inputs):
        return

    inputs.append(
        {
            "id": input_id,
            "type": "promptString",
            "description": description,
            "password": True,
        }
    )


def copy_file(source: Path, target: Path) -> None:
    shutil.copyfile(source, target)


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")

    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")


if __name__ == "__main__":
    main()
