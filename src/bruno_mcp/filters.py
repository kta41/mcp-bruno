"""Filter scenario parsing, request rewriting, and validation logic.

All functions here are pure (or operate only on explicitly passed paths) so the
MCP-facing orchestration stays in runner.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import unquote, urlencode

from bruno_mcp.redaction import preview_response_data
from bruno_mcp.responses import extract_response_items, response_item_count
from bruno_mcp.types import (
    FilterScenario,
    QueryParamInfo,
    RequestDetail,
    RequestFilterInfo,
    ValidationCheck,
)

AUTH_FAILURE_REGEX = re.compile(
    r"\b(401|403|unauthori[sz]ed|forbidden|token\s+(expired|invalid|caducad[oa]|inv[aá]lid[oa])|"
    r"expired\s+token|invalid\s+token|jwt\s+expired|bearer)\b",
    re.IGNORECASE,
)
ROUTING_FAILURE_REGEX = re.compile(r"\b(404|405)\b")
TIMEOUT_FAILURE_REGEX = re.compile(r"\b(502|503|504|timed?\s*out|timeout|gateway\s*timeout)\b", re.IGNORECASE)
PLAIN_DESCRIPTION_WITH_COLON_REGEX = re.compile(r"^(\s*description:\s+)([^\"'\n].*:\s+.*)$")
PERCENT_ENCODED_SEQUENCE_REGEX = re.compile(r"%[0-9A-Fa-f]{2}")

NON_RESTRICTIVE_FILTERS = {"offset", "limit", "sortBy", "sortOrder"}


def is_auth_failure(message: str) -> bool:
    return bool(AUTH_FAILURE_REGEX.search(message))


def is_failed_request(request: RequestDetail) -> bool:
    if request.error:
        return True
    if isinstance(request.status, int):
        return request.status >= 400
    if isinstance(request.status, str) and request.status.isdigit():
        return int(request.status) >= 400
    return False


def build_request_detail(result: dict) -> RequestDetail:
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
        response_item_count=response_item_count(response_data),
        response_body_preview=preview_response_data(response_data),
        error=result.get("error"),
        tests_total=len(test_results),
        tests_passed=sum(1 for test in test_results if test.get("status") == "pass"),
        tests_failed=sum(1 for test in test_results if test.get("status") == "fail"),
        assertions_total=len(assertion_results),
        assertions_passed=sum(1 for assertion in assertion_results if assertion.get("status") == "pass"),
        assertions_failed=sum(1 for assertion in assertion_results if assertion.get("status") == "fail"),
    )


def classify_failure_layer(request: RequestDetail | None) -> str | None:
    """Classify infrastructure-level failures that make filter evaluation inconclusive."""
    if request is None:
        return None
    if request.error:
        return "connectivity"
    status_text = str(request.status_text or "")
    message = f"{request.status} {status_text}"
    if is_auth_failure(message):
        return "auth"
    status_code = int(str(request.status)) if str(request.status).isdigit() else None
    if status_code is not None and ROUTING_FAILURE_REGEX.search(str(status_code)):
        return "routing"
    if status_code in (502, 503, 504) or TIMEOUT_FAILURE_REGEX.search(status_text):
        return "timeout"
    if status_code is not None and status_code >= 500:
        return "server_error"
    return None


def build_filter_scenarios(requests: list[RequestFilterInfo], max_scenarios: int) -> list[FilterScenario]:
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


def first_yaml_value(content: str, section: str, key: str) -> str | None:
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
                return clean_yaml_scalar(match.group(1))
    return None


def parse_query_params(content: str) -> list[QueryParamInfo]:
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
            current = {"name": clean_yaml_scalar(name_match.group(1)), "disabled": False}
            continue

        if current is None:
            continue

        field_match = re.match(r"^      (value|type|description|disabled):\s*(.*)$", line)
        if not field_match:
            continue
        field, value = field_match.groups()
        if field == "disabled":
            current[field] = clean_yaml_scalar(value).lower() == "true"
        else:
            current[field] = clean_yaml_scalar(value)

    if current and current.get("type") == "query":
        params.append(QueryParamInfo(**current))
    return params


def clean_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def apply_query_param_overrides(request_file: Path, query_params: dict[str, str]) -> None:
    lines = request_file.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    index = 0

    while index < len(lines):
        name_match = re.match(r"^(    - name:\s*)(.*)$", lines[index])
        if not name_match:
            updated.append(lines[index])
            index += 1
            continue

        param_name = clean_yaml_scalar(name_match.group(2))
        block = [lines[index]]
        index += 1
        while index < len(lines) and not re.match(r"^    - name:\s*", lines[index]):
            block.append(lines[index])
            index += 1

        if param_name in query_params:
            block = override_query_param_block(block, query_params[param_name])
        updated.extend(block)

    # bru CLI does not reliably transmit query params declared in the `params:` block,
    # so the overridden params are also embedded directly in the request URL.
    updated = embed_query_params_in_url(updated, query_params)

    request_file.write_text("\n".join(updated) + "\n", encoding="utf-8")


def embed_query_params_in_url(lines: list[str], query_params: dict[str, str]) -> list[str]:
    if not query_params:
        return lines
    # `bru` re-encodes the query string itself when it executes the request, so pre-encoding
    # reserved-but-valid-in-query characters like ':' here would make it end up double-encoded.
    query_string = urlencode(
        {key: normalize_query_param_value(value) for key, value in query_params.items()},
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


def normalize_query_param_value(value: str) -> str:
    if PERCENT_ENCODED_SEQUENCE_REGEX.search(value):
        return unquote(value)
    return value


def override_query_param_block(block: list[str], value: str) -> list[str]:
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


def sanitize_yaml_for_bru_cli(yaml_file: Path) -> None:
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


def build_validation_checks(
    result: dict,
    query_params: dict[str, str],
    baseline_items: list[Any] | None = None,
) -> list[ValidationCheck]:
    request = build_request_detail(result)
    response = result.get("response") or {}
    response_data = response.get("data")
    checks = [
        ValidationCheck(
            name="http_status",
            status="passed" if not is_failed_request(request) else "failed",
            message=f"HTTP status is {request.status} {request.status_text or ''}".strip(),
        )
    ]

    failure_layer = classify_failure_layer(request)
    if failure_layer is not None:
        checks.append(
            ValidationCheck(
                name="failure_layer",
                status="failed",
                message=(
                    f"Infrastructure failure ({failure_layer}): filter behaviour cannot be evaluated. "
                    "Fix the underlying issue and rerun."
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

    items = extract_response_items(response_data)
    if response_data is None:
        checks.append(ValidationCheck(name="response_body", status="skipped", message="Response body is empty."))
        return checks

    checks.append(
        ValidationCheck(
            name="response_body",
            status="passed",
            message=f"Response body type is {type(response_data).__name__}; item count is {response_item_count(response_data)}.",
        )
    )

    for name, value in query_params.items():
        checks.append(validate_filter_param(name, value, items))

    checks.append(build_filter_impact_check(query_params, items, baseline_items))

    return checks


def build_filter_impact_check(
    query_params: dict[str, str],
    items: list[Any] | None,
    baseline_items: list[Any] | None,
) -> ValidationCheck:
    """Compare the filtered response against the unfiltered baseline to prove the filter had an effect."""
    restrictive = [
        (name, value)
        for name, value in query_params.items()
        if name not in NON_RESTRICTIVE_FILTERS and value.strip().upper() != "ALL"
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
        if items_contradict_filters(query_params, baseline_items):
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


def items_contradict_filters(query_params: dict[str, str], items: list[Any]) -> bool:
    """Return True when at least one item violates at least one restrictive scenario filter."""
    for name, value in query_params.items():
        if name in NON_RESTRICTIVE_FILTERS or value.strip().upper() == "ALL":
            continue
        field_names = filter_field_names(name)
        for item in items:
            values = [str(found) for field_name in field_names for found in find_values_by_key(item, field_name)]
            if values and not values_match_filter(name, value, values):
                return True
    return False


def validate_filter_param(name: str, value: str, items: list[Any] | None) -> ValidationCheck:
    if name in {"offset", "sortBy", "sortOrder"}:
        return ValidationCheck(
            name=f"filter:{name}",
            status="skipped",
            message="Filter affects pagination or ordering and needs baseline comparison.",
        )

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
        return ValidationCheck(
            name=f"filter:{name}",
            status="passed",
            message="Response returned no items, so the filter is not contradicted.",
        )

    field_names = filter_field_names(name)
    mismatches = []
    for item in items:
        values = [str(found) for field_name in field_names for found in find_values_by_key(item, field_name)]
        if not values:
            return ValidationCheck(
                name=f"filter:{name}",
                status="skipped",
                message=f"Could not find a comparable field for {name} in response items.",
            )
        if not values_match_filter(name, value, values):
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


def filter_field_names(name: str) -> list[str]:
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


def values_match_filter(name: str, expected: str, values: list[str]) -> bool:
    if name.startswith("min"):
        return any((number := to_float(value)) is not None and number >= float(expected) for value in values)
    if name.startswith("max"):
        return any((number := to_float(value)) is not None and number <= float(expected) for value in values)
    if name.startswith("from"):
        return any(value >= expected for value in values)
    if name.startswith("to"):
        return any(value <= expected for value in values)
    if name in {"title", "hostname"}:
        return any(expected.lower() in value.lower() for value in values)
    return any(value.lower() == expected.lower() for value in values)


def to_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def find_values_by_key(data: Any, key: str) -> list[Any]:
    if isinstance(data, dict):
        values = [value for current_key, value in data.items() if current_key.lower() == key.lower()]
        for value in data.values():
            values.extend(find_values_by_key(value, key))
        return values
    if isinstance(data, list):
        values = []
        for item in data:
            values.extend(find_values_by_key(item, key))
        return values
    return []
