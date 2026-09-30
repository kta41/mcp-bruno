from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import unquote, urlencode

from bruno_mcp.config import load_auth_variable_aliases, load_bruno_roots
from bruno_mcp.types import (
    BrunoRunResult,
    ArtifactInfo,
    ArtifactRequestSummary,
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
    QueryParamInfo,
    ReadRunArtifactParams,
    ReadRunArtifactResult,
    RequestDetail,
    RequestFilterInfo,
    ResponseDataSummary,
    RunDiagnostics,
    RunFilterScenariosParams,
    RunFilterScenariosResult,
    RunFullValidationParams,
    RunFullValidationResult,
    RunCollectionParams,
    Summary,
    Timings,
    ValidationCheck,
)
from bruno_mcp.utils import report_file


REQUEST_SUMMARY_REGEX = re.compile(r"Requests:\s+(\d+)\s+passed,\s+(\d+)\s+failed,\s+(\d+)\s+total")
VARIABLE_NAME_REGEX = re.compile(r"^[\s\"']*([A-Za-z_][A-Za-z0-9_\-.]*)[\s\"']*[:=]", re.MULTILINE)
AUTH_FAILURE_REGEX = re.compile(
    r"\b(401|403|unauthori[sz]ed|forbidden|token\s+(expired|invalid|caducad[oa]|inv[aá]lid[oa])|"
    r"expired\s+token|invalid\s+token|jwt\s+expired|bearer)\b",
    re.IGNORECASE,
)
ROUTING_FAILURE_REGEX = re.compile(r"\b(404|405)\b")
TIMEOUT_FAILURE_REGEX = re.compile(r"\b(502|503|504|timed?\s*out|timeout|gateway\s*timeout)\b", re.IGNORECASE)
YAML_PARSE_ERROR_REGEX = re.compile(r"YAMLParseError|Error parsing item", re.IGNORECASE)
PLAIN_DESCRIPTION_WITH_COLON_REGEX = re.compile(r"^(\s*description:\s+)([^\"'\n].*:\s+.*)$")
REQUEST_FILE_SUFFIXES = {".yml", ".yaml"}
SECRET_KEY_REGEX = re.compile(r"token|secret|password|cookie|authorization|api[-_]?key|jwt|bearer", re.IGNORECASE)
PERCENT_ENCODED_SEQUENCE_REGEX = re.compile(r"%[0-9A-Fa-f]{2}")


def _mask_value(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:3] + "..." + value[-3:]


def _sha_prefix(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


MAX_PREVIEW_LIST_ITEMS = 1
MAX_PREVIEW_DICT_KEYS = 8
MAX_PREVIEW_STRING_CHARS = 120


class BrunoRunner:
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
        start_time = datetime.now(timezone.utc)

        async with report_file("bruno-run-", ".json") as output_file:
            bru_command = await self._ensure_bru_cli()
            collection_dir, collection_target = self._resolve_collection_target(Path(params.collection))

            args = [bru_command, "run"]
            if collection_target:
                args.append(collection_target)

            if params.environment:
                args.extend(["--env", params.environment])

            if params.variables:
                for variable in params.variables:
                    args.extend(["--env-var", variable])

            inherited_diagnostics: list[InheritedVariableDiagnostic] = []
            if params.inherited_variables:
                for variable_name in params.inherited_variables:
                    resolved = self._resolve_inherited_variable(variable_name)
                    if resolved is None:
                        raise RuntimeError(
                            f"Missing inherited environment variable: {variable_name}. "
                            f"Set BRUNO_AUTH_TOKEN (or BRUNO_BEARER_TOKEN) in the VS Code MCP secure input or in the server process environment."
                        )
                    target_name = resolved["target_name"]
                    variable_value = resolved["value"]
                    if not variable_value.strip():
                        raise RuntimeError(
                            f"Inherited variable {variable_name} resolved to an empty value. "
                            f"Refresh BRUNO_AUTH_TOKEN in the VS Code MCP secure input; an empty token causes HTTP 401."
                        )
                    args.extend(["--env-var", f"{target_name}={variable_value}"])
                    inherited_diagnostics.append(
                        InheritedVariableDiagnostic(
                            requested_name=variable_name,
                            resolved_name=target_name,
                            resolved=True,
                            empty=False,
                            source=resolved["source"],
                            length=len(variable_value),
                            sha256_prefix=_sha_prefix(variable_value),
                        )
                    )

            args.extend(["--reporter-json", str(output_file)])
            args.append("--reporter-skip-all-headers")

            available_auth_vars = [
                name
                for name in ["BRUNO_AUTH_TOKEN", "BRUNO_BEARER_TOKEN", "BEARER_TOKEN", "bearerToken"]
                if os.environ.get(name)
            ]
            diagnostics = RunDiagnostics(
                cwd=str(collection_dir),
                bru_args=self._mask_bru_args(args),
                inherited_variables=inherited_diagnostics,
                note=(
                    "If Authorization header is missing, the token did not reach Bruno CLI. "
                    f"Non-empty auth env vars visible to the server: {available_auth_vars or 'none'}. "
                    "You can also write the token to ~/.config/bruno-mcp/.bearer_token as a fallback."
                ),
            )

            returncode, stdout, stderr = await self._run_bru(args, collection_dir)

            if returncode != 0 and YAML_PARSE_ERROR_REGEX.search(stderr):
                sanitized_collection_dir = self._sanitize_collection_for_cli(collection_dir)
                returncode, stdout, stderr = await self._run_bru(args, sanitized_collection_dir)

            if returncode != 0 and not output_file.is_file() and not REQUEST_SUMMARY_REGEX.search(stdout):
                raise RuntimeError(f"CLI stderr: {stderr or 'Unknown error'}")

            result_json = output_file.read_text(encoding="utf-8")
            artifact = self._write_artifact("bruno-run", result_json)
            self._cleanup_sanitized_collections()
            completed_time = datetime.now(timezone.utc)
            duration = int((completed_time - start_time).total_seconds() * 1000)
            json_result = json.loads(result_json)

            first_result = json_result[0] if json_result else {}
            summary_data = first_result.get("summary", {})
            results = first_result.get("results", []) or []
            requests = [self._build_request_detail(result) for result in results]
            failed_requests = sum(1 for request in requests if self._is_failed_request(request))
            success = failed_requests == 0

            failures = [
                Failure(
                    name=request.name,
                    message=request.error or f"HTTP {request.status} {request.status_text or ''}".strip(),
                    auth_failure=self._is_auth_failure(
                        " ".join(str(part) for part in [request.error, request.status, request.status_text] if part)
                    ),
                )
                for request in requests
                if self._is_failed_request(request)
            ]
            auth_failure = any(failure.auth_failure for failure in failures)

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
                    "Open the VS Code Command Palette, run 'MCP: Reset Input', re-enter the token, then restart the bruno-runner server."
                    if auth_failure
                    else None
                ),
                timings=Timings(
                    started=start_time.isoformat().replace("+00:00", "Z"),
                    completed=completed_time.isoformat().replace("+00:00", "Z"),
                    duration=duration,
                ),
            )

    async def run_filter_scenarios(self, params: RunFilterScenariosParams) -> RunFilterScenariosResult:
        start_time = datetime.now(timezone.utc)
        bru_command = await self._ensure_bru_cli()
        collection_dir, _collection_target = self._resolve_collection_target(Path(params.collection))
        request_filters = self._list_request_filter_info(collection_dir)
        scenarios = params.scenarios or self._build_filter_scenarios(request_filters, params.max_scenarios)

        scenario_results, artifact, scenario_diagnostics = await self._execute_scenarios(
            bru_command, collection_dir, scenarios, params
        )

        completed_time = datetime.now(timezone.utc)
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
            and self._is_auth_failure(
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
            timings=Timings(
                started=start_time.isoformat().replace("+00:00", "Z"),
                completed=completed_time.isoformat().replace("+00:00", "Z"),
                duration=int((completed_time - start_time).total_seconds() * 1000),
            ),
        )

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
        for scenario in scenarios:
            sanitized_collection_dir = self._sanitize_collection_for_cli(collection_dir)
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
                    bru_command, collection_dir, scenario.request, params
                )
            baseline_items = baseline_items_by_request[scenario.request]

            self._apply_query_param_overrides(request_file, scenario.query_params)
            async with report_file("bruno-filter-scenario-", ".json") as output_file:
                args, scenario_diagnostics = self._build_bru_args(bru_command, scenario.request, params, output_file)
                returncode, stdout, stderr = await self._run_bru(args, sanitized_collection_dir)

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
                request = self._build_request_detail(raw_result) if raw_result else None
                scenario_results.append(
                    FilterScenarioResult(
                        scenario=scenario,
                        request=request,
                        failure_layer=self._classify_failure_layer(request),
                        validation_checks=self._build_validation_checks(raw_result, scenario.query_params, baseline_items),
                    )
                )

        artifact = self._write_artifact("bruno-filter-scenarios", json.dumps(scenario_artifact_payloads, ensure_ascii=False))
        self._cleanup_sanitized_collections()
        return scenario_results, artifact, scenario_diagnostics

    def _cleanup_sanitized_collections(self) -> None:
        """Delete temporary sanitized collection copies; reports are already persisted as artifacts."""
        sanitized_root = Path.cwd() / "build" / "sanitized-collections"
        if not sanitized_root.is_dir():
            return
        for entry in sanitized_root.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)

    async def run_full_validation(self, params: RunFullValidationParams) -> RunFullValidationResult:
        start_time = datetime.now(timezone.utc)

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
                status="failed" if self._is_failed_request(request) else "passed",
                message=(
                    request.error or f"HTTP {request.status} {request.status_text or ''}".strip()
                    if self._is_failed_request(request)
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

        bru_command = await self._ensure_bru_cli()
        collection_dir, _collection_target = self._resolve_collection_target(Path(params.collection))
        request_filters = self._list_request_filter_info(collection_dir)
        scenarios = self._build_filter_scenarios(request_filters, params.max_scenarios)

        scenario_params = RunFilterScenariosParams(
            collection=params.collection,
            environment=params.environment,
            variables=params.variables,
            inherited_variables=params.inherited_variables,
        )
        scenario_results, filters_artifact, _diagnostics = await self._execute_scenarios(
            bru_command, collection_dir, scenarios, scenario_params
        )

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
                status = "skipped"
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
            and self._is_auth_failure(
                " ".join(
                    str(part)
                    for part in [result.request.error, result.request.status, result.request.status_text]
                    if part
                )
            )
            for result in scenario_results
        )

        completed_time = datetime.now(timezone.utc)
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
            timings=Timings(
                started=start_time.isoformat().replace("+00:00", "Z"),
                completed=completed_time.isoformat().replace("+00:00", "Z"),
                duration=int((completed_time - start_time).total_seconds() * 1000),
            ),
        )


    def _is_auth_failure(self, message: str) -> bool:
        return bool(AUTH_FAILURE_REGEX.search(message))

    def read_run_artifact(self, params: ReadRunArtifactParams) -> ReadRunArtifactResult:
        artifact_path = Path(params.path).expanduser().resolve()
        artifacts_root = (Path.cwd() / "build" / "artifacts").resolve()
        if artifacts_root not in [artifact_path, *artifact_path.parents]:
            raise RuntimeError("Artifact path is outside the Bruno MCP artifacts directory.")
        if not artifact_path.is_file():
            raise RuntimeError(f"Artifact not found: {artifact_path}")

        artifact_data = json.loads(artifact_path.read_text(encoding="utf-8"))
        reports = self._artifact_reports(artifact_data)
        requests = []
        for report in reports:
            first_result = report[0] if isinstance(report, list) and report else {}
            for result in first_result.get("results", []) or []:
                requests.append(self._artifact_request_summary(result, params.max_items))

        return ReadRunArtifactResult(
            artifact=ArtifactInfo(path=str(artifact_path), description="Raw Bruno JSON report stored locally."),
            requests=requests,
        )

    def _artifact_reports(self, artifact_data: Any) -> list[Any]:
        if isinstance(artifact_data, list) and artifact_data and isinstance(artifact_data[0], dict) and "report" in artifact_data[0]:
            return [item.get("report") for item in artifact_data if isinstance(item, dict)]
        return [artifact_data]

    def _artifact_request_summary(self, result: dict, max_items: int) -> ArtifactRequestSummary:
        request = result.get("request") or {}
        response = result.get("response") or {}
        response_data = response.get("data")
        return ArtifactRequestSummary(
            name=result.get("name") or result.get("suitename") or "Unknown request",
            path=result.get("path"),
            method=request.get("method"),
            url=request.get("url"),
            status=response.get("status") or result.get("status"),
            status_text=response.get("statusText"),
            response_time=response.get("responseTime") or response.get("duration"),
            response_data=self._response_data_summary(response_data, max_items),
        )

    def _response_data_summary(self, response_data: Any, max_items: int) -> ResponseDataSummary:
        items = self._extract_response_items(response_data)
        top_level_keys = list(response_data.keys())[:20] if isinstance(response_data, dict) else []
        item_keys = []
        sample_items = []

        if items is not None:
            key_names = set()
            for item in items[:max_items]:
                if isinstance(item, dict):
                    key_names.update(str(key) for key in item.keys())
                sample_items.append(self._preview_response_data(item))
            item_keys = sorted(key_names)[:30]

        return ResponseDataSummary(
            data_type=type(response_data).__name__ if response_data is not None else None,
            item_count=len(items) if items is not None else None,
            top_level_keys=top_level_keys,
            item_keys=item_keys,
            sample_items=sample_items,
        )

    def _write_artifact(self, prefix: str, content: str) -> ArtifactInfo:
        artifacts_dir = Path.cwd() / "build" / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{prefix}-{int(datetime.now(timezone.utc).timestamp() * 1000)}.json"
        artifact_path.write_text(content, encoding="utf-8")
        return ArtifactInfo(path=str(artifact_path), description="Raw Bruno JSON report stored locally; use read-result-artifact for a bounded summary.")

    def _build_request_detail(self, result: dict) -> RequestDetail:
        request = result.get("request") or {}
        response = result.get("response") or {}
        response_data = response.get("data")
        test_results = result.get("testResults") or []
        assertion_results = result.get("assertionResults") or []

        return RequestDetail(
            name=result.get("name") or result.get("suitename") or "Unknown request",
            path=result.get("path"),
            method=request.get("method"),
            url=request.get("url"),
            status=response.get("status") or result.get("status"),
            status_text=response.get("statusText"),
            response_time=response.get("responseTime") or response.get("duration"),
            response_data_type=type(response_data).__name__ if response_data is not None else None,
            response_item_count=self._response_item_count(response_data),
            response_body_preview=self._preview_response_data(response_data),
            error=result.get("error"),
            tests_total=len(test_results),
            tests_passed=sum(1 for test in test_results if test.get("status") == "pass"),
            tests_failed=sum(1 for test in test_results if test.get("status") == "fail"),
            assertions_total=len(assertion_results),
            assertions_passed=sum(1 for assertion in assertion_results if assertion.get("status") == "pass"),
            assertions_failed=sum(1 for assertion in assertion_results if assertion.get("status") == "fail"),
        )

    def _is_failed_request(self, request: RequestDetail) -> bool:
        if request.error:
            return True
        if isinstance(request.status, int):
            return request.status >= 400
        if isinstance(request.status, str) and request.status.isdigit():
            return int(request.status) >= 400
        return False

    def _build_bru_args(
        self,
        bru_command: str,
        collection_target: str | None,
        params: Any,
        output_file: Path,
    ) -> tuple[list[str], RunDiagnostics]:
        args = [bru_command, "run"]
        if collection_target:
            args.append(collection_target)

        if params.environment:
            args.extend(["--env", params.environment])

        if params.variables:
            for variable in params.variables:
                args.extend(["--env-var", variable])

        inherited_diagnostics: list[InheritedVariableDiagnostic] = []
        if params.inherited_variables:
            for variable_name in params.inherited_variables:
                resolved = self._resolve_inherited_variable(variable_name)
                if resolved is None:
                    raise RuntimeError(
                        f"Missing inherited environment variable: {variable_name}. "
                        f"Set BRUNO_AUTH_TOKEN (or BRUNO_BEARER_TOKEN) in the VS Code MCP secure input or in the server process environment."
                    )
                target_names = self._alias_target_names(variable_name)
                variable_value = resolved["value"]
                if not variable_value.strip():
                    raise RuntimeError(
                        f"Inherited variable {variable_name} resolved to an empty value. "
                        f"Refresh BRUNO_AUTH_TOKEN in the VS Code MCP secure input; an empty token causes HTTP 401."
                    )
                for target_name in target_names:
                    args.extend(["--env-var", f"{target_name}={variable_value}"])
                inherited_diagnostics.append(
                    InheritedVariableDiagnostic(
                        requested_name=variable_name,
                        resolved_name=",".join(target_names),
                        resolved=True,
                        empty=False,
                        source=resolved["source"],
                        length=len(variable_value),
                        sha256_prefix=_sha_prefix(variable_value),
                    )
                )

        args.extend(["--reporter-json", str(output_file)])
        args.append("--reporter-skip-all-headers")
        cwd = self._resolve_collection_target(Path(params.collection))[0]
        available_auth_vars = [
            name
            for name in ["BRUNO_AUTH_TOKEN", "BRUNO_BEARER_TOKEN", "BEARER_TOKEN", "bearerToken"]
            if os.environ.get(name)
        ]
        diagnostics = RunDiagnostics(
            cwd=str(cwd),
            bru_args=self._mask_bru_args(args),
            inherited_variables=inherited_diagnostics,
            note=(
                "If Authorization header is missing, the token did not reach Bruno CLI. "
                f"Non-empty auth env vars visible to the server: {available_auth_vars or 'none'}. "
                "You can also write the token to ~/.config/bruno-mcp/.bearer_token as a fallback."
            ),
        )
        return args, diagnostics

    def _mask_bru_args(self, args: list[str]) -> list[str]:
        masked: list[str] = []
        previous_arg = ""
        for arg in args:
            is_env_var_value = previous_arg == "--env-var" and "=" in arg
            if is_env_var_value or (arg.startswith("--env-var=") and "=" in arg):
                prefix = "" if is_env_var_value else "--env-var="
                raw = arg if is_env_var_value else arg[len("--env-var="):]
                key, _, value = raw.partition("=")
                masked.append(f"{prefix}{key}={_mask_value(value)}")
            elif "=" in arg and SECRET_KEY_REGEX.search(arg.split("=", 1)[0]):
                key, value = arg.split("=", 1)
                masked.append(f"{key}={_mask_value(value)}")
            else:
                masked.append(arg)
            previous_arg = arg
        return masked

    def _build_filter_scenarios(self, requests: list[RequestFilterInfo], max_scenarios: int) -> list[FilterScenario]:
        scenarios: list[FilterScenario] = []
        for request in requests:
            for query_param in request.disabled_query_params:
                if query_param.value is None:
                    continue
                if query_param.value.upper() == "ALL":
                    continue
                scenarios.append(
                    FilterScenario(
                        name=f"{request.name} - {query_param.name}={query_param.value}",
                        request=request.path,
                        query_params={query_param.name: query_param.value},
                    )
                )
                if len(scenarios) >= max_scenarios:
                    return scenarios
        return scenarios

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

            name = self._first_yaml_value(content, "info", "name") or request_file.stem
            method = self._first_yaml_value(content, "http", "method")
            url = self._first_yaml_value(content, "http", "url")
            query_params = self._parse_query_params(content)
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

    def _first_yaml_value(self, content: str, section: str, key: str) -> str | None:
        in_section = False
        for line in content.splitlines():
            if line == f"{section}:":
                in_section = True
                continue
            if in_section and line and not line.startswith(" "):
                return None
            if in_section:
                match = re.match(rf"^  {re.escape(key)}:\s*(.*)$", line)
                if match:
                    return self._clean_yaml_scalar(match.group(1))
        return None

    def _parse_query_params(self, content: str) -> list[QueryParamInfo]:
        params = []
        in_params = False
        current: dict[str, Any] | None = None

        for line in content.splitlines():
            if line == "  params:":
                in_params = True
                continue
            if in_params and line.startswith("  ") and not line.startswith("    "):
                break
            if not in_params:
                continue

            name_match = re.match(r"^    - name:\s*(.*)$", line)
            if name_match:
                if current and current.get("type") == "query":
                    params.append(QueryParamInfo(**current))
                current = {"name": self._clean_yaml_scalar(name_match.group(1)), "disabled": False}
                continue

            if current is None:
                continue

            field_match = re.match(r"^      (value|type|description|disabled):\s*(.*)$", line)
            if not field_match:
                continue
            field, value = field_match.groups()
            if field == "disabled":
                current[field] = self._clean_yaml_scalar(value).lower() == "true"
            else:
                current[field] = self._clean_yaml_scalar(value)

        if current and current.get("type") == "query":
            params.append(QueryParamInfo(**current))
        return params

    def _clean_yaml_scalar(self, value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    def _apply_query_param_overrides(self, request_file: Path, query_params: dict[str, str]) -> None:
        lines = request_file.read_text(encoding="utf-8").splitlines()
        updated: list[str] = []
        index = 0

        while index < len(lines):
            name_match = re.match(r"^(    - name:\s*)(.*)$", lines[index])
            if not name_match:
                updated.append(lines[index])
                index += 1
                continue

            param_name = self._clean_yaml_scalar(name_match.group(2))
            block = [lines[index]]
            index += 1
            while index < len(lines) and not re.match(r"^    - name:\s*", lines[index]):
                block.append(lines[index])
                index += 1

            if param_name in query_params:
                block = self._override_query_param_block(block, query_params[param_name])
            updated.extend(block)

        # bru CLI does not reliably transmit query params declared in the `params:` block,
        # so the overridden params are also embedded directly in the request URL.
        updated = self._embed_query_params_in_url(updated, query_params)

        request_file.write_text("\n".join(updated) + "\n", encoding="utf-8")

    def _embed_query_params_in_url(self, lines: list[str], query_params: dict[str, str]) -> list[str]:
        if not query_params:
            return lines
        # `bru` re-encodes the query string itself when it executes the request, so pre-encoding
        # reserved-but-valid-in-query characters like ':' here would make it end up double-encoded.
        query_string = urlencode(
            {key: self._normalize_query_param_value(value) for key, value in query_params.items()},
            safe=":",
            quote_via=url_quote,
        )
        updated: list[str] = []
        embedded = False
        for line in lines:
            match = re.match(r"^(\s*url:\s*)(.*)$", line) if not embedded else None
            if not match:
                updated.append(line)
                continue
            prefix, raw_url = match.groups()
            raw_url = raw_url.strip()
            quote = raw_url[0] if raw_url[:1] in {"'", '"'} and raw_url.endswith(raw_url[0]) else ""
            url = raw_url[1:-1] if quote else raw_url
            separator = "&" if "?" in url else "?"
            updated.append(f"{prefix}{quote}{url}{separator}{query_string}{quote}")
            embedded = True
        return updated

    def _normalize_query_param_value(self, value: str) -> str:
        if PERCENT_ENCODED_SEQUENCE_REGEX.search(value):
            return unquote(value)
        return value

    def _override_query_param_block(self, block: list[str], value: str) -> list[str]:
        result = []
        saw_disabled = False
        for line in block:
            if re.match(r"^      value:\s*", line):
                result.append(f"      value: {json.dumps(value)}")
            elif re.match(r"^      disabled:\s*", line):
                result.append("      disabled: false")
                saw_disabled = True
            else:
                result.append(line)

        if not saw_disabled:
            result.append("      disabled: false")
        return result

    async def _run_request_baseline(
        self,
        bru_command: str,
        collection_dir: Path,
        request_path: str,
        params: Any,
    ) -> list[Any] | None:
        """Run the request once without filter overrides and return its response items (None when unavailable)."""
        sanitized_collection_dir = self._sanitize_collection_for_cli(collection_dir)
        request_file = sanitized_collection_dir / request_path
        if not request_file.is_file():
            return None
        async with report_file("bruno-filter-baseline-", ".json") as output_file:
            args, _diagnostics = self._build_bru_args(bru_command, request_path, params, output_file)
            await self._run_bru(args, sanitized_collection_dir)
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
        return self._extract_response_items(response.get("data"))

    def _classify_failure_layer(self, request: RequestDetail | None) -> str | None:
        """Classify infrastructure-level failures that make filter evaluation inconclusive."""
        if request is None:
            return None
        if request.error:
            return "connectivity"
        status_text = str(request.status_text or "")
        message = f"{request.status} {status_text}"
        if self._is_auth_failure(message):
            return "auth"
        status_code = int(request.status) if str(request.status).isdigit() else None
        if status_code is not None and ROUTING_FAILURE_REGEX.search(str(status_code)):
            return "routing"
        if status_code in (502, 503, 504) or TIMEOUT_FAILURE_REGEX.search(status_text):
            return "timeout"
        if status_code is not None and status_code >= 500:
            return "server_error"
        return None

    def _build_validation_checks(
        self,
        result: dict,
        query_params: dict[str, str],
        baseline_items: list[Any] | None = None,
    ) -> list[ValidationCheck]:
        request = self._build_request_detail(result)
        response = result.get("response") or {}
        response_data = response.get("data")
        checks = [
            ValidationCheck(
                name="http_status",
                status="passed" if not self._is_failed_request(request) else "failed",
                message=f"HTTP status is {request.status} {request.status_text or ''}".strip(),
            )
        ]

        failure_layer = self._classify_failure_layer(request)
        if failure_layer is not None:
            checks.append(
                ValidationCheck(
                    name="failure_layer",
                    status="failed",
                    message=(
                        f"Infrastructure failure ({failure_layer}): filter behaviour cannot be evaluated. "
                        f"Fix the {failure_layer} issue and rerun before reporting filter defects."
                    ),
                )
            )
            checks.append(
                ValidationCheck(name="response_body", status="skipped", message=f"Not evaluated: {failure_layer} failure.")
            )
            for name in query_params:
                checks.append(
                    ValidationCheck(name=f"filter:{name}", status="skipped", message=f"Not evaluated: {failure_layer} failure.")
                )
            return checks

        items = self._extract_response_items(response_data)
        if response_data is None:
            checks.append(ValidationCheck(name="response_body", status="skipped", message="Response body is empty."))
            return checks

        checks.append(
            ValidationCheck(
                name="response_body",
                status="passed",
                message=f"Response body type is {type(response_data).__name__}; item count is {self._response_item_count(response_data)}.",
            )
        )

        for name, value in query_params.items():
            checks.append(self._validate_filter_param(name, value, items))

        checks.append(self._build_filter_impact_check(query_params, items, baseline_items))

        return checks

    def _build_filter_impact_check(
        self,
        query_params: dict[str, str],
        items: list[Any] | None,
        baseline_items: list[Any] | None,
    ) -> ValidationCheck:
        """Compare the filtered response against the unfiltered baseline to prove the filter had an effect."""
        restrictive = [
            (name, value)
            for name, value in query_params.items()
            if name not in {"offset", "limit", "sortBy", "sortOrder"} and value.strip().upper() != "ALL"
        ]
        if not restrictive:
            return ValidationCheck(
                name="filter_impact",
                status="skipped",
                message="Scenario has no restrictive filter; baseline comparison not applicable.",
            )
        if baseline_items is None or items is None:
            return ValidationCheck(
                name="filter_impact",
                status="skipped",
                message="Baseline comparison unavailable (baseline run failed or no identifiable item array).",
            )
        if not items:
            if not baseline_items:
                return ValidationCheck(
                    name="filter_impact",
                    status="skipped",
                    message="Baseline also returned no items; cannot prove the filter had an effect.",
                )
            return ValidationCheck(
                name="filter_impact",
                status="passed",
                message=(
                    f"Filter returned 0 results while the unfiltered baseline returned {len(baseline_items)} items "
                    "- filter applied; no matching records in the dataset."
                ),
            )
        if items == baseline_items:
            if self._items_contradict_filters(query_params, baseline_items):
                return ValidationCheck(
                    name="filter_impact",
                    status="failed",
                    message=(
                        "Response is identical to the unfiltered baseline and contains items that violate the filter "
                        "- the backend ignored the filter."
                    ),
                )
            return ValidationCheck(
                name="filter_impact",
                status="skipped",
                message=(
                    "Response matches the baseline, but every baseline item satisfies the filter; "
                    "cannot distinguish an ignored filter from a no-op."
                ),
            )
        return ValidationCheck(
            name="filter_impact",
            status="passed",
            message="Response differs from the unfiltered baseline, so the filter had an effect.",
        )

    def _items_contradict_filters(self, query_params: dict[str, str], items: list[Any]) -> bool:
        """Return True when at least one item violates at least one restrictive scenario filter."""
        for name, value in query_params.items():
            if name in {"offset", "limit", "sortBy", "sortOrder"} or value.strip().upper() == "ALL":
                continue
            field_names = self._filter_field_names(name)
            for item in items:
                values = [str(found) for field_name in field_names for found in self._find_values_by_key(item, field_name)]
                if values and not self._values_match_filter(name, value, values):
                    return True
        return False

    def _validate_filter_param(self, name: str, value: str, items: list[Any] | None) -> ValidationCheck:
        if name in {"offset", "sortBy", "sortOrder"}:
            return ValidationCheck(name=f"filter:{name}", status="skipped", message="Filter affects pagination or ordering and needs baseline comparison.")

        if name == "limit":
            if items is None:
                return ValidationCheck(name="filter:limit", status="skipped", message="Could not identify a response item array.")
            return ValidationCheck(
                name="filter:limit",
                status="passed" if len(items) <= int(value) else "failed",
                message=f"Returned {len(items)} items with limit={value}.",
            )

        if value.upper() == "ALL":
            return ValidationCheck(name=f"filter:{name}", status="skipped", message="ALL is not restrictive, so it cannot prove filtering.")

        if items is None:
            return ValidationCheck(name=f"filter:{name}", status="skipped", message="Could not identify a response item array.")
        if not items:
            return ValidationCheck(name=f"filter:{name}", status="passed", message="Response returned no items, so the filter is not contradicted.")

        field_names = self._filter_field_names(name)
        mismatches = []
        for item in items:
            values = [str(found) for field_name in field_names for found in self._find_values_by_key(item, field_name)]
            if not values:
                return ValidationCheck(name=f"filter:{name}", status="skipped", message=f"Could not find a comparable field for {name} in response items.")
            if not self._values_match_filter(name, value, values):
                mismatches.append(values[:3])

        return ValidationCheck(
            name=f"filter:{name}",
            status="passed" if not mismatches else "failed",
            message=(
                f"All returned items match {name}={value}."
                if not mismatches
                else f"Some returned items do not match {name}={value}; sample values: {mismatches[:3]}."
            ),
        )

    def _filter_field_names(self, name: str) -> list[str]:
        aliases = {
            "minBaseScore": ["baseScore", "score"],
            "maxBaseScore": ["baseScore", "score"],
            "minSeverityScore": ["severityScore"],
            "maxSeverityScore": ["severityScore"],
            "fromCreatedAt": ["createdAt", "created_at"],
            "toCreatedAt": ["createdAt", "created_at"],
            "fromUpdatedAt": ["updatedAt", "updated_at"],
            "toUpdatedAt": ["updatedAt", "updated_at"],
            "fromPublishedAt": ["publishedAt", "published_at"],
            "toPublishedAt": ["publishedAt", "published_at"],
            "minAgeDays": ["ageDays", "age_days"],
            "maxAgeDays": ["ageDays", "age_days"],
        }
        return aliases.get(name, [name])

    def _values_match_filter(self, name: str, expected: str, values: list[str]) -> bool:
        if name.startswith("min"):
            return any(self._to_float(value) is not None and self._to_float(value) >= float(expected) for value in values)
        if name.startswith("max"):
            return any(self._to_float(value) is not None and self._to_float(value) <= float(expected) for value in values)
        if name.startswith("from"):
            return any(value >= expected for value in values)
        if name.startswith("to"):
            return any(value <= expected for value in values)
        if name in {"title", "hostname"}:
            return any(expected.lower() in value.lower() for value in values)
        return any(value.lower() == expected.lower() for value in values)

    def _to_float(self, value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    def _find_values_by_key(self, data: Any, key: str) -> list[Any]:
        if isinstance(data, dict):
            values = [value for current_key, value in data.items() if current_key.lower() == key.lower()]
            for value in data.values():
                values.extend(self._find_values_by_key(value, key))
            return values
        if isinstance(data, list):
            values = []
            for item in data:
                values.extend(self._find_values_by_key(item, key))
            return values
        return []

    def _response_item_count(self, response_data: Any) -> int | None:
        items = self._extract_response_items(response_data)
        return len(items) if items is not None else None

    def _extract_response_items(self, response_data: Any) -> list[Any] | None:
        if isinstance(response_data, list):
            return response_data
        if isinstance(response_data, dict):
            for key in ["data", "items", "results", "content", "records", "entries"]:
                value = response_data.get(key)
                if isinstance(value, list):
                    return value
        return None

    def _preview_response_data(self, response_data: Any, depth: int = 0) -> Any:
        if response_data is None or isinstance(response_data, bool | int | float):
            return response_data
        if isinstance(response_data, str):
            return response_data[:MAX_PREVIEW_STRING_CHARS]
        if isinstance(response_data, list):
            preview = [self._preview_response_data(item, depth + 1) for item in response_data[:MAX_PREVIEW_LIST_ITEMS]]
            if len(response_data) > MAX_PREVIEW_LIST_ITEMS:
                preview.append({"_omitted_items": len(response_data) - MAX_PREVIEW_LIST_ITEMS})
            return preview
        if isinstance(response_data, dict):
            if depth >= 2:
                preview = {
                    key: self._redacted_or_short_value(key, value)
                    for key, value in list(response_data.items())[:MAX_PREVIEW_DICT_KEYS]
                }
            else:
                preview = {
                key: self._preview_response_data(value, depth + 1) if not SECRET_KEY_REGEX.search(str(key)) else "<redacted>"
                    for key, value in list(response_data.items())[:MAX_PREVIEW_DICT_KEYS]
                }
            if len(response_data) > MAX_PREVIEW_DICT_KEYS:
                preview["_omitted_keys"] = len(response_data) - MAX_PREVIEW_DICT_KEYS
            return preview
        return str(response_data)[:MAX_PREVIEW_STRING_CHARS]

    def _redacted_or_short_value(self, key: str, value: Any) -> Any:
        if SECRET_KEY_REGEX.search(str(key)):
            return "<redacted>"
        if isinstance(value, str):
            return value[:MAX_PREVIEW_STRING_CHARS]
        if isinstance(value, bool | int | float) or value is None:
            return value
        return f"<{type(value).__name__}>"

    async def _run_bru(self, args: list[str], cwd: Path) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        return (
            process.returncode or 0,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )

    def _write_artifact(self, prefix: str, content: str) -> ArtifactInfo:
        artifact_dir = Path.cwd() / "build" / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        artifact_path = artifact_dir / f"{prefix}-{timestamp}.json"
        artifact_path.write_text(content, encoding="utf-8")
        return ArtifactInfo(
            path=str(artifact_path),
            description="Full raw Bruno JSON report. Use read-result-artifact for a bounded summary.",
        )

    def read_run_artifact(self, params: ReadRunArtifactParams) -> ReadRunArtifactResult:
        artifact_path = Path(params.path)
        if not artifact_path.is_file():
            return ReadRunArtifactResult(
                artifact=ArtifactInfo(path=str(artifact_path), description="Not found."),
                requests=[],
            )

        raw_report = json.loads(artifact_path.read_text(encoding="utf-8"))
        first_result = raw_report[0] if raw_report else {}
        results = first_result.get("results", []) or []

        request_summaries = []
        for result in results:
            request = result.get("request") or {}
            response = result.get("response") or {}
            response_data = response.get("data")
            request_summaries.append(
                ArtifactRequestSummary(
                    name=result.get("name") or result.get("suitename") or "Unknown request",
                    path=result.get("path"),
                    method=request.get("method"),
                    url=request.get("url"),
                    status=response.get("status") or result.get("status"),
                    status_text=response.get("statusText"),
                    response_time=response.get("responseTime") or response.get("duration"),
                    response_data=self._summarize_response_data(response_data, params.max_items),
                )
            )

        return ReadRunArtifactResult(
            artifact=ArtifactInfo(
                path=str(artifact_path),
                description="Bounded, redacted summary of the raw Bruno JSON report.",
            ),
            requests=request_summaries,
        )

    def _summarize_response_data(self, response_data: Any, max_items: int) -> ResponseDataSummary:
        if response_data is None:
            return ResponseDataSummary(data_type=None, item_count=None, top_level_keys=[], item_keys=[], sample_items=[])

        top_level_keys = list(response_data.keys()) if isinstance(response_data, dict) else []
        items = self._extract_response_items(response_data)

        if items is None:
            return ResponseDataSummary(
                data_type=type(response_data).__name__,
                item_count=None,
                top_level_keys=top_level_keys,
                item_keys=[],
                sample_items=[self._preview_response_data(response_data)],
            )

        item_keys = []
        if items and isinstance(items[0], dict):
            item_keys = list(items[0].keys())

        return ResponseDataSummary(
            data_type=type(response_data).__name__,
            item_count=len(items),
            top_level_keys=top_level_keys,
            item_keys=item_keys,
            sample_items=[self._preview_response_data(item) for item in items[:max_items]],
        )

    def _sanitize_collection_for_cli(self, collection_dir: Path) -> Path:
        sanitized_root = Path.cwd() / "build" / "sanitized-collections"
        sanitized_workspace = sanitized_root / f"{collection_dir.name}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
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
            self._sanitize_yaml_for_bru_cli(yaml_file)

        return sanitized_dir

    def _sanitize_yaml_for_bru_cli(self, yaml_file: Path) -> None:
        content = yaml_file.read_text(encoding="utf-8")
        sanitized_lines = []
        changed = False

        for line in content.splitlines():
            match = PLAIN_DESCRIPTION_WITH_COLON_REGEX.match(line)
            if match:
                prefix, value = match.groups()
                sanitized_lines.append(f"{prefix}{json.dumps(value)}")
                changed = True
            else:
                sanitized_lines.append(line)

        if changed:
            yaml_file.write_text("\n".join(sanitized_lines) + "\n", encoding="utf-8")

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

    async def _ensure_bru_cli(self) -> str:
        bru_command = shutil.which("bru")
        if bru_command:
            return bru_command

        npm_command = shutil.which("npm")
        if not npm_command:
            raise RuntimeError(
                "Bruno CLI command `bru` is not installed and `npm` was not found, so it cannot be installed automatically."
            )

        process = await asyncio.create_subprocess_exec(
            npm_command,
            "install",
            "-g",
            "@usebruno/cli",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            stderr = stderr_bytes.decode(errors="replace")
            raise RuntimeError(f"Bruno CLI `bru` is not installed and automatic npm installation failed: {stderr}")

        bru_command = shutil.which("bru")
        if not bru_command:
            raise RuntimeError("Bruno CLI installation completed, but `bru` is still not available in PATH.")

        return bru_command

    def _resolve_collection_target(self, collection_path: Path) -> tuple[Path, str | None]:
        path = collection_path.expanduser().resolve()

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
