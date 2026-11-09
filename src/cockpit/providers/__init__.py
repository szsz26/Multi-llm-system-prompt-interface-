"""Provider registry: lane config -> transport adapter."""

from __future__ import annotations

from dataclasses import replace

from ..config import CockpitConfig, ProviderConfig
from ..domain import CockpitError
from ..secrets import SecretResolver
from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .claude_cli import ClaudeCodeCLIAdapter
from .codex_cli import CodexCLIAdapter
from .command_cli import CommandCLIAdapter
from .fake import FakeAdapter
from .gemini import GeminiAdapter
from .grok_cli import GrokCLIAdapter
from .manual import ManualAdapter
from .openai import OpenAIResponsesAdapter
from .openai_compat import OpenAICompatAdapter, PerplexityAdapter, XAIAdapter

API_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIResponsesAdapter,
    "gemini": GeminiAdapter,
    "xai": XAIAdapter,
    "perplexity": PerplexityAdapter,
    "openai_compat": OpenAICompatAdapter,
}

CLI_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "claude-code": ClaudeCodeCLIAdapter,
    "codex": CodexCLIAdapter,
    "grok": GrokCLIAdapter,
    "command": CommandCLIAdapter,
}


def api_config(cfg: ProviderConfig) -> ProviderConfig:
    """The same lane, but over the provider's paid API (used only with explicit approval)."""
    return replace(cfg, transport="api", model=cfg.api_model or cfg.model)


def build_adapter(cfg: ProviderConfig, secrets: SecretResolver, config: CockpitConfig | None = None) -> ProviderAdapter:
    if cfg.transport == "fake" or cfg.kind == "fake":
        return FakeAdapter(cfg, secrets)
    if cfg.transport == "manual":
        return ManualAdapter(cfg, secrets)
    if cfg.transport in CLI_ADAPTERS:
        workdir = config.workdir(cfg.id) if config else None
        return CLI_ADAPTERS[cfg.transport](cfg, secrets, workdir=workdir)  # type: ignore[call-arg]
    if cfg.transport != "api":
        raise CockpitError(f"{cfg.id}: unknown transport {cfg.transport!r}")
    try:
        return API_ADAPTERS[cfg.kind](cfg, secrets)
    except KeyError:
        raise CockpitError(f"{cfg.id}: no API adapter for kind {cfg.kind!r}") from None
