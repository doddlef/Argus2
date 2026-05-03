from __future__ import annotations

import os
from pathlib import Path

from agent_core.config import ClientConfig, load_config
from agent_core.context import ApplicationContext, ClientTier, PRLockManager
from agent_core.dispatcher import Dispatcher
from github_connect import create_app
from llm_framework.client import LLMClient, RetryClient
from llm_framework.providers.anthropic import AnthropicClient
from llm_framework.providers.openrouter import OpenRouterClient
from llm_framework.providers.ollama import OllamaClient


def _build_client(cfg: ClientConfig) -> LLMClient:
    provider = cfg.provider.lower().strip()
    if provider == "anthropic":
        return RetryClient(AnthropicClient(model=cfg.model, api_key=cfg.api_key))
    if provider == "ollama":
        host = os.environ.get("ARGUS_OLLAMA_HOST", "http://localhost:11434")
        return RetryClient(OllamaClient(model=cfg.model, host=host))
    if provider == "openrouter":
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        app_name = os.environ.get("OPENROUTER_APP_NAME")
        app_url = os.environ.get("OPENROUTER_APP_URL")
        return RetryClient(OpenRouterClient(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=base_url,
            app_name=app_name,
            app_url=app_url,
        ))
    raise ValueError(f"Unsupported provider '{cfg.provider}' for model '{cfg.model}'")


cfg = load_config()

clients = ClientTier(
    fast=_build_client(cfg.fast_client),
    standard=_build_client(cfg.standard_client),
    deep=_build_client(cfg.deep_client),
)

app_ctx = ApplicationContext(
    clients=clients,
    wiki_root=Path(cfg.wiki_root),
    pr_lock=PRLockManager(),
    bot_username=cfg.bot_username,
    config=cfg,
)

dispatcher = Dispatcher(app_ctx)
app = create_app(dispatcher, bot_username=cfg.bot_username)
