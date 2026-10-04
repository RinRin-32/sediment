"""Configuration: one TOML file plus environment overrides.

Lookup order for the file (first hit wins; nothing is merged across files):
  1. an explicit `--config PATH`
  2. $SEDIMENT_CONFIG (the brief spelled it $SEDIAMENT_CONFIG; that is honoured too)
  3. ./sediment.toml (project-local)
  4. $XDG_CONFIG_HOME/sediment/config.toml (default ~/.config/sediment/config.toml)

Environment variables override individual fields from whichever file loaded.
Missing settings are only an error for the commands that need them, and the
error says where we looked and what to set; no traceback.

Tokens are secrets. Nothing in sediment prints one; use `redact()` for logging.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

DEFAULT_MAX_SKILL_TOKENS = 30000

T = TypeVar("T")


class ConfigError(Exception):
    """A configuration problem the user can fix; the message says how."""


@dataclass(frozen=True)
class PebbleSettings:
    url: str
    token: str


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str = ""


@dataclass(frozen=True)
class ToolPolicy:
    """Knobs for the local agent loop. Approvals themselves cannot be turned off."""

    max_turns: int = 20
    command_timeout: int = 120
    max_output_chars: int = 20000
    allow_always: bool = True


@dataclass(frozen=True)
class Config:
    pebble_url: str = ""
    pebble_token: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    repo: str = ""
    max_skill_tokens: int = DEFAULT_MAX_SKILL_TOKENS
    report_skill_use: bool = True
    cache_dir: Path = field(default_factory=lambda: default_cache_dir(os.environ))
    tools: ToolPolicy = field(default_factory=ToolPolicy)
    source: Path | None = None
    searched: tuple[Path, ...] = ()

    def require_pebble(self) -> PebbleSettings:
        missing = [
            name
            for name, value in (
                ("pebble.url (SEDIMENT_PEBBLE_URL)", self.pebble_url),
                ("pebble.token (SEDIMENT_PEBBLE_TOKEN)", self.pebble_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(self._missing_message(missing))
        return PebbleSettings(url=self.pebble_url.rstrip("/"), token=self.pebble_token)

    def require_model(self) -> ModelSettings:
        missing = [
            name
            for name, value in (
                ("model.base_url (SEDIMENT_MODEL_BASE_URL)", self.model_base_url),
                ("model.name (SEDIMENT_MODEL)", self.model_name),
            )
            if not value
        ]
        if missing:
            raise ConfigError(self._missing_message(missing))
        return ModelSettings(
            base_url=self.model_base_url.rstrip("/"),
            model=self.model_name,
            api_key=self.model_api_key,
        )

    def _missing_message(self, missing: list[str]) -> str:
        where = (
            f"loaded {self.source}"
            if self.source
            else "no config file found; looked in: " + ", ".join(str(p) for p in self.searched)
        )
        return (
            "sediment is missing required settings: "
            + "; ".join(missing)
            + f"\n  ({where})\n"
            + "  Minimal config.toml:\n"
            + '    [pebble]\n    url = "https://pebble.example"\n    token = "..."\n'
            + '    [model]\n    base_url = "http://localhost:8000/v1"\n    name = "your-model"\n'
        )


def redact(token: str) -> str:
    """Safe-to-log description of a secret: a short prefix and the length only."""
    if not token:
        return "(not set)"
    return f"{token[:4]}... (len {len(token)})"


def default_cache_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sediment"


def candidate_paths(env: Mapping[str, str], cwd: Path, explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit]
    named = env.get("SEDIMENT_CONFIG") or env.get("SEDIAMENT_CONFIG")
    if named:
        return [Path(named).expanduser()]
    xdg = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [cwd / "sediment.toml", Path(xdg) / "sediment" / "config.toml"]


def load_config(
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Config:
    """Find, parse and env-override the configuration. Raises ConfigError only on bad input."""
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    candidates = candidate_paths(env, cwd, explicit)
    pinned = explicit is not None or bool(
        env.get("SEDIMENT_CONFIG") or env.get("SEDIAMENT_CONFIG")
    )

    source: Path | None = None
    data: dict[str, object] = {}
    for path in candidates:
        if path.is_file():
            source = path
            data = _read_toml(path)
            break
    if source is None and pinned:
        raise ConfigError(f"config file {candidates[0]} does not exist.")

    pebble = _table(data, "pebble", source)
    model = _table(data, "model", source)
    skills = _table(data, "skills", source)
    tools = _table(data, "tools", source)

    def pick_str(env_key: str, table: dict[str, object], key: str) -> str:
        if env.get(env_key):
            return env[env_key]
        return _expect(table, key, str, source, "")

    def pick_int(env_key: str, table: dict[str, object], key: str, default: int) -> int:
        raw = env.get(env_key)
        if raw:
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{env_key} must be an integer, got {raw!r}") from exc
        return _expect(table, key, int, source, default)

    cache_raw = env.get("SEDIMENT_CACHE_DIR") or _expect(skills, "cache_dir", str, source, "")
    policy = ToolPolicy(
        max_turns=pick_int("SEDIMENT_MAX_TURNS", tools, "max_turns", ToolPolicy.max_turns),
        command_timeout=_expect(tools, "command_timeout", int, source, ToolPolicy.command_timeout),
        max_output_chars=_expect(
            tools, "max_output_chars", int, source, ToolPolicy.max_output_chars
        ),
        allow_always=_expect(tools, "allow_always", bool, source, ToolPolicy.allow_always),
    )
    if policy.max_turns < 1:
        raise ConfigError("tools.max_turns must be at least 1")

    return Config(
        pebble_url=pick_str("SEDIMENT_PEBBLE_URL", pebble, "url"),
        pebble_token=pick_str("SEDIMENT_PEBBLE_TOKEN", pebble, "token"),
        model_base_url=pick_str("SEDIMENT_MODEL_BASE_URL", model, "base_url"),
        model_api_key=pick_str("SEDIMENT_MODEL_API_KEY", model, "api_key"),
        model_name=pick_str("SEDIMENT_MODEL", model, "name"),
        repo=env.get("SEDIMENT_REPO") or _expect(data, "repo", str, source, ""),
        max_skill_tokens=pick_int(
            "SEDIMENT_MAX_SKILL_TOKENS", skills, "max_tokens", DEFAULT_MAX_SKILL_TOKENS
        ),
        report_skill_use=_expect(skills, "report", bool, source, True),
        cache_dir=Path(cache_raw).expanduser() if cache_raw else default_cache_dir(env),
        tools=policy,
        source=source,
        searched=tuple(candidates),
    )


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _table(data: dict[str, object], key: str, source: Path | None) -> dict[str, object]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: [{key}] must be a table")
    return value


def _expect(
    table: Mapping[str, object], key: str, kind: type[T], source: Path | None, default: T
) -> T:
    value = table.get(key)
    if value is None:
        return default
    # bool is an int subclass; don't let `max_turns = true` slip through as 1.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{source}: '{key}' must be {kind.__name__}, got {value!r}")
    return value
