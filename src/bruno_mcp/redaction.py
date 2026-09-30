"""Secret masking and bounded/redacted preview helpers.

Nothing in this module ever returns a full secret value: only masked
representations, length metadata, or SHA-256 prefixes for correlation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SECRET_KEY_REGEX = re.compile(r"token|secret|password|cookie|authorization|api[-_]?key|jwt|bearer", re.IGNORECASE)

MAX_PREVIEW_LIST_ITEMS = 1
MAX_PREVIEW_DICT_KEYS = 8
MAX_PREVIEW_STRING_CHARS = 120


def mask_value(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:3] + "..." + value[-3:]


def sha_prefix(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def mask_bru_args(args: list[str]) -> list[str]:
    """Mask any secret-looking values in a bru argument list before logging/returning it."""
    masked: list[str] = []
    previous_arg = ""
    for arg in args:
        is_env_var_value = previous_arg == "--env-var" and "=" in arg
        if is_env_var_value or (arg.startswith("--env-var=") and "=" in arg):
            prefix = "" if is_env_var_value else "--env-var="
            raw = arg if is_env_var_value else arg[len("--env-var="):]
            key, _, value = raw.partition("=")
            masked.append(f"{prefix}{key}={mask_value(value)}")
        elif "=" in arg and SECRET_KEY_REGEX.search(arg.split("=", 1)[0]):
            key, value = arg.split("=", 1)
            masked.append(f"{key}={mask_value(value)}")
        else:
            masked.append(arg)
        previous_arg = arg
    return masked


def redacted_or_short_value(key: str, value: Any) -> Any:
    if SECRET_KEY_REGEX.search(str(key)):
        return "<redacted>"
    if isinstance(value, str):
        return value[:MAX_PREVIEW_STRING_CHARS]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return f"<{type(value).__name__}>"


def preview_response_data(response_data: Any, depth: int = 0) -> Any:
    """Return a bounded, secret-redacted preview of a response payload."""
    if response_data is None or isinstance(response_data, bool | int | float):
        return response_data
    if isinstance(response_data, str):
        return response_data[:MAX_PREVIEW_STRING_CHARS]
    if isinstance(response_data, list):
        preview = [preview_response_data(item, depth + 1) for item in response_data[:MAX_PREVIEW_LIST_ITEMS]]
        if len(response_data) > MAX_PREVIEW_LIST_ITEMS:
            preview.append({"_omitted_items": len(response_data) - MAX_PREVIEW_LIST_ITEMS})
        return preview
    if isinstance(response_data, dict):
        if depth >= 2:
            dict_preview = {
                key: redacted_or_short_value(key, value)
                for key, value in list(response_data.items())[:MAX_PREVIEW_DICT_KEYS]
            }
        else:
            dict_preview = {
                key: preview_response_data(value, depth + 1) if not SECRET_KEY_REGEX.search(str(key)) else "<redacted>"
                for key, value in list(response_data.items())[:MAX_PREVIEW_DICT_KEYS]
            }
        if len(response_data) > MAX_PREVIEW_DICT_KEYS:
            dict_preview["_omitted_keys"] = len(response_data) - MAX_PREVIEW_DICT_KEYS
        return dict_preview
    return str(response_data)[:MAX_PREVIEW_STRING_CHARS]
