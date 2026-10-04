"""Small, typed accessors for JSON coming back from servers.

Server responses are untrusted shapes. Rather than passing raw dicts around and
hoping, every read goes through these helpers so a missing or mistyped field
becomes a sensible default (or an explicit None) in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

# Spelled with TypeAlias (not the 3.12 `type` statement) so 3.11 still imports this.
JSON: TypeAlias = "bool | int | float | str | list[JSON] | dict[str, JSON] | None"
JSONObject: TypeAlias = "dict[str, JSON]"


def get_str(obj: Mapping[str, JSON], key: str, default: str = "") -> str:
    value = obj.get(key)
    return value if isinstance(value, str) else default


def get_opt_str(obj: Mapping[str, JSON], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def get_int(obj: Mapping[str, JSON], key: str, default: int = 0) -> int:
    value = obj.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def get_bool(obj: Mapping[str, JSON], key: str, default: bool = False) -> bool:
    value = obj.get(key)
    return value if isinstance(value, bool) else default


def get_str_list(obj: Mapping[str, JSON], key: str) -> list[str]:
    value = obj.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def get_obj(obj: Mapping[str, JSON], key: str) -> JSONObject | None:
    value = obj.get(key)
    return value if isinstance(value, dict) else None


def get_obj_list(obj: Mapping[str, JSON], key: str) -> list[JSONObject]:
    value = obj.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
