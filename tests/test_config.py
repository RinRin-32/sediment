"""Config file lookup order, env overrides, and helpful failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from sediment.config import ConfigError, load_config, redact


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    return cwd, env


def test_user_config_is_used_when_nothing_else(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    user = write(Path(env["XDG_CONFIG_HOME"]) / "sediment/config.toml", '[pebble]\nurl = "u"\n')
    cfg = load_config(env=env, cwd=cwd)
    assert cfg.source == user and cfg.pebble_url == "u"


def test_project_file_beats_user_file(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    write(Path(env["XDG_CONFIG_HOME"]) / "sediment/config.toml", '[pebble]\nurl = "user"\n')
    write(cwd / "sediment.toml", '[pebble]\nurl = "project"\n')
    assert load_config(env=env, cwd=cwd).pebble_url == "project"


@pytest.mark.parametrize("var", ["SEDIMENT_CONFIG", "SEDIAMENT_CONFIG"])
def test_env_named_file_beats_project_file(dirs: tuple[Path, dict[str, str]], var: str) -> None:
    cwd, env = dirs
    write(cwd / "sediment.toml", '[pebble]\nurl = "project"\n')
    named = write(cwd.parent / "named.toml", '[pebble]\nurl = "named"\n')
    cfg = load_config(env={**env, var: str(named)}, cwd=cwd)
    assert cfg.pebble_url == "named"


def test_explicit_beats_everything(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    named = write(cwd.parent / "named.toml", '[pebble]\nurl = "named"\n')
    explicit = write(cwd.parent / "explicit.toml", '[pebble]\nurl = "explicit"\n')
    cfg = load_config(explicit=explicit, env={**env, "SEDIMENT_CONFIG": str(named)}, cwd=cwd)
    assert cfg.pebble_url == "explicit"


def test_pinned_but_missing_file_is_an_error(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(env={**env, "SEDIMENT_CONFIG": "/nope.toml"}, cwd=cwd)


def test_env_overrides_file_fields(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    write(
        cwd / "sediment.toml",
        'repo = "file-repo"\n[pebble]\nurl = "file"\ntoken = "filetok"\n'
        '[model]\nbase_url = "m"\nname = "file-model"\n[skills]\nmax_tokens = 1000\n'
        "[tools]\nmax_turns = 3\nallow_always = false\n",
    )
    env = {
        **env,
        "SEDIMENT_PEBBLE_URL": "env-url",
        "SEDIMENT_PEBBLE_TOKEN": "envtok",
        "SEDIMENT_MODEL": "env-model",
        "SEDIMENT_MODEL_BASE_URL": "env-base",
        "SEDIMENT_MAX_SKILL_TOKENS": "500",
    }
    cfg = load_config(env=env, cwd=cwd)
    assert (cfg.pebble_url, cfg.pebble_token) == ("env-url", "envtok")
    assert (cfg.model_name, cfg.model_base_url) == ("env-model", "env-base")
    assert cfg.max_skill_tokens == 500
    assert cfg.repo == "file-repo"
    assert cfg.tools.max_turns == 3 and cfg.tools.allow_always is False


def test_missing_settings_message_is_helpful(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    cfg = load_config(env=env, cwd=cwd)
    with pytest.raises(ConfigError) as info:
        cfg.require_pebble()
    message = str(info.value)
    assert "SEDIMENT_PEBBLE_URL" in message and "SEDIMENT_PEBBLE_TOKEN" in message
    assert "sediment.toml" in message  # tells you where it looked
    assert "[pebble]" in message  # and shows an example


def test_bad_toml_and_bad_types(dirs: tuple[Path, dict[str, str]]) -> None:
    cwd, env = dirs
    write(cwd / "sediment.toml", "[pebble\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(env=env, cwd=cwd)
    write(cwd / "sediment.toml", "[tools]\nmax_turns = true\n")
    with pytest.raises(ConfigError, match="max_turns"):
        load_config(env=env, cwd=cwd)
    with pytest.raises(ConfigError, match="integer"):
        load_config(env={**env, "SEDIMENT_MAX_TURNS": "lots"}, cwd=cwd)


def test_redact_never_shows_the_token() -> None:
    token = "pbl_supersecretvalue"
    shown = redact(token)
    assert token not in shown
    assert shown.startswith("pbl_") and "len 20" in shown
    assert redact("") == "(not set)"
