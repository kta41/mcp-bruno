"""MCP-facing orchestration for running Bruno collections.

Heavy lifting lives in focused modules:
- execution.py: bru CLI discovery, version pinning, timeouts, output caps, concurrency.
- artifacts.py: confined, permission-hardened artifact storage with retention.
- filters.py: filter scenario parsing, request rewriting, and validation checks.
- redaction.py: secret masking and bounded previews.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from bruno_mcp import artifacts
from bruno_mcp.config import load_auth_variable_aliases, load_bruno_roots
from bruno_mcp.execution import BruExecutor, secret_env_file
from bruno_mcp.filters import (
    apply_query_param_overrides,
    build_filter_scenarios,
    build_request_detail,
    build_validation_checks,
    classify_failure_layer,
    first_yaml_value,
    is_auth_failure,
    is_failed_request,
    parse_query_params,
    sanitize_yaml_for_bru_cli,
)
from bruno_mcp.redaction import mask_bru_args, sha_prefix
from bruno_mcp.responses import extract_response_items
from bruno_mcp.settings import Settings, load_settings
from bruno_mcp.types import (
    ArtifactInfo,
    BrunoRunResult,
    CollectionInfo,
    DiscoverEnvironmentsParams,
    DiscoverEnvironmentsResult,
    EndpointStatus,
    EnvironmentInfo,
    Failure,
    FilterFinding,
    FilterScenario,
    FilterScenarioResult,
    FilterSummary,
    InheritedVariableDiagnostic,
    ListCollectionsParams,
    ListCollectionsResult,
    ListRequestFiltersParams,
    ListRequestFiltersResult,
    ReadRunArtifactParams,
    ReadRunArtifactResult,
    RequestFilterInfo,
    RunCollectionParams,
    RunDiagnostics,
    RunFilterScenariosParams,
    RunFilterScenariosResult,
    RunFullValidationParams,
    RunFullValidationResult,
    Summary,
    Timings,
    ValidationCheck,
)
from bruno_mcp.utils import report_file

logger = logging.getLogger("bruno_mcp.runner")

REQUEST_SUMMARY_REGEX = re.compile(r"Requests:\s+(\d+)\s+passed,\s+(\d+)\s+failed,\s+(\d+)\s+total")
VARIABLE_NAME_REGEX = re.compile(r"^[\s\"']*([A-Za-z_][A-Za-z0-9_\-.]*)[\s\"']*[:=]", re.MULTILINE)
YAML_PARSE_ERROR_REGEX = re.compile(r"YAMLParseError|Error parsing item", re.IGNORECASE)
ENV_FILE_SUFFIXES = (".json", ".bru", ".yml", ".yaml")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timings(start_time: datetime, completed_time: datetime) -> Timings:
    return Timings(
        started=start_time.isoformat().replace("+00:00", "Z"),
        completed=completed_time.isoformat().replace("+00:00", "Z"),
        duration=int((completed_time - start_time).total_seconds() * 1000),
    )


class BrunoRunner:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or load_settings()
        self._executor = BruExecutor(self._settings)

    # ------------------------------------------------------------------
    # MCP tools
    # ------------------------------------------------------------------

    async def list_collections(self, params: ListCollectionsParams) -> ListCollectionsResult:
        roots = [Path(params.root).expanduser()] if params.root else load_bruno_roots()
        query = params.query.lower() if params.query else None

        collections: list[CollectionInfo] = []
        for root in roots:
            collections_dir = root / "collections" if root.name != "collections" else root
            if not collections_dir.is_dir():
                continue

            for collection in sorted(path for path in collections_dir.iterdir() if path.is_dir()):
                if collection.name.startswith("."):
                    continue
                if query and query not in collection.name.lower() and query not in str(collection).lower():
                    continue
                collections.append(
                    CollectionInfo(
                        name=collection.name,
                        path=str(collection.resolve()),
                        root=str(root.resolve()),
                    )
                )

        return ListCollectionsResult(
            roots=[str(root.expanduser()) for root in roots],
            collections=collections,
        )

    async def discover_environments(self, params: DiscoverEnvironmentsParams) -> DiscoverEnvironmentsResult:
        collection_path = Path(params.collection).expanduser().resolve()
        self._check_root_confinement(collection_path)
        environments_dir = self._find_environments_dir(collection_path)

        if environments_dir is None:
            return DiscoverEnvironmentsResult(
                collection=str(collection_path),
                environments_dir=None,
                environments=[],
            )

        environments = []
        for environment_file in sorted(path for path in environments_dir.iterdir() if path.is_file()):
            if environment_file.name.startswith("."):
                continue

            variables = self._extract_variable_names(environment_file)
            environments.append(
                EnvironmentInfo(
                    name=environment_file.stem,
                    path=str(environment_file),
                    variables=variables,
                )
            )

        return DiscoverEnvironmentsResult(
            collection=str(collection_path),
            environments_dir=str(environments_dir),
            environments=environments,
        )

    async def list_request_filters(self, params: ListRequestFiltersParams) -> ListRequestFiltersResult:
        collection_dir, _collection_target = self._resolve_collection_target(Path(params.collection))
        collection_dir = collection_dir.resolve()
        return ListRequestFiltersResult(
            collection=str(collection_dir),
            requests=self._list_request_filter_info(collection_dir),
        )

    async def run_collection(self, params: RunCollectionParams) -> BrunoRunResult:
        start_time = _utc_now()
        collection_dir, collection_target = self._resolve_collection_target(Path(params.collection))
        bru_command = await self._executor.ensure_bru_cli()
        secrets, secret_diagnostics = self._resolve_secret_variables(params.inherited_variables)

        run_dir = self._prepare_run_dir(collection_dir, params, secrets)
        logger.info("run_collection collection=%s environment=%s secrets=%d", collection_dir, params.environment, len(secrets))
        try:
            async with report_file("bruno-run-", ".json") as output_file:
                args, diagnostics = self._prepare_bru_invocation(
                    bru_command, collection_target, params, output_file, cwd=run_dir,
                    secret_diagnostics=secret_diagnostics,
                )

                with self._secret_env_context(secrets) as secret_args:
                    full_args = [*args, *secret_args]
                    diagnostics.bru_args = mask_bru_args(full_args)
                    returncode, stdout, stderr = await self._executor.run(full_args, run_dir, env=self._bru_env(secrets))

                    if returncode != 0 and YAML_PARSE_ERROR_REGEX.search(stderr) and run_dir == collection_dir:
                        run_dir = self._sanitize_collection_for_cli(collection_dir)
                        self._neutralize_env_conflicts(run_dir, params, secrets)
                        returncode, stdout, stderr = await self._executor.run(
                            full_args, run_dir, env=self._bru_env(secrets)
                        )

                if returncode != 0 and not output_file.is_file() and not REQUEST_SUMMARY_REGEX.search(stdout):
                    raise RuntimeError(f"CLI stderr: {stderr or 'Unknown error'}")

                result_json = output_file.read_text(encoding="utf-8")
                artifact = self._write_artifact("bruno-run", result_json)
                completed_time = _utc_now()
                json_result = json.loads(result_json)

                first_result = json_result[0] if json_result else {}
                summary_data = first_result.get("summary", {})
                results = first_result.get("results", []) or []
                requests = [build_request_detail(result) for result in results]
                failed_requests = sum(1 for request in requests if is_failed_request(request))
                success = failed_requests == 0

                failures = [
                    Failure(
                        name=request.name,
                        message=request.error or f"HTTP {request.status} {request.status_text or ''}".strip(),
                        auth_failure=is_auth_failure(
                            " ".join(str(part) for part in [request.error, request.status, request.status_text] if part)
                        ),
                    )
                    for request in requests
                    if is_failed_request(request)
                ]
                auth_failure = any(failure.auth_failure for failure in failures)
                logger.info(
                    "run_collection_done success=%s total=%d failed=%d duration_ms=%d",
                    success,
                    len(requests),
                    failed_requests,
                    int((completed_time - start_time).total_seconds() * 1000),
                )

                return BrunoRunResult(
                    success=success,
                    summary=Summary(
                        total=summary_data.get("totalRequests") or 0,
                        failed=failed_requests,
                        passed=(summary_data.get("totalRequests") or 0) - failed_requests,
                    ),
                    requests=requests,
                    failures=failures,
                    artifact=artifact,
                    diagnostics=diagnostics,
                    auth_failure=auth_failure,
                    auth_message=(
                        "One or more requests returned HTTP 401/403 or an auth-related error. "
                        "If you already entered BRUNO_AUTH_TOKEN, it may be empty/expired/cached as empty by VS Code. "
                        "Open the VS Code Command Palette, run 'MCP: Reset Input', re-enter the token, "
                        "then restart the bruno-runner server."
                        if auth_failure
                        else None
                    ),
                    timings=_timings(start_time, completed_time),
                )
        finally:
            self._cleanup_sanitized_collections()

    async def run_filter_scenarios(self, params: RunFilterScenariosParams) -> RunFilterScenariosResult:
        start_time = _utc_now()
        collection_dir, _collection_target = self._resolve_collection_target(Path(params.collection))
        bru_command = await self._executor.ensure_bru_cli()
        request_filters = self._list_request_filter_info(collection_dir)
        scenarios = params.scenarios or build_filter_scenarios(request_filters, params.max_scenarios)

        logger.info("run_filter_scenarios collection=%s scenarios=%d", collection_dir, len(scenarios))
        try:
            scenario_results, artifact, scenario_diagnostics = await self._execute_scenarios(
                bru_command, collection_dir, scenarios, params
            )
        finally:
            self._cleanup_sanitized_collections()

        completed_time = _utc_now()
        failed = 0
        inconclusive = 0
        failure_layers: dict[str, int] = {}
        for result in scenario_results:
            if result.failure_layer is not None:
                inconclusive += 1
                failure_layers[result.failure_layer] = failure_layers.get(result.failure_layer, 0) + 1
            elif any(check.status == "failed" for check in result.validation_checks):
                failed += 1
        auth_failure = any(
            result.request is not None
            and is_auth_failure(
                " ".join(
                    str(part)
                    for part in [result.request.error, result.request.status, result.request.status_text]
                    if part
                )
            )
            for result in scenario_results
        )

        return RunFilterScenariosResult(
            success=failed == 0 and inconclusive == 0,
            scenarios=scenario_results,
            summary=Summary(
                total=len(scenario_results),
                failed=failed,
                passed=len(scenario_results) - failed - inconclusive,
                inconclusive=inconclusive,
            ),
            failure_layers=failure_layers,
            artifact=artifact,
            diagnostics=scenario_diagnostics,
            auth_failure=auth_failure,
            auth_message=(
                "One or more filter scenarios returned HTTP 401/403 or an auth-related error. "
                "If you already entered BRUNO_AUTH_TOKEN, it may be empty/expired/cached as empty by VS Code. "
                "Open the VS Code Command Palette, run 'MCP: Reset Input', re-enter the token, then restart the bruno-runner server."
                if auth_failure
                else None
            ),
            timings=_timings(start_time, completed_time),
        )

    async def run_full_validation(self, params: RunFullValidationParams) -> RunFullValidationResult:
        start_time = _utc_now()

        baseline = await self.run_collection(
            RunCollectionParams(
                collection=params.collection,
                environment=params.environment,
                variables=params.variables,
                inherited_variables=params.inherited_variables,
            )
        )

        endpoints = [
            EndpointStatus(
                name=request.name,
                path=request.path,
                status="failed" if is_failed_request(request) else "passed",
                message=(
                    request.error or f"HTTP {request.status} {request.status_text or ''}".strip()
                    if is_failed_request(request)
                    else None
                ),
            )
            for request in baseline.requests
        ]

        if not baseline.success:
            return RunFullValidationResult(
                phase="baseline_failed",
                baseline_summary=baseline.summary,
                endpoints=endpoints,
                artifact=baseline.artifact,
                auth_failure=baseline.auth_failure,
                auth_message=(
                    baseline.auth_message
                    or "Filter validation (phase 2) was skipped because one or more baseline endpoint checks failed."
                ),
                timings=baseline.timings,
            )

        bru_command = await self._executor.ensure_bru_cli()
        collection_dir, _collection_target = self._resolve_collection_target(Path(params.collection))
        request_filters = self._list_request_filter_info(collection_dir)
        scenarios = build_filter_scenarios(request_filters, params.max_scenarios)

        scenario_params = RunFilterScenariosParams(
            collection=params.collection,
            environment=params.environment,
            variables=params.variables,
            inherited_variables=params.inherited_variables,
        )
        try:
            scenario_results, filters_artifact, _diagnostics = await self._execute_scenarios(
                bru_command, collection_dir, scenarios, scenario_params
            )
        finally:
            self._cleanup_sanitized_collections()

        request_filters_by_path = {request.path: request.name for request in request_filters}
        filters: list[FilterFinding] = []
        for result in scenario_results:
            failed_checks = [check for check in result.validation_checks if check.status == "failed"]
            filter_checks = [
                check
                for check in result.validation_checks
                if check.name.startswith("filter:") or check.name == "filter_impact"
            ]
            if result.failure_layer is not None:
                status: Literal["passed", "failed", "skipped"] = "skipped"
                message = f"Inconclusive: {result.failure_layer} failure - filter logic was not evaluated."
            elif failed_checks:
                status = "failed"
                message = "; ".join(check.message for check in failed_checks)
            elif filter_checks and all(check.status == "skipped" for check in filter_checks):
                status = "skipped"
                message = "; ".join(check.message for check in filter_checks)
            else:
                status = "passed"
                message = "; ".join(check.message for check in filter_checks) or "HTTP status passed."

            filters.append(
                FilterFinding(
                    endpoint_name=request_filters_by_path.get(result.scenario.request, result.scenario.request),
                    endpoint_path=result.scenario.request,
                    filter_params=result.scenario.query_params,
                    status=status,
                    message=message,
                )
            )

        failed_filters = sum(1 for finding in filters if finding.status == "failed")
        passed_filters = sum(1 for finding in filters if finding.status == "passed")
        skipped_filters = sum(1 for finding in filters if finding.status == "skipped")

        auth_failure = any(
            result.request is not None
            and is_auth_failure(
                " ".join(
                    str(part)
                    for part in [result.request.error, result.request.status, result.request.status_text]
                    if part
                )
            )
            for result in scenario_results
        )

        completed_time = _utc_now()
        return RunFullValidationResult(
            phase="completed",
            baseline_summary=baseline.summary,
            endpoints=endpoints,
            filter_summary=FilterSummary(
                total=len(filters), passed=passed_filters, failed=failed_filters, skipped=skipped_filters
            ),
            filters=filters,
            artifact=baseline.artifact,
            filters_artifact=filters_artifact,
            auth_failure=auth_failure,
            auth_message=(
                "One or more filter scenarios returned HTTP 401/403 or an auth-related error. "
                "Refresh BRUNO_AUTH_TOKEN via VS Code MCP secure input or ~/.config/bruno-mcp/.bearer_token, then rerun."
                if auth_failure
                else None
            ),
            timings=_timings(start_time, completed_time),
        )

    def read_run_artifact(self, params: ReadRunArtifactParams) -> ReadRunArtifactResult:
        return artifacts.read_run_artifact(params, self._settings)

    # ------------------------------------------------------------------
    # Scenario execution
    # ------------------------------------------------------------------

    async def _execute_scenarios(
        self,
        bru_command: str,
        collection_dir: Path,
        scenarios: list[FilterScenario],
        params: Any,
    ) -> tuple[list[FilterScenarioResult], ArtifactInfo, RunDiagnostics | None]:
        scenario_results: list[FilterScenarioResult] = []
        scenario_artifact_payloads: list[dict[str, Any]] = []
        scenario_diagnostics: RunDiagnostics | None = None
        baseline_items_by_request: dict[str, list[Any] | None] = {}
        secrets, secret_diagnostics = self._resolve_secret_variables(getattr(params, "inherited_variables", None))

        for scenario in scenarios:
            sanitized_collection_dir = self._sanitize_collection_for_cli(collection_dir)
            self._neutralize_env_conflicts(sanitized_collection_dir, params, secrets)
            request_file = sanitized_collection_dir / scenario.request
            if not request_file.is_file():
                scenario_results.append(
                    FilterScenarioResult(
                        scenario=scenario,
                        request=None,
                        validation_checks=[
                            ValidationCheck(
                                name="request_file",
                                status="failed",
                                message=f"Request file not found in temporary collection: {scenario.request}",
                            )
                        ],
                    )
                )
                continue

            if scenario.request not in baseline_items_by_request:
                baseline_items_by_request[scenario.request] = await self._run_request_baseline(
                    bru_command, collection_dir, scenario.request, params, secrets, secret_diagnostics
                )
            baseline_items = baseline_items_by_request[scenario.request]

            apply_query_param_overrides(request_file, scenario.query_params)
            async with report_file("bruno-filter-scenario-", ".json") as output_file:
                args, scenario_diagnostics = self._prepare_bru_invocation(
                    bru_command, scenario.request, params, output_file, cwd=sanitized_collection_dir,
                    secret_diagnostics=secret_diagnostics,
                )
                with self._secret_env_context(secrets) as secret_args:
                    full_args = [*args, *secret_args]
                    scenario_diagnostics.bru_args = mask_bru_args(full_args)
                    returncode, stdout, stderr = await self._executor.run(
                        full_args, sanitized_collection_dir, env=self._bru_env(secrets)
                    )

                if returncode != 0 and not output_file.is_file() and not REQUEST_SUMMARY_REGEX.search(stdout):
                    scenario_results.append(
                        FilterScenarioResult(
                            scenario=scenario,
                            request=None,
                            failure_layer="execution",
                            validation_checks=[
                                ValidationCheck(
                                    name="bru_execution",
                                    status="failed",
                                    message=stderr or "Bruno CLI did not produce a JSON report.",
                                )
                            ],
                        )
                    )
                    continue

                json_result = json.loads(output_file.read_text(encoding="utf-8"))
                scenario_artifact_payloads.append({"scenario": scenario.model_dump(), "report": json_result})
                first_result = json_result[0] if json_result else {}
                results = first_result.get("results", []) or []
                raw_result = results[0] if results else {}
                request = build_request_detail(raw_result) if raw_result else None
                scenario_results.append(
                    FilterScenarioResult(
                        scenario=scenario,
                        request=request,
                        failure_layer=classify_failure_layer(request),
                        validation_checks=build_validation_checks(raw_result, scenario.query_params, baseline_items),
                    )
                )

        artifact = self._write_artifact("bruno-filter-scenarios", json.dumps(scenario_artifact_payloads, ensure_ascii=False))
        return scenario_results, artifact, scenario_diagnostics

    async def _run_request_baseline(
        self,
        bru_command: str,
        collection_dir: Path,
        request_path: str,
        params: Any,
        secrets: dict[str, str],
        secret_diagnostics: list[InheritedVariableDiagnostic],
    ) -> list[Any] | None:
        """Run the request once without filter overrides and return its response items (None when unavailable)."""
        sanitized_collection_dir = self._sanitize_collection_for_cli(collection_dir)
        self._neutralize_env_conflicts(sanitized_collection_dir, params, secrets)
        request_file = sanitized_collection_dir / request_path
        if not request_file.is_file():
            return None
        async with report_file("bruno-filter-baseline-", ".json") as output_file:
            args, _diagnostics = self._prepare_bru_invocation(
                bru_command, request_path, params, output_file, cwd=sanitized_collection_dir,
                secret_diagnostics=secret_diagnostics,
            )
            with self._secret_env_context(secrets) as secret_args:
                await self._executor.run(
                    [*args, *secret_args], sanitized_collection_dir, env=self._bru_env(secrets)
                )
            if not output_file.is_file():
                return None
            try:
                json_result = json.loads(output_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        first_result = json_result[0] if isinstance(json_result, list) and json_result else {}
        results = first_result.get("results", []) or []
        raw_result = results[0] if results else {}
        response = raw_result.get("response") or {}
        return extract_response_items(response.get("data"))

    # ------------------------------------------------------------------
    # bru invocation assembly
    # ------------------------------------------------------------------

    def _prepare_bru_invocation(
        self,
        bru_command: str,
        collection_target: str | None,
        params: Any,
        output_file: Path,
        cwd: Path,
        secret_diagnostics: list[InheritedVariableDiagnostic],
    ) -> tuple[list[str], RunDiagnostics]:
        """Build the bru argument list (without secrets) and run diagnostics.

        Secret variables are deliberately NOT part of the returned args: they travel
        via a temporary `--env-file` (see _secret_env_context) so they never appear
        in the process list or in logs.
        """
        args = [bru_command, "run"]
        if collection_target:
            args.append(collection_target)

        if params.environment:
            args.extend(["--env", params.environment])

        if params.variables:
            for variable in params.variables:
                args.extend(["--env-var", variable])

        args.extend(["--reporter-json", str(output_file)])
        args.append("--reporter-skip-all-headers")

        available_auth_vars = [
            name
            for name in ["BRUNO_AUTH_TOKEN", "BRUNO_BEARER_TOKEN", "BEARER_TOKEN", "bearerToken"]
            if os.environ.get(name)
        ]
        diagnostics = RunDiagnostics(
            cwd=str(cwd),
            bru_args=mask_bru_args(args),
            inherited_variables=secret_diagnostics,
            note=(
                "Secrets are injected via a temporary --env-file (never via CLI arguments) and are also "
                "available to requests as process.env.<NAME>. "
                "If Authorization header is missing, the token did not reach Bruno CLI. "
                f"Non-empty auth env vars visible to the server: {available_auth_vars or 'none'}. "
                "You can also write the token to ~/.config/bruno-mcp/.bearer_token as a fallback."
            ),
        )
        return args, diagnostics

    def _resolve_secret_variables(
        self, inherited_variables: list[str] | None
    ) -> tuple[dict[str, str], list[InheritedVariableDiagnostic]]:
        """Resolve requested inherited variables to {target_name: value}, expanding known aliases."""
        secrets: dict[str, str] = {}
        diagnostics: list[InheritedVariableDiagnostic] = []
        if not inherited_variables:
            return secrets, diagnostics

        for variable_name in inherited_variables:
            resolved = self._resolve_inherited_variable(variable_name)
            if resolved is None:
                raise RuntimeError(
                    f"Missing inherited environment variable: {variable_name}. "
                    f"Set BRUNO_AUTH_TOKEN (or BRUNO_BEARER_TOKEN) in the VS Code MCP secure input or in the server process environment."
                )
            variable_value = resolved["value"]
            if not variable_value.strip():
                raise RuntimeError(
                    f"Inherited variable {variable_name} resolved to an empty value. "
                    f"Refresh BRUNO_AUTH_TOKEN in the VS Code MCP secure input; an empty token causes HTTP 401."
                )
            target_names = self._alias_target_names(variable_name) or [variable_name]
            for target_name in target_names:
                secrets[target_name] = variable_value
            diagnostics.append(
                InheritedVariableDiagnostic(
                    requested_name=variable_name,
                    resolved_name=",".join(target_names),
                    resolved=True,
                    empty=False,
                    source=resolved["source"],
                    length=len(variable_value),
                    sha256_prefix=sha_prefix(variable_value),
                )
            )
        return secrets, diagnostics

    @contextmanager
    def _secret_env_context(self, secrets: dict[str, str]) -> Iterator[list[str]]:
        """Yield the extra bru args that inject secrets without exposing them in the process list."""
        if not secrets:
            yield []
            return
        with secret_env_file(secrets) as env_file_path:
            yield ["--env-file", str(env_file_path)]

    @staticmethod
    def _bru_env(secrets: dict[str, str]) -> dict[str, str] | None:
        """Expose secrets to bru as process env vars too (enables {{process.env.NAME}} lookups)."""
        if not secrets:
            return None
        return {**os.environ, **secrets}

    # ------------------------------------------------------------------
    # Environment conflict handling
    # ------------------------------------------------------------------

    def _prepare_run_dir(self, collection_dir: Path, params: Any, secrets: dict[str, str]) -> Path:
        """Return the directory bru should run in.

        When the selected environment file defines a variable that an injected secret
        targets, bru's --env would take precedence over the --env-file secrets and
        silently break auth. In that case the run happens in a temporary sanitized
        copy with the conflicting entries removed from the environment file copy.
        """
        if not secrets or not getattr(params, "environment", None):
            return collection_dir
        conflicts = self._environment_secret_conflicts(collection_dir, params.environment, set(secrets))
        if not conflicts:
            return collection_dir
        logger.info(
            "env_conflict_neutralized environment=%s variables=%s",
            params.environment,
            sorted(conflicts),
        )
        run_dir = self._sanitize_collection_for_cli(collection_dir)
        self._remove_env_variables(run_dir, params.environment, set(secrets))
        return run_dir

    def _neutralize_env_conflicts(self, run_dir: Path, params: Any, secrets: dict[str, str]) -> None:
        environment = getattr(params, "environment", None)
        if not secrets or not environment:
            return
        self._remove_env_variables(run_dir, environment, set(secrets))

    def _find_environment_file(self, collection_dir: Path, environment: str) -> Path | None:
        environments_dir = self._find_environments_dir(collection_dir)
        if environments_dir is None:
            return None
        for suffix in ENV_FILE_SUFFIXES:
            candidate = environments_dir / f"{environment}{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def _environment_secret_conflicts(
        self, collection_dir: Path, environment: str, secret_names: set[str]
    ) -> set[str]:
        env_file = self._find_environment_file(collection_dir, environment)
        if env_file is None:
            return set()
        try:
            declared = set(self._extract_variable_names(env_file))
            declared |= set(self._extract_list_style_variable_names(env_file))
        except OSError:
            return set()
        return declared & secret_names

    @staticmethod
    def _extract_list_style_variable_names(env_file: Path) -> list[str]:
        """Match `- name: <var>` entries used by list-style environment files."""
        names = []
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*-\s*name:\s*[\"']?([A-Za-z_][A-Za-z0-9_\-.]*)[\"']?\s*$", line)
            if match:
                names.append(match.group(1))
        return names

    def _remove_env_variables(self, sanitized_collection_dir: Path, environment: str, names: set[str]) -> None:
        env_file = self._find_environment_file(sanitized_collection_dir, environment)
        if env_file is None:
            return

        if env_file.suffix == ".json":
            try:
                data = json.loads(env_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("env_conflict_neutralize_failed file=%s reason=invalid_json", env_file)
                return
            variables = data.get("variables")
            if isinstance(variables, list):
                data["variables"] = [
                    variable
                    for variable in variables
                    if not (isinstance(variable, dict) and variable.get("name") in names)
                ]
                env_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return

        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
        removed: list[str] = []
        kept: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            map_match = re.match(r"^(\s*)[\"']?([A-Za-z_][A-Za-z0-9_\-.]*)[\"']?\s*[:=]", line)
            list_match = re.match(r"^(\s*)-\s*name:\s*[\"']?([A-Za-z_][A-Za-z0-9_\-.]*)[\"']?\s*$", line)
            if list_match and list_match.group(2) in names:
                indent = list_match.group(1)
                removed.append(list_match.group(2))
                index += 1
                while index < len(lines):
                    continuation = lines[index]
                    if continuation.strip() and not continuation.startswith(f"{indent} "):
                        break
                    if re.match(rf"^{re.escape(indent)}-\s+name:", continuation):
                        break
                    index += 1
                continue
            if map_match and map_match.group(2) in names:
                removed.append(map_match.group(2))
                index += 1
                continue
            kept.append(line)
            index += 1

        if removed:
            env_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
            logger.info("env_conflicts_removed file=%s variables=%s", env_file, sorted(set(removed)))

    # ------------------------------------------------------------------
    # Artifacts and temp copies
    # ------------------------------------------------------------------

    def _write_artifact(self, prefix: str, content: str) -> ArtifactInfo:
        return artifacts.write_artifact(prefix, content, self._settings)

    def _cleanup_sanitized_collections(self) -> None:
        """Delete temporary sanitized collection copies; reports are already persisted as artifacts."""
        sanitized_root = Path.cwd() / "build" / "sanitized-collections"
        if not sanitized_root.is_dir():
            return
        for entry in sanitized_root.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)

    def _sanitize_collection_for_cli(self, collection_dir: Path) -> Path:
        sanitized_root = Path.cwd() / "build" / "sanitized-collections"
        sanitized_workspace = sanitized_root / f"{collection_dir.name}-{int(_utc_now().timestamp() * 1000)}"
        sanitized_collections_dir = sanitized_workspace / "collections"
        sanitized_dir = sanitized_collections_dir / collection_dir.name
        shutil.copytree(
            collection_dir,
            sanitized_dir,
            ignore=shutil.ignore_patterns("node_modules", ".git"),
        )

        environments_dir = self._find_environments_dir(collection_dir)
        if environments_dir is not None:
            shutil.copytree(
                environments_dir,
                sanitized_dir / "environments",
                ignore=shutil.ignore_patterns("node_modules", ".git"),
            )

        for yaml_file in [*sanitized_dir.rglob("*.yml"), *sanitized_dir.rglob("*.yaml")]:
            sanitize_yaml_for_bru_cli(yaml_file)

        return sanitized_dir

    # ------------------------------------------------------------------
    # Collection resolution and confinement
    # ------------------------------------------------------------------

    def _resolve_collection_target(self, collection_path: Path) -> tuple[Path, str | None]:
        path = collection_path.expanduser().resolve()
        self._check_root_confinement(path)

        if path.name in {"opencollection.yml", "opencollection.yaml"}:
            return path.parent, None

        if path.is_dir() and self._is_collection_root(path):
            return path, None

        if path.suffix in {".bru", ".vru"}:
            collection_root = self._find_collection_root(path.parent)
            if collection_root is None:
                return path.parent, path.name
            return collection_root, str(path.relative_to(collection_root))

        return path.parent, path.name

    def _check_root_confinement(self, path: Path) -> None:
        """Reject collection paths outside the configured workspace roots.

        Confinement only applies when at least one configured root exists on disk;
        with no usable roots configured (for example right after installation, when
        the config still has placeholder roots) any path is accepted.
        """
        if not self._settings.enforce_root_confinement:
            return
        roots = [root.expanduser().resolve() for root in load_bruno_roots()]
        existing_roots = [root for root in roots if root.is_dir()]
        if not existing_roots:
            return
        if any(path == root or root in path.parents for root in existing_roots):
            return
        logger.warning("collection_path_rejected path=%s roots=%s", path, existing_roots)
        raise PermissionError(
            f"Collection path {path} is outside the configured workspace roots "
            f"({', '.join(str(root) for root in existing_roots)}). "
            "Add the parent Bruno root to bruno-mcp.toml [workspace] roots, or set "
            "BRUNO_MCP_ENFORCE_ROOT_CONFINEMENT=0 to disable this check."
        )

    def _is_collection_root(self, path: Path) -> bool:
        return (path / "opencollection.yml").is_file() or (path / "opencollection.yaml").is_file()

    def _find_collection_root(self, path: Path) -> Path | None:
        for candidate in [path, *path.parents]:
            if self._is_collection_root(candidate):
                return candidate
        return None

    def _find_environments_dir(self, collection_path: Path) -> Path | None:
        candidates = []

        for parent in [collection_path, *collection_path.parents]:
            if parent.name == "collections":
                candidates.append(parent.parent / "environments")
            candidates.append(parent / "environments")

        for candidate in candidates:
            if candidate.is_dir():
                return candidate

        return None

    def _extract_variable_names(self, environment_file: Path) -> list[str]:
        try:
            content = environment_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = environment_file.read_text(encoding="latin-1")

        names = set(VARIABLE_NAME_REGEX.findall(content))
        return sorted(names)

    # ------------------------------------------------------------------
    # Filter discovery
    # ------------------------------------------------------------------

    def _list_request_filter_info(self, collection_dir: Path) -> list[RequestFilterInfo]:
        requests = []
        for request_file in sorted(collection_dir.rglob("*.yml")) + sorted(collection_dir.rglob("*.yaml")):
            if request_file.name in {"opencollection.yml", "opencollection.yaml"}:
                continue
            if "environments" in request_file.relative_to(collection_dir).parts:
                continue

            try:
                content = request_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = request_file.read_text(encoding="latin-1")

            name = first_yaml_value(content, "info", "name") or request_file.stem
            method = first_yaml_value(content, "http", "method")
            url = first_yaml_value(content, "http", "url")
            query_params = parse_query_params(content)
            if not query_params:
                continue

            requests.append(
                RequestFilterInfo(
                    name=name,
                    path=str(request_file.relative_to(collection_dir)),
                    method=method,
                    url=url,
                    enabled_query_params=[param for param in query_params if not param.disabled],
                    disabled_query_params=[param for param in query_params if param.disabled],
                )
            )
        return requests

    # ------------------------------------------------------------------
    # Inherited variable resolution (unchanged lookup order)
    # ------------------------------------------------------------------

    def _resolve_inherited_variable(self, variable_name: str) -> dict[str, Any] | None:
        variable_value = os.environ.get(variable_name)
        if variable_value:
            variable_value = variable_value.strip()
            return {
                "target_name": variable_name,
                "value": variable_value,
                "source": variable_name,
                "resolved": True,
                "empty": not variable_value,
            }

        for source_name, target_names in load_auth_variable_aliases().items():
            if variable_name not in target_names:
                continue
            variable_value = os.environ.get(source_name)
            if variable_value:
                variable_value = variable_value.strip()
                return {
                    "target_name": variable_name,
                    "value": variable_value,
                    "source": source_name,
                    "resolved": True,
                    "empty": not variable_value,
                }

        token_file = Path.home() / ".config" / "bruno-mcp" / ".bearer_token"
        if token_file.is_file() and self._alias_target_names(variable_name):
            variable_value = token_file.read_text(encoding="utf-8").strip()
            if variable_value:
                return {
                    "target_name": variable_name,
                    "value": variable_value,
                    "source": str(token_file),
                    "resolved": True,
                    "empty": False,
                }

        return None

    def _alias_target_names(self, variable_name: str) -> list[str]:
        """Return every known alias for an auth variable (itself included), in stable order."""
        aliases = load_auth_variable_aliases()
        if variable_name not in aliases and not any(variable_name in targets for targets in aliases.values()):
            return []
        names: list[str] = []
        for source, targets in aliases.items():
            if variable_name == source or variable_name in targets:
                for candidate in [source, *targets]:
                    if candidate not in names:
                        names.append(candidate)
        return names
