"""Argus configuration.

Resolution order per parameter:
  1. TOML file at ARGUS_CONFIG_PATH (or the default path if unset)
  2. Environment variable
  3. Coded default, or ValueError for required fields
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DEFAULT_CONFIG_PATH = Path("/etc/argus/config.toml")


@dataclass(frozen=True)
class ClientConfig:
    provider: str
    model: str


@dataclass(frozen=True)
class ArgusConfig:
    # [argus]
    wiki_root: Path
    bot_username: str
    review_drafts: bool = False
    acknowledge_events: bool = True

    # [limits]
    max_plans: int = 5
    max_inline_suggestions: int = 10
    max_search_results: int = 20
    max_file_window_lines: int = 200
    thread_compact_threshold: int = 10
    preload_diff_threshold: int = 300

    # [review]
    verdict_threshold: Literal["critical", "high", "medium"] = "high"

    # [clients.*]
    fast_client: ClientConfig = ClientConfig("anthropic", "claude-haiku-4-5-20251001")
    standard_client: ClientConfig = ClientConfig("anthropic", "claude-sonnet-4-6")
    deep_client: ClientConfig = ClientConfig("anthropic", "claude-opus-4-7")


def load_config(path: Path | None = None) -> ArgusConfig:
    """Load configuration from TOML file with env-var and default fallbacks."""
    raw: dict = {}

    if path is None:
        env_path = os.environ.get("ARGUS_CONFIG_PATH")
        path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH

    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    argus = raw.get("argus", {})
    limits = raw.get("limits", {})
    review = raw.get("review", {})
    clients = raw.get("clients", {})

    wiki_root_str = argus.get("wiki_root") or os.environ.get("ARGUS_WIKI_ROOT")
    if not wiki_root_str:
        raise ValueError(
            "wiki_root is required — set [argus] wiki_root in config or ARGUS_WIKI_ROOT env"
        )

    bot_username = argus.get("bot_username") or os.environ.get("ARGUS_BOT_USERNAME")
    if not bot_username:
        raise ValueError(
            "bot_username is required — set [argus] bot_username in config or ARGUS_BOT_USERNAME env"
        )

    return ArgusConfig(
        wiki_root=Path(wiki_root_str),
        bot_username=bot_username,
        review_drafts=argus.get("review_drafts", False),
        acknowledge_events=argus.get("acknowledge_events", True),
        max_plans=limits.get("max_plans", 5),
        max_inline_suggestions=limits.get("max_inline_suggestions", 10),
        max_search_results=limits.get("max_search_results", 20),
        max_file_window_lines=limits.get("max_file_window_lines", 200),
        thread_compact_threshold=limits.get("thread_compact_threshold", 10),
        preload_diff_threshold=limits.get("preload_diff_threshold", 300),
        verdict_threshold=review.get("verdict_threshold", "high"),
        fast_client=_parse_client(
            clients.get("fast", {}), "anthropic", "claude-haiku-4-5-20251001"
        ),
        standard_client=_parse_client(
            clients.get("standard", {}), "anthropic", "claude-sonnet-4-6"
        ),
        deep_client=_parse_client(
            clients.get("deep", {}), "anthropic", "claude-opus-4-7"
        ),
    )


def _parse_client(raw: dict, default_provider: str, default_model: str) -> ClientConfig:
    return ClientConfig(
        provider=raw.get("provider", default_provider),
        model=raw.get("model", default_model),
    )
