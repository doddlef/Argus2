"""Argus configuration.

Resolution order per parameter (highest to lowest priority):
  1. Environment variable
  2. TOML file at ARGUS_CONFIG_PATH (or /etc/argus/config.toml if unset)
  3. Coded default

Required fields (wiki_root, bot_username) raise ValueError if absent from both
env and TOML. Per-client api_key is required for providers that need it
(anthropic/openrouter), and optional for ollama.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DEFAULT_CONFIG_PATH = Path("/etc/argus/config.toml")

_DEFAULT_MODELS: dict[str, str] = {
    "fast":     "claude-haiku-4-5-20251001",
    "standard": "claude-sonnet-4-6",
    "deep":     "claude-opus-4-7",
}


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientConfig:
    provider: str
    model: str
    api_key: str


@dataclass(frozen=True)
class ArgusConfig:
    # Required fields (no defaults — must be supplied)
    wiki_root:       Path
    bot_username:    str
    fast_client:     ClientConfig
    standard_client: ClientConfig
    deep_client:     ClientConfig

    # [argus] optional
    review_drafts:      bool = False
    acknowledge_events: bool = True
    structure_sync_enabled: bool = False

    # [limits] optional
    max_plans:                int = 5
    max_inline_suggestions:   int = 10
    max_search_results:       int = 20
    max_file_window_lines:    int = 200
    thread_compact_threshold: int = 10
    preload_diff_threshold:   int = 300

    # [review] optional
    verdict_threshold: Literal["critical", "high", "medium"] = "high"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(path: Path | None = None) -> ArgusConfig:
    """Load configuration with env > TOML > default priority."""
    raw = _load_toml(path)

    def r(section: str, key: str, env_key: str, default=None, cast=None):
        """Resolve one value. cast is applied only to env strings, not TOML/default."""
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return cast(env_val) if cast is not None else env_val
        # Traverse dotted section path (e.g. "clients.fast")
        node = raw
        for part in section.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        toml_val = node.get(key) if isinstance(node, dict) else None
        if toml_val is not None:
            return toml_val
        return default

    wiki_root_str = r("argus", "wiki_root", "ARGUS_WIKI_ROOT")
    if not wiki_root_str:
        raise ValueError(
            "wiki_root is required — "
            "set [argus] wiki_root in config or ARGUS_WIKI_ROOT env"
        )

    bot_username = r("argus", "bot_username", "ARGUS_BOT_USERNAME")
    if not bot_username:
        raise ValueError(
            "bot_username is required — "
            "set [argus] bot_username in config or ARGUS_BOT_USERNAME env"
        )

    return ArgusConfig(
        wiki_root=Path(wiki_root_str),
        bot_username=bot_username,
        fast_client=_load_client(raw, "fast"),
        standard_client=_load_client(raw, "standard"),
        deep_client=_load_client(raw, "deep"),
        review_drafts=r("argus", "review_drafts", "ARGUS_REVIEW_DRAFTS",
                        False, cast=_to_bool),
        acknowledge_events=r("argus", "acknowledge_events", "ARGUS_ACKNOWLEDGE_EVENTS",
                             True, cast=_to_bool),
        structure_sync_enabled=r("argus", "structure_sync_enabled", "ARGUS_STRUCTURE_SYNC_ENABLED",
                                 False, cast=_to_bool),
        max_plans=r("limits", "max_plans", "ARGUS_MAX_PLANS",
                    5, cast=int),
        max_inline_suggestions=r("limits", "max_inline_suggestions", "ARGUS_MAX_INLINE_SUGGESTIONS",
                                 10, cast=int),
        max_search_results=r("limits", "max_search_results", "ARGUS_MAX_SEARCH_RESULTS",
                             20, cast=int),
        max_file_window_lines=r("limits", "max_file_window_lines", "ARGUS_MAX_FILE_WINDOW_LINES",
                                200, cast=int),
        thread_compact_threshold=r("limits", "thread_compact_threshold", "ARGUS_THREAD_COMPACT_THRESHOLD",
                                   10, cast=int),
        preload_diff_threshold=r("limits", "preload_diff_threshold", "ARGUS_PRELOAD_DIFF_THRESHOLD",
                                 300, cast=int),
        verdict_threshold=r("review", "verdict_threshold", "ARGUS_VERDICT_THRESHOLD", "high"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_client(raw: dict, tier: str) -> ClientConfig:
    tier_upper = tier.upper()

    def get(key: str, env_key: str, default=None, cast=None):
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return cast(env_val) if cast is not None else env_val
        toml_val = raw.get("clients", {}).get(tier, {}).get(key)
        if toml_val is not None:
            return toml_val
        return default

    provider = str(get("provider", f"ARGUS_{tier_upper}_PROVIDER", "anthropic")).lower()
    api_key = get("api_key", f"ARGUS_{tier_upper}_API_KEY", "")
    if provider in {"anthropic", "openrouter"} and not api_key:
        raise ValueError(
            f"clients.{tier} api_key is required — "
            f"set [clients.{tier}] api_key in config or ARGUS_{tier_upper}_API_KEY env"
        )

    return ClientConfig(
        provider=provider,
        model=get("model", f"ARGUS_{tier_upper}_MODEL", _DEFAULT_MODELS[tier]),
        api_key=api_key,
    )


def _to_bool(val: str) -> bool:
    return val.lower() in ("1", "true", "yes")


def _load_toml(path: Path | None) -> dict:
    if path is None:
        env_path = os.environ.get("ARGUS_CONFIG_PATH")
        path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}
