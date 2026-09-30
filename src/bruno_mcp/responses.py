"""Helpers to locate the item array inside arbitrary API response payloads."""

from __future__ import annotations

from typing import Any

ITEM_ARRAY_KEYS = ("data", "items", "results", "content", "records", "entries")


def extract_response_items(response_data: Any) -> list[Any] | None:
    if isinstance(response_data, list):
        return response_data
    if isinstance(response_data, dict):
        for key in ITEM_ARRAY_KEYS:
            value = response_data.get(key)
            if isinstance(value, list):
                return value
    return None


def response_item_count(response_data: Any) -> int | None:
    items = extract_response_items(response_data)
    return len(items) if items is not None else None
