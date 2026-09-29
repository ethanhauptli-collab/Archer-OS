"""Claude via the official Anthropic SDK."""

from __future__ import annotations

import base64
import json
import time

from .base import ImagePart, Part, ProviderError, TextPart, Usage, UsageLog

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"
    supports_vision = True

    def __init__(self, model: str | None = None, effort: str | None = "high", client=None, use_fallbacks: bool = True):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - it's a hard dependency
            raise ProviderError("the anthropic package is not installed: pip install anthropic") from e
        self._anthropic = anthropic
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.client = client or anthropic.Anthropic()
        self.use_fallbacks = use_fallbacks
        self.usage = UsageLog()

    def _content(self, parts: list[Part]) -> list[dict]:
        blocks: list[dict] = []
        for p in parts:
            if isinstance(p, TextPart):
                block: dict = {"type": "text", "text": p.text}
                if p.cache:
                    block["cache_control"] = {"type": "ephemeral"}
                blocks.append(block)
            elif isinstance(p, ImagePart):
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": p.media_type,
                            "data": base64.standard_b64encode(p.data).decode("ascii"),
                        },
                    }
                )
        return blocks

    def complete_json(
        self,
        system: str,
        parts: list[Part],
        schema: dict,
        *,
        schema_name: str,
        purpose: str,
        effort: str | None = None,
        max_tokens: int = 64000,
    ) -> dict:
        anthropic = self._anthropic
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        effort = effort or self.effort
        if effort:
            output_config["effort"] = effort
        params = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": self._content(parts)}],
            output_config=output_config,
        )
        if self.use_fallbacks:
            # If the request is declined by a safety classifier, the API re-runs
            # it on Anthropic's recommended fallback model instead of failing.
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        t0 = time.monotonic()
        try:
            with self.client.beta.messages.stream(**params) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise ProviderError("Anthropic rejected the API key. Set ANTHROPIC_API_KEY (or run `ant auth login`).") from e
        except anthropic.BadRequestError as e:
            if self.use_fallbacks and "fallback" in str(e).lower():
                self.use_fallbacks = False
                return self.complete_json(
                    system, parts, schema, schema_name=schema_name, purpose=purpose, effort=effort, max_tokens=max_tokens
                )
            raise ProviderError(f"Anthropic API rejected the request: {e}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Could not reach the Anthropic API: {e}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e}") from e

        u = message.usage
        self.usage.add(
            Usage(
                model=getattr(message, "model", self.model) or self.model,
                purpose=purpose,
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                seconds=time.monotonic() - t0,
            )
        )

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            why = getattr(details, "explanation", None) or "no details"
            raise ProviderError(f"The model declined the {purpose} request ({why}).")
        if message.stop_reason == "max_tokens":
            raise ProviderError(f"The {purpose} response hit max_tokens={max_tokens} before finishing.")
        text = "".join(b.text for b in message.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"{purpose}: model returned invalid JSON: {e}") from e
