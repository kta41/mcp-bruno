from __future__ import annotations

import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from bruno_mcp.runner import BrunoRunner
from bruno_mcp.types import (
    DiscoverEnvironmentsParams,
    ListCollectionsParams,
    ListRequestFiltersParams,
    ReadRunArtifactParams,
    RunCollectionParams,
    RunFilterScenariosParams,
    RunFullValidationParams,
)


def create_server() -> Server:
    runner = BrunoRunner()

    def json_text(result: Any) -> str:
        return json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False, separators=(",", ":"))

    async def list_tools(_context: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="run-collection",
                    title="Run Bruno collection",
                    description=(
                        "Run a Bruno collection with the local `bru` CLI and return normalized JSON "
                        "with success, request summary, per-request details, failures, and execution timings. Provide "
                        "`collection` as the collection path; optionally pass `environment` and "
                        "non-secret `variables` as KEY=value strings. For secrets, pass only names "
                        "through `inherited_variables`; values are read from the MCP server process and "
                        "injected via a temporary private --env-file, never via CLI arguments. "
                        "Collection paths must stay inside the configured workspace roots."
                    ),
                    input_schema=RunCollectionParams.model_json_schema(),
                ),
                types.Tool(
                    name="list-collections",
                    title="List Bruno collections",
                    description=(
                        "List Bruno collections below a root directory or configured roots. Use this "
                        "when the user gives a partial collection name instead of a full path."
                    ),
                    input_schema=ListCollectionsParams.model_json_schema(),
                ),
                types.Tool(
                    name="discover-environments",
                    title="Discover Bruno environments",
                    description=(
                        "Inspect the folder structure around a Bruno collection and return the sibling "
                        "environments directory, available environment names, and variable names. "
                        "Secret values are not returned."
                    ),
                    input_schema=DiscoverEnvironmentsParams.model_json_schema(),
                ),
                types.Tool(
                    name="list-request-filters",
                    title="List Bruno request filters",
                    description=(
                        "Inspect OpenCollection YAML requests and return enabled and disabled query params "
                        "that can be used as filter scenarios. Secret values are not returned."
                    ),
                    input_schema=ListRequestFiltersParams.model_json_schema(),
                ),
                types.Tool(
                    name="run-filter-scenarios",
                    title="Run Bruno filter scenarios",
                    description=(
                        "Run temporary Bruno request variants with selected disabled query params enabled, "
                        "then validate HTTP status and response payload consistency. Each request is also run "
                        "once without filters (baseline) to distinguish an ignored filter (response identical "
                        "to baseline) from a filter that legitimately returns zero matches. Infrastructure "
                        "failures (auth, routing, timeout, connectivity) are reported as inconclusive, never "
                        "as filter defects. Source collection files are not modified."
                    ),
                    input_schema=RunFilterScenariosParams.model_json_schema(),
                ),
                types.Tool(
                    name="read-result-artifact",
                    title="Read Bruno result artifact",
                    description=(
                        "Read a bounded, redacted summary from a raw Bruno JSON artifact path returned by "
                        "run-collection or run-filter-scenarios. Use this when response data is too large "
                        "to include directly in a tool result."
                    ),
                    input_schema=ReadRunArtifactParams.model_json_schema(),
                ),
                types.Tool(
                    name="run-full-validation",
                    title="Run full Bruno validation (endpoints then filters)",
                    description=(
                        "Two-phase validation. Phase 1 runs the collection once and checks every endpoint "
                        "is reachable (HTTP status, no request errors). Phase 2 only runs if phase 1 passes: "
                        "it tests every documented disabled query filter across every endpoint in temporary "
                        "request copies, without modifying source files. Each scenario is compared against an "
                        "unfiltered baseline to distinguish ignored filters from legitimate zero-match results, "
                        "and infrastructure failures are marked inconclusive instead of failed. Returns a "
                        "consolidated result stating which phase ran, per-endpoint baseline status, and "
                        "per-endpoint per-filter pass/fail/skip with the reason."
                    ),
                    input_schema=RunFullValidationParams.model_json_schema(),
                )
            ]
        )

    async def call_tool(_context: Any, params: Any) -> types.CallToolResult:
        result: Any
        if params.name == "list-collections":
            list_params = ListCollectionsParams(**(params.arguments or {}))
            result = await runner.list_collections(list_params)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json_text(result),
                    )
                ]
            )

        if params.name == "discover-environments":
            discover_params = DiscoverEnvironmentsParams(**(params.arguments or {}))
            result = await runner.discover_environments(discover_params)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json_text(result),
                    )
                ]
            )

        if params.name == "list-request-filters":
            filter_params = ListRequestFiltersParams(**(params.arguments or {}))
            result = await runner.list_request_filters(filter_params)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json_text(result),
                    )
                ]
            )

        if params.name == "run-filter-scenarios":
            scenario_params = RunFilterScenariosParams(**(params.arguments or {}))
            result = await runner.run_filter_scenarios(scenario_params)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json_text(result),
                    )
                ]
            )

        if params.name == "read-result-artifact":
            artifact_params = ReadRunArtifactParams(**(params.arguments or {}))
            result = runner.read_run_artifact(artifact_params)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json_text(result),
                    )
                ]
            )

        if params.name == "run-full-validation":
            full_validation_params = RunFullValidationParams(**(params.arguments or {}))
            result = await runner.run_full_validation(full_validation_params)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json_text(result),
                    )
                ]
            )

        if params.name != "run-collection":
            raise ValueError(f"Unknown tool: {params.name}")

        run_params = RunCollectionParams(**(params.arguments or {}))
        result = await runner.run_collection(run_params)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json_text(result),
                )
            ]
        )

    return Server(
        name="bruno-runner",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
