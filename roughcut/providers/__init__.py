"""Pick a model provider by name."""

from __future__ import annotations

import os

from .base import ImagePart, Part, Provider, ProviderAuthError, ProviderError, TextPart, Usage, UsageLog, extract_json

PROVIDERS = ("anthropic", "openai", "ollama", "openai-compatible", "none")

# Published per-million-token prices for cost estimates in the report.
# (input, output, cache read); cache writes bill at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}


def make_provider(
    name: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    effort: str | None = None,
    vision: bool | None = None,
) -> Provider | None:
    if name == "none":
        return None
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model, effort=effort)

    from .openai_compat import OpenAICompatProvider

    if name == "openai":
        key = os.environ.get(api_key_env or "OPENAI_API_KEY")
        return OpenAICompatProvider(model or "", name="openai", base_url=base_url, api_key=key, supports_vision=vision is not False)
    if name == "ollama":
        return OpenAICompatProvider(
            model or "",
            name="ollama",
            base_url=base_url or "http://localhost:11434/v1",
            api_key="ollama",
            supports_vision=bool(vision),
        )
    if name == "openai-compatible":
        if not base_url:
            raise ProviderError("--base-url is required for the openai-compatible provider")
        # Local servers (LM Studio, llama.cpp) accept any key; the client just needs one.
        key = (os.environ.get(api_key_env) if api_key_env else None) or "none"
        return OpenAICompatProvider(model or "", name="openai-compatible", base_url=base_url, api_key=key, supports_vision=bool(vision))
    raise ProviderError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")


def estimate_cost(usage: UsageLog) -> float | None:
    total = 0.0
    priced = False
    for call in usage.calls:
        price = PRICES.get(call.model)
        if price is None:
            continue
        priced = True
        inp, out, cache_read = price
        total += call.input_tokens * inp / 1e6
        total += call.cache_write_tokens * inp * 1.25 / 1e6
        total += call.cache_read_tokens * cache_read / 1e6
        total += call.output_tokens * out / 1e6
    return total if priced else None


__all__ = [
    "ImagePart",
    "Part",
    "Provider",
    "ProviderAuthError",
    "ProviderError",
    "TextPart",
    "Usage",
    "UsageLog",
    "extract_json",
    "make_provider",
    "estimate_cost",
    "PROVIDERS",
]
