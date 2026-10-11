"""Pick a model provider by name."""

from __future__ import annotations

import os

from .base import ImagePart, Part, Provider, ProviderAuthError, ProviderError, TextPart, Usage, UsageLog, extract_json

PROVIDERS = ("anthropic", "claude-code", "openai", "ollama", "openai-compatible", "none")

# Published per-million-token prices for cost estimates in the report, matched by
# the longest model-name prefix. (input, output, cache read); cache writes bill at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
# Prompts above this many tokens bill at the second price (Haiku 5.5).
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}


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
    if name == "claude-code":
        from .claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(model=model, effort=effort)

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


def is_subscription(model: str) -> bool:
    """Calls made through Claude Code on a Pro/Max plan: no API bill."""
    return "(subscription)" in model


def price_for(model: str) -> str | None:
    """The PRICES key for a model id (longest prefix: 'claude-opus-5-5' before 'claude-opus-5'), or None."""
    if not model or is_subscription(model):
        return None
    keys = [k for k in PRICES if model == k or model.startswith(k + "-") or model.startswith(k + "@")]
    return max(keys, key=len) if keys else None


def call_cost(call: Usage) -> float | None:
    key = price_for(call.model)
    if key is None:
        return None
    inp, out, cache_read = PRICES[key]
    if key in LONG_PROMPT:
        limit, long_price = LONG_PROMPT[key]
        if call.input_tokens + call.cache_read_tokens + call.cache_write_tokens > limit:
            inp, out, cache_read = long_price
    return (
        call.input_tokens * inp / 1e6
        + call.cache_write_tokens * inp * 1.25 / 1e6
        + call.cache_read_tokens * cache_read / 1e6
        + call.output_tokens * out / 1e6
    )


def estimate_cost(usage: UsageLog) -> float | None:
    costs = [c for c in (call_cost(call) for call in usage.calls) if c is not None]
    return sum(costs) if costs else None


def cost_breakdown(usages: list[UsageLog]) -> dict:
    """What the model calls of a run cost, by step ('visual log', 'edit plan', 'graphics design'...).

    Calls on a Claude subscription and calls to models without a known price
    are counted, not priced, so a total is never silently low.
    """
    calls = [c for u in usages for c in u.calls]
    out = {"calls": len(calls), "dollars": 0.0, "by_purpose": {}, "calls_by_purpose": {}, "subscription_calls": 0, "unpriced_calls": 0}
    for call in calls:
        out["calls_by_purpose"][call.purpose] = out["calls_by_purpose"].get(call.purpose, 0) + 1
        cost = call_cost(call)
        if cost is None:
            out["subscription_calls" if is_subscription(call.model) else "unpriced_calls"] += 1
            continue
        out["dollars"] += cost
        out["by_purpose"][call.purpose] = out["by_purpose"].get(call.purpose, 0.0) + cost
    out["dollars"] = round(out["dollars"], 4)
    out["by_purpose"] = {k: round(v, 4) for k, v in out["by_purpose"].items()}
    return out


def describe_cost(breakdown: dict) -> str | None:
    """'about $1.84 (visual log $0.62, edit plan $1.10)', or the call counts on a subscription."""
    if not breakdown or not breakdown.get("calls"):
        return None
    parts = []
    if breakdown["by_purpose"]:
        steps = ", ".join(f"{k} ${v:.2f}" for k, v in breakdown["by_purpose"].items())
        parts.append(f"about ${breakdown['dollars']:.2f} ({steps})")
    if breakdown["subscription_calls"]:
        steps = ", ".join(f"{k} {v}" for k, v in breakdown["calls_by_purpose"].items())
        n = breakdown["subscription_calls"]
        parts.append(f"{n} request{'s' if n != 1 else ''} on your Claude plan, no API charge ({steps})")
    if breakdown["unpriced_calls"]:
        n = breakdown["unpriced_calls"]
        parts.append(f"{n} call{'s' if n != 1 else ''} to a model with no price on file")
    return "; ".join(parts)


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
    "cost_breakdown",
    "describe_cost",
    "PROVIDERS",
]
