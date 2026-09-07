"""Provider adapters and the factory that builds them from configuration."""

from __future__ import annotations

from model_router.config import ProviderConfig, ProviderKind
from model_router.providers.anthropic import AnthropicProvider
from model_router.providers.base import Provider, ProviderError
from model_router.providers.openai_compat import OpenAICompatProvider


def build_provider(config: ProviderConfig) -> Provider:
    key = config.api_key()
    if config.kind is ProviderKind.ANTHROPIC:
        return AnthropicProvider(
            config.name, config.base_url, api_key=key, timeout_s=config.timeout_s
        )
    return OpenAICompatProvider(
        config.name, config.base_url, api_key=key, timeout_s=config.timeout_s
    )


__all__ = [
    "AnthropicProvider",
    "OpenAICompatProvider",
    "Provider",
    "ProviderError",
    "build_provider",
]
