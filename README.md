# 🔌 Bruno MCP Server for Python

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/Model_Context_Protocol-Enabled-blue?style=for-the-badge)](https://modelcontextprotocol.io/)
[![Bruno](https://img.shields.io/badge/Bruno-API_Client-orange?style=for-the-badge)](https://www.usebruno.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

</div>

> Python MCP server for running [Bruno](https://www.usebruno.com/) collections. It exposes a Model Context Protocol server over `stdio` with tools that run collections through the `bru` CLI and return normalized JSON results.

*Note: All examples in this repository use placeholder names (`project1`, `/home/user/project/bruno`, `example.test`). Replace them with your own paths and collection names.*

---

## ✨ Features

* **Run Bruno collections** natively using the Bruno CLI.
* **Discover collections** and sibling environment files automatically.
* **Support environment files** and dynamic environment variables.
* **Secure Secret Injection:** Pass secrets to Bruno without exposing values to the LLM via `inherited_variables`. Secret values are injected through a temporary, owner-only `--env-file` and the child process environment — they never appear in CLI arguments (`ps` output) or logs.
* **Filter Inspection:** Inspect documented query filters and run temporary filter scenarios without modifying the source collection.
* **Two-Phase Full Validation:** Execute baseline tests + all documented filters in a single tool call.
* **Normalized Outputs:** Return structured execution results containing `success`, `summary`, `failures`, and `timings`.

---

## 📦 Requirements

* **Python:** 3.10 or newer
* **Package Manager:** `uv`
* **Node Package Manager:** `npm` *(only if the Bruno CLI is not already installed)*

The installer checks whether the Bruno CLI command `bru` is available. If it is missing, the server fails with an explicit error instead of silently installing packages. To install the pinned version manually:

```bash
npm install -g @usebruno/cli@4.2.0
```

Runtime auto-install is available but opt-in: set `BRUNO_MCP_AUTO_INSTALL_BRU=1` (or `auto_install = true` under `[bruno]` in the config file) and the server installs exactly the pinned version from `cli_version` / `BRUNO_MCP_BRU_VERSION`.

---

## 🚀 Installation & Running

### 1. Installation
Install dependencies using `uv`:

```bash
uv sync
```
*(If your configured package index does not mirror the MCP Python SDK, point uv at PyPI for the sync: `UV_DEFAULT_INDEX=https://pypi.org/simple uv sync`)*

### 2. Running the Server
You can run the server directly using `uv`:

```bash
uv run bruno-mcp
```
*Alternatively, run the module directly inside the uv environment: `uv run python -m bruno_mcp`*

---

## ⚙️ Configuration

### MCP Configuration
Example MCP `stdio` configuration from this workspace root:

```json
{
  "mcpServers": {
    "bruno-runner": {
      "command": "uv",
      "args": ["run", "bruno-mcp"]
    }
  }
}
```

### Configuration File (`bruno-mcp.toml`)
Default roots and auth aliases can be configured in `bruno-mcp.toml` in the current working directory, or globally in `~/.config/bruno-mcp/config.toml`. 

See `bruno-mcp.example.toml` for a commented template:

```toml
[workspace]
roots = [
  "/home/user/project/bruno"
]

[bruno]
cli_version = "4.2.0"   # pinned Bruno CLI version
auto_install = false     # never install bru silently at runtime

[limits]
run_timeout_seconds = 300
max_output_bytes = 8388608
max_concurrent_runs = 2

[artifacts]
ttl_hours = 24           # raw reports are auto-deleted after this
max_files = 50

[security]
enforce_root_confinement = true   # reject collection paths outside workspace roots

[auth]
inherited_variables = [
  "BRUNO_AUTH_TOKEN",
  "BRUNO_API_KEY"
]

[defaults]
environment = "dev"
```

Every setting can also be set via environment variables (`BRUNO_MCP_BRU_VERSION`, `BRUNO_MCP_AUTO_INSTALL_BRU`, `BRUNO_MCP_RUN_TIMEOUT`, `BRUNO_MCP_MAX_OUTPUT_BYTES`, `BRUNO_MCP_MAX_CONCURRENT_RUNS`, `BRUNO_MCP_ARTIFACTS_DIR`, `BRUNO_MCP_ARTIFACT_TTL_HOURS`, `BRUNO_MCP_ARTIFACT_MAX_FILES`, `BRUNO_MCP_ENFORCE_ROOT_CONFINEMENT`, `BRUNO_MCP_LOG_LEVEL`), which take precedence over the file.

> **Note:** The installer creates `~/.config/bruno-mcp/config.toml` with a dummy root. Local `bruno-mcp.toml` files are git-ignored so real paths and environment names are never committed.

---

## 💻 Local VS Code Installation

From the project root, install dependencies with uv:

```bash
UV_DEFAULT_INDEX=[https://pypi.org/simple](https://pypi.org/simple) uv sync
```

Ensure the Bruno CLI is available (`bru --version`). This repository includes `.vscode/mcp.json`, allowing VS Code to discover the local MCP server from the workspace:

```json
{
  "servers": {
    "bruno-runner": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "bruno-mcp"]
    }
  }
}
```

Reload the VS Code window after syncing dependencies. The `bruno-runner` server should now be available in the MCP servers list.

### Global Installation
To install the MCP server in the VS Code user profile so it is available from **any** workspace:

```bash
uv run python scripts/install_vscode.py
```
*(To also install the reusable Copilot prompt and agent globally, append `--with-copilot-customizations` to the command above).*

To install it manually in another local VS Code workspace, change the command args to include `--directory`:
```json
"args": ["--directory", "/home/user/mcp-bruno", "run", "bruno-mcp"]
```

---

## 🧰 Available Tools

### 🔍 `list-collections`
Lists Bruno collections below a root directory or configured roots. Use it when the user provides a partial collection name instead of a full path.
* **`root`** *(optional)*: Bruno root directory (usually contains `collections/` and `environments/`).
* **`query`** *(optional)*: Case-insensitive text used to filter collection names and paths.

### ▶️ `run-collection`
Runs a Bruno collection and returns normalized execution results.
* **`collection`** *(required)*: Path to the Bruno collection.
* **`environment`** *(optional)*: Path to an environment file.
* **`variables`** *(optional)*: Environment variables as `KEY=value` strings.
* **`inherited_variables`** *(optional)*: Names of environment variables to read from the MCP server process and inject into Bruno without exposing values to the LLM. Values travel via a temporary owner-only `--env-file` and the child process environment (`{{process.env.NAME}}` also works) — never in CLI arguments.

<details>
<summary><b>View detailed authentication & path behavior</b></summary>

**Auth Handling:**
For secrets, prefer `inherited_variables` instead of writing values in chat. By default, the secure MCP input `BRUNO_AUTH_TOKEN` can satisfy Bruno variables named `bearerToken`, `BEARER_TOKEN`, `AUTH_TOKEN`, `TOKEN`, `accessToken`, or `access_token`. Secrets are injected via a temporary private `--env-file`; if the selected `--env` environment file declares the same variable (which would take precedence inside bru), the run transparently switches to a temporary sanitized copy of the collection with the conflicting entry removed, so the injected secret always wins and source files are never modified.

**Workspace confinement:**
When `[workspace] roots` are configured (and at least one exists on disk), `collection` paths outside those roots are rejected with a clear error. Set `enforce_root_confinement = false` to disable.

**Execution limits:**
Each bru run has a configurable timeout (`run_timeout_seconds`, default 300s), bounded stdout/stderr capture (`max_output_bytes`), and a concurrency cap (`max_concurrent_runs`). Raw JSON reports are stored as artifacts with owner-only (`0600`) permissions and expire automatically (`ttl_hours` / `max_files`). Structured logs go to stderr (`BRUNO_MCP_LOG_LEVEL`).

**Supported Collection Inputs:**
* Collection directory: `/path/to/bruno/collections/project1`
* Bruno request file: `/path/to/bruno/collections/project1/request.bru`
* Internal `.vru` request file: `/path/to/bruno/collections/project1/request.vru`
* Open collection descriptor: `/path/to/bruno/collections/project1/opencollection.yml`

When Bruno failures look like authentication problems, the response includes `auth_failure: true` and an `auth_message`. 

Example with inherited secrets:
```json
{
  "collection": "/home/user/project/bruno/collections/project1",
  "environment": "dev",
  "inherited_variables": ["BRUNO_AUTH_TOKEN"]
}
```
</details>

### 🌍 `discover-environments`
Inspects the folder structure around a Bruno collection and returns the sibling environments directory, available environment names, and variable names (without returning secret values).

### 📖 `read-result-artifact`
Reads a bounded, redacted summary from the raw Bruno JSON artifact path returned by `run-collection`. 
* **`path`** *(required)*: The `artifact.path` value returned by a previous run.
* **`max_items`** *(optional)*: Number of response items to sample per request (default 3, max 20).

### 🧪 `list-request-filters` & `run-filter-scenarios`
* `list-request-filters`: Inspects YAML request files and returns query params split into enabled and disabled groups.
* `run-filter-scenarios`: Runs temporary request variants with selected disabled query params enabled without modifying the source files.

### 🛡️ `run-full-validation`
Two-phase orchestration in a single tool call:
1. **Baseline**: Runs the collection once and checks every endpoint responds without errors (no `4xx`/`5xx`).
2. **Filters**: Only runs if the baseline is green. Automatically discovers and tests every disabled query filter across every endpoint.

---

## Agent Skills & Copilot Integration

These workflows use the MCP server configured as `bruno-runner`; installing a skill or prompt does not install or configure the server. Configure the server in your client first using the MCP configuration above.

### VS Code GitHub Copilot

The repository includes a [custom agent](copilot/agents/bruno-runner.agent.md) and a [slash-command prompt](copilot/prompts/run-bruno-collection.prompt.md). From this repository root, copy them into the target workspace:

```bash
mkdir -p /path/to/workspace/.github/agents /path/to/workspace/.github/prompts
cp copilot/agents/bruno-runner.agent.md /path/to/workspace/.github/agents/
cp copilot/prompts/run-bruno-collection.prompt.md /path/to/workspace/.github/prompts/
```

Select the `bruno-runner` custom agent in Copilot Chat's agent picker (`@bruno-runner` where supported), or run `/run-bruno-collection <collection or path> [environment]` in Chat. The slash command works independently of selecting the custom agent.

Alternatively, install both globally with:

```bash
uv run python scripts/install_vscode.py --with-copilot-customizations
```

### Claude Code / Cursor / Windsurf

The universal [Bruno Runner skill](skills/bruno-runner/SKILL.md) consolidates collection discovery, environment selection, standard runs, targeted filter scenarios, full two-phase validation, secret safety, and reporting rules.

For Claude Code, copy it into the target project's skill directory:

```bash
mkdir -p /path/to/workspace/.claude/skills/bruno-runner
cp skills/bruno-runner/SKILL.md /path/to/workspace/.claude/skills/bruno-runner/SKILL.md
```

Or symlink the skill directory using an absolute source path:

```bash
mkdir -p /path/to/workspace/.claude/skills
ln -s /absolute/path/to/mcp-bruno/skills/bruno-runner /path/to/workspace/.claude/skills/bruno-runner
```

For Cursor, copy or symlink the same directory to `.cursor/skills/bruno-runner/`; for Windsurf, use `.windsurf/skills/bruno-runner/` in the target workspace. Skill discovery depends on your client's version and configuration. For clients using rules instead, adapt the skill body into their native rule format (for example, a `.cursor/rules/bruno-runner.mdc` file with Cursor rule frontmatter); merely copying `SKILL.md` into a rules directory does not guarantee activation.

Ask to run a collection, validate a specific filter, or test all endpoints and filters. The workflows resolve secrets locally through `inherited_variables`; never paste credentials into chat. The server's local token fallback is `~/.config/bruno-mcp/.bearer_token` (raw token), not `~/.config/mcp-bruno/.bearer_token`. After updating it, restart the MCP server.

### Repository Assets

Skills and Copilot templates remain organized under `skills/` and `copilot/` at the repository root, separately from the Python packages discovered under `src/` in `pyproject.toml`. They are repository assets, not installed wheel resources; use a repository checkout for the copy, symlink, and installer commands above. Installing the Python package alone does not register agent workflows with a client.

Execution results retain the server's normalized output contract:

```json
{
  "success": true,
  "summary": {
    "total": 5,
    "failed": 0,
    "passed": 5
  },
  "failures": [],
  "auth_failure": false,
  "auth_message": null,
  "timings": {
    "started": "2024-03-14T10:00:00.000000Z",
    "completed": "2024-03-14T10:00:01.000000Z",
    "duration": 1000
  }
}
```

---

## 🐳 Docker

The Docker image installs both this Python server and the Bruno CLI:

```bash
docker build -t bruno-mcp-python .
docker run --rm -i bruno-mcp-python
```

---

## 🛠️ Development & Project Structure

**Commands:**
* Compile-check the sources: `uv run python -m compileall src`
* Run the test suite: `uv run python -m unittest discover -s tests -v`

**Structure:**
```text
.
├── src/bruno_mcp/         # MCP server, runner, config, and types
├── scripts/               # VS Code installer script
├── copilot/               # Reusable Copilot prompt and agent templates
├── skills/bruno-runner/    # Universal Agent Skill (SKILL.md)
├── tests/                 # Unit tests
├── .vscode/mcp.json       # Workspace MCP server entry
└── bruno-mcp.example.toml # Commented configuration template
```
---
mcp-name: io.github.kta41/mcp-bruno
---



## 📄 License
Released under the [MIT License](LICENSE).


