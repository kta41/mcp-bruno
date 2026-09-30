from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

# Input hardening: LLM-provided arguments are bounded so malformed or hostile
# inputs are rejected before reaching the filesystem or the bru CLI.
PathString = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
ShortString = Annotated[str, StringConstraints(min_length=1, max_length=256)]
VariableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")]
EnvVarAssignment = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}=.{0,2048}$")]
FilterValue = Annotated[str, StringConstraints(max_length=2048)]


class RunCollectionParams(BaseModel):
    collection: PathString = Field(
        description=(
            "Path to the Bruno collection directory, a .bru/.vru request file, or an opencollection.yml file. "
            "When opencollection.yml is provided, the server runs its parent collection directory."
        ),
        examples=["/home/user/api-tests/bruno/collections/example/opencollection.yml"],
    )
    environment: ShortString | None = Field(
        default=None,
        description="Optional Bruno environment name or environment file accepted by `bru run --env`.",
        examples=["dev"],
    )
    variables: list[EnvVarAssignment] | None = Field(
        default=None,
        max_length=64,
        description="Optional Bruno environment variables passed as repeated `--env-var` values. Use KEY=value strings.",
        examples=[["BASE_URL=https://api.example.com", "TOKEN=redacted"]],
    )
    inherited_variables: list[VariableName] | None = Field(
        default=None,
        max_length=32,
        description=(
            "Optional names of environment variables to read from the MCP server process and inject into Bruno "
            "via a temporary private --env-file (values never appear in CLI arguments or logs). "
            "Use this for secrets so the LLM only provides variable names, never values."
        ),
        examples=[["BRUNO_AUTH_TOKEN", "BRUNO_API_KEY"]],
    )


class DiscoverEnvironmentsParams(BaseModel):
    collection: PathString = Field(
        description=(
            "Path to a Bruno collection under a bruno/collections-style tree. "
            "The server will look for a sibling environments directory."
        ),
        examples=["/home/user/project/bruno/collections/project1"],
    )


class ListCollectionsParams(BaseModel):
    root: PathString | None = Field(
        default=None,
        description=(
            "Optional Bruno root directory. When omitted, the server uses roots from bruno-mcp.toml "
            "or ~/.config/bruno-mcp/config.toml."
        ),
        examples=["/home/user/project/bruno"],
    )
    query: ShortString | None = Field(
        default=None,
        description="Optional case-insensitive text used to filter collection names and paths.",
        examples=["project1"],
    )


class ListRequestFiltersParams(BaseModel):
    collection: PathString = Field(
        description="Path to the Bruno collection directory or opencollection.yml file to inspect for query filters.",
        examples=["/home/user/project/bruno/collections/project1"],
    )


class FilterScenario(BaseModel):
    name: ShortString = Field(description="Human-readable scenario name.")
    request: PathString = Field(description="Request YAML path relative to the collection root.")
    query_params: dict[VariableName, FilterValue] = Field(
        max_length=32,
        description="Query params to enable or override for this scenario.",
    )


class RunFilterScenariosParams(BaseModel):
    collection: PathString = Field(description="Path to the Bruno collection directory or opencollection.yml file.")
    environment: ShortString | None = Field(default=None, description="Optional Bruno environment name.")
    variables: list[EnvVarAssignment] | None = Field(default=None, max_length=64, description="Optional non-secret KEY=value variables.")
    inherited_variables: list[VariableName] | None = Field(
        default=None,
        max_length=32,
        description="Optional secret variable names to inherit from the MCP server process.",
    )
    scenarios: list[FilterScenario] | None = Field(
        default=None,
        max_length=100,
        description="Optional explicit filter scenarios. When omitted, scenarios are generated from disabled query params.",
    )
    max_scenarios: int = Field(
        default=8,
        ge=1,
        le=100,
        description="Maximum number of generated scenarios when scenarios are omitted.",
    )


class ReadRunArtifactParams(BaseModel):
    path: PathString = Field(description="Path returned by run-collection or run-filter-scenarios as artifact_path.")
    max_items: int = Field(default=3, ge=1, le=20, description="Maximum response items to sample per request.")


class RunFullValidationParams(BaseModel):
    collection: PathString = Field(description="Path to the Bruno collection directory or opencollection.yml file.")
    environment: ShortString | None = Field(default=None, description="Optional Bruno environment name.")
    variables: list[EnvVarAssignment] | None = Field(default=None, max_length=64, description="Optional non-secret KEY=value variables.")
    inherited_variables: list[VariableName] | None = Field(
        default=None,
        max_length=32,
        description="Optional secret variable names to inherit from the MCP server process.",
    )
    max_scenarios: int = Field(
        default=500,
        ge=1,
        le=500,
        description="Maximum number of filter scenarios to run in phase 2, across all endpoints combined.",
    )


class EnvironmentInfo(BaseModel):
    name: str
    path: str
    variables: list[str]


class DiscoverEnvironmentsResult(BaseModel):
    collection: str
    environments_dir: str | None
    environments: list[EnvironmentInfo]


class CollectionInfo(BaseModel):
    name: str
    path: str
    root: str


class ListCollectionsResult(BaseModel):
    roots: list[str]
    collections: list[CollectionInfo]


class QueryParamInfo(BaseModel):
    name: str
    value: str | None = None
    description: str | None = None
    disabled: bool = False


class RequestFilterInfo(BaseModel):
    name: str
    path: str
    method: str | None = None
    url: str | None = None
    enabled_query_params: list[QueryParamInfo]
    disabled_query_params: list[QueryParamInfo]


class ListRequestFiltersResult(BaseModel):
    collection: str
    requests: list[RequestFilterInfo]


class Summary(BaseModel):
    total: int
    failed: int
    passed: int
    inconclusive: int = 0


class Failure(BaseModel):
    name: str
    message: str
    auth_failure: bool = False


class RequestDetail(BaseModel):
    name: str
    path: str | None = None
    method: str | None = None
    url: str | None = None
    status: str | int | None = None
    status_text: str | None = None
    response_time: int | float | None = None
    response_data_type: str | None = None
    response_item_count: int | None = None
    response_body_preview: Any = None
    error: str | None = None
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    assertions_total: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0


class Timings(BaseModel):
    started: str
    completed: str
    duration: int


class InheritedVariableDiagnostic(BaseModel):
    requested_name: str
    resolved_name: str
    resolved: bool
    empty: bool
    source: str | None = None
    length: int | None = None
    sha256_prefix: str | None = None


class RunDiagnostics(BaseModel):
    cwd: str | None = None
    bru_args: list[str] = []
    inherited_variables: list[InheritedVariableDiagnostic] = []
    note: str | None = None


class ArtifactInfo(BaseModel):
    path: str
    description: str


class ResponseDataSummary(BaseModel):
    data_type: str | None = None
    item_count: int | None = None
    top_level_keys: list[str] = []
    item_keys: list[str] = []
    sample_items: list[Any] = []


class ArtifactRequestSummary(BaseModel):
    name: str
    path: str | None = None
    method: str | None = None
    url: str | None = None
    status: str | int | None = None
    status_text: str | None = None
    response_time: int | float | None = None
    response_data: ResponseDataSummary


class ReadRunArtifactResult(BaseModel):
    artifact: ArtifactInfo
    requests: list[ArtifactRequestSummary]


class BrunoRunResult(BaseModel):
    success: bool
    summary: Summary
    requests: list[RequestDetail]
    failures: list[Failure]
    artifact: ArtifactInfo | None = None
    diagnostics: RunDiagnostics | None = None
    auth_failure: bool = False
    auth_message: str | None = None
    timings: Timings


class ValidationCheck(BaseModel):
    name: str
    status: Literal["passed", "failed", "skipped"]
    message: str


class FilterScenarioResult(BaseModel):
    scenario: FilterScenario
    request: RequestDetail | None = None
    failure_layer: str | None = None
    validation_checks: list[ValidationCheck]


class RunFilterScenariosResult(BaseModel):
    success: bool
    scenarios: list[FilterScenarioResult]
    summary: Summary
    failure_layers: dict[str, int] = {}
    artifact: ArtifactInfo | None = None
    diagnostics: RunDiagnostics | None = None
    auth_failure: bool = False
    auth_message: str | None = None
    timings: Timings


class EndpointStatus(BaseModel):
    name: str
    path: str | None = None
    status: Literal["passed", "failed"]
    message: str | None = None


class FilterFinding(BaseModel):
    endpoint_name: str
    endpoint_path: str
    filter_params: dict[str, str]
    status: Literal["passed", "failed", "skipped"]
    message: str


class FilterSummary(BaseModel):
    total: int
    passed: int
    failed: int
    skipped: int


class RunFullValidationResult(BaseModel):
    phase: Literal["baseline_failed", "completed"]
    baseline_summary: Summary
    endpoints: list[EndpointStatus]
    filter_summary: FilterSummary | None = None
    filters: list[FilterFinding] = []
    artifact: ArtifactInfo | None = None
    filters_artifact: ArtifactInfo | None = None
    auth_failure: bool = False
    auth_message: str | None = None
    timings: Timings
