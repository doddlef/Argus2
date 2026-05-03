"""Tests for agent_core.config.load_config.

Priority under test: env > TOML > coded default.
"""

from pathlib import Path

import pytest

from agent_core.config import ArgusConfig, ClientConfig, load_config


# ---------------------------------------------------------------------------
# Fixture: minimal env vars that satisfy all required fields
# ---------------------------------------------------------------------------

@pytest.fixture
def required_env(monkeypatch):
    monkeypatch.setenv("ARGUS_WIKI_ROOT", "/tmp/wiki")
    monkeypatch.setenv("ARGUS_BOT_USERNAME", "argus-bot")
    monkeypatch.setenv("ARGUS_FAST_API_KEY", "fast-key")
    monkeypatch.setenv("ARGUS_STANDARD_API_KEY", "standard-key")
    monkeypatch.setenv("ARGUS_DEEP_API_KEY", "deep-key")


# ---------------------------------------------------------------------------
# Required fields — missing raises ValueError
# ---------------------------------------------------------------------------

def test_missing_wiki_root_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_WIKI_ROOT", raising=False)
    with pytest.raises(ValueError, match="wiki_root"):
        load_config(tmp_path / "none.toml")


def test_missing_bot_username_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WIKI_ROOT", "/tmp/wiki")
    monkeypatch.delenv("ARGUS_BOT_USERNAME", raising=False)
    with pytest.raises(ValueError, match="bot_username"):
        load_config(tmp_path / "none.toml")


def test_missing_fast_api_key_raises(required_env, monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_FAST_API_KEY")
    with pytest.raises(ValueError, match="clients.fast"):
        load_config(tmp_path / "none.toml")


def test_missing_standard_api_key_raises(required_env, monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_STANDARD_API_KEY")
    with pytest.raises(ValueError, match="clients.standard"):
        load_config(tmp_path / "none.toml")


def test_missing_deep_api_key_raises(required_env, monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_DEEP_API_KEY")
    with pytest.raises(ValueError, match="clients.deep"):
        load_config(tmp_path / "none.toml")


# ---------------------------------------------------------------------------
# Priority: env > TOML > default
# ---------------------------------------------------------------------------

def test_env_wins_over_toml_for_wiki_root(required_env, monkeypatch, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b'[argus]\nwiki_root = "/toml/wiki"\n')
    monkeypatch.setenv("ARGUS_WIKI_ROOT", "/env/wiki")
    cfg = load_config(toml_path)
    assert cfg.wiki_root == Path("/env/wiki")


def test_toml_wins_over_default_for_max_plans(required_env, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b"[limits]\nmax_plans = 3\n")
    cfg = load_config(toml_path)
    assert cfg.max_plans == 3


def test_default_used_when_absent_from_both(required_env, tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.max_plans == 5
    assert cfg.max_inline_suggestions == 10
    assert cfg.verdict_threshold == "high"


def test_env_wins_over_toml_for_client_api_key(required_env, monkeypatch, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b"[clients.fast]\napi_key = 'toml-key'\n")
    monkeypatch.setenv("ARGUS_FAST_API_KEY", "env-key")
    cfg = load_config(toml_path)
    assert cfg.fast_client.api_key == "env-key"


def test_toml_api_key_used_when_no_env(required_env, monkeypatch, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b"[clients.fast]\napi_key = 'toml-key'\n")
    monkeypatch.delenv("ARGUS_FAST_API_KEY")
    cfg = load_config(toml_path)
    assert cfg.fast_client.api_key == "toml-key"


def test_toml_model_wins_over_default(required_env, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b"[clients.fast]\nmodel = 'claude-haiku-custom'\n")
    cfg = load_config(toml_path)
    assert cfg.fast_client.model == "claude-haiku-custom"


def test_env_model_wins_over_toml(required_env, monkeypatch, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b"[clients.fast]\nmodel = 'claude-haiku-toml'\n")
    monkeypatch.setenv("ARGUS_FAST_MODEL", "claude-haiku-env")
    cfg = load_config(toml_path)
    assert cfg.fast_client.model == "claude-haiku-env"


# ---------------------------------------------------------------------------
# Type coercion for env-sourced values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", ["true", "True", "1", "yes"])
def test_bool_env_truthy(required_env, monkeypatch, tmp_path, val):
    monkeypatch.setenv("ARGUS_REVIEW_DRAFTS", val)
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.review_drafts is True


@pytest.mark.parametrize("val", ["false", "False", "0", "no"])
def test_bool_env_falsy(required_env, monkeypatch, tmp_path, val):
    monkeypatch.setenv("ARGUS_REVIEW_DRAFTS", val)
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.review_drafts is False


def test_int_env_coercion(required_env, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_MAX_PLANS", "8")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.max_plans == 8


# ---------------------------------------------------------------------------
# TOML bool values are passed through without cast
# ---------------------------------------------------------------------------

def test_toml_bool_false_not_overridden_by_default(required_env, tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_bytes(b"[argus]\nacknowledge_events = false\n")
    cfg = load_config(toml_path)
    assert cfg.acknowledge_events is False


# ---------------------------------------------------------------------------
# Client defaults
# ---------------------------------------------------------------------------

def test_client_model_defaults(required_env, tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.fast_client.model == "claude-haiku-4-5-20251001"
    assert cfg.standard_client.model == "claude-sonnet-4-6"
    assert cfg.deep_client.model == "claude-opus-4-7"


def test_client_provider_default(required_env, tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.fast_client.provider == "anthropic"


def test_all_three_api_keys_resolved(required_env, tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.fast_client.api_key == "fast-key"
    assert cfg.standard_client.api_key == "standard-key"
    assert cfg.deep_client.api_key == "deep-key"


def test_ollama_provider_allows_missing_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WIKI_ROOT", "/tmp/wiki")
    monkeypatch.setenv("ARGUS_BOT_USERNAME", "argus-bot")
    monkeypatch.setenv("ARGUS_FAST_PROVIDER", "ollama")
    monkeypatch.delenv("ARGUS_FAST_API_KEY", raising=False)
    monkeypatch.setenv("ARGUS_STANDARD_API_KEY", "standard-key")
    monkeypatch.setenv("ARGUS_DEEP_API_KEY", "deep-key")

    cfg = load_config(tmp_path / "none.toml")
    assert cfg.fast_client.provider == "ollama"
    assert cfg.fast_client.api_key == ""


def test_openrouter_provider_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_WIKI_ROOT", "/tmp/wiki")
    monkeypatch.setenv("ARGUS_BOT_USERNAME", "argus-bot")
    monkeypatch.setenv("ARGUS_FAST_PROVIDER", "openrouter")
    monkeypatch.delenv("ARGUS_FAST_API_KEY", raising=False)
    monkeypatch.setenv("ARGUS_STANDARD_API_KEY", "standard-key")
    monkeypatch.setenv("ARGUS_DEEP_API_KEY", "deep-key")

    with pytest.raises(ValueError, match="clients.fast"):
        load_config(tmp_path / "none.toml")
