"""Any OpenAI-compatible chat endpoint: OpenAI, Ollama, LM Studio, OpenRouter...

Local servers vary in what they support, so this degrades gracefully:
strict JSON schema -> JSON mode -> plain text with the JSON pulled out.
"""

from __future__ import annotations

import base64
import json
import time

from .base import ImagePart, Part, ProviderError, TextPart, Usage, UsageLog, extract_json


class OpenAICompatProvider:
    def __init__(
        self,
        model: str,
        *,
        name: str = "openai",
        base_url: str | None = None,
        api_key: str | None = None,
        supports_vision: bool = True,
        client=None,
    ):
        if not model:
            raise ProviderError(f"--model is required for the {name} provider")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise ProviderError("the openai package is not installed: pip install -e '.[openai]'") from e
            try:
                client = OpenAI(base_url=base_url, api_key=api_key)
            except Exception as e:  # e.g. missing OPENAI_API_KEY
                raise ProviderError(f"{name}: {e}") from e
        self.client = client
        self.name = name
        self.model = model
        self.supports_vision = supports_vision
        self.usage = UsageLog()

    def _content(self, parts: list[Part]):
        if not any(isinstance(p, ImagePart) for p in parts):
            return "\n\n".join(p.text for p in parts if isinstance(p, TextPart))
        blocks = []
        for p in parts:
            if isinstance(p, TextPart):
                blocks.append({"type": "text", "text": p.text})
            elif isinstance(p, ImagePart):
                b64 = base64.standard_b64encode(p.data).decode("ascii")
                blocks.append({"type": "image_url", "image_url": {"url": f"data:{p.media_type};base64,{b64}"}})
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
        content = self._content(parts)
        schema_hint = (
            "\n\nReply with a single JSON object and nothing else. It must match this JSON schema:\n"
            + json.dumps(schema)
        )
        attempts = [
            (system, {"response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}}}),
            (system + schema_hint, {"response_format": {"type": "json_object"}}),
            (system + schema_hint, {}),
        ]
        errors = []
        for sys_text, extra in attempts:
            t0 = time.monotonic()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": sys_text}, {"role": "user", "content": content}],
                    **extra,
                )
            except Exception as e:  # servers disagree on error types; try the next mode
                errors.append(f"{extra.get('response_format', {}).get('type', 'plain')}: {e}")
                continue
            usage = getattr(resp, "usage", None)
            self.usage.add(
                Usage(
                    model=self.model,
                    purpose=purpose,
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    seconds=time.monotonic() - t0,
                )
            )
            text = resp.choices[0].message.content or ""
            try:
                return extract_json(text)
            except ValueError as e:
                errors.append(f"unparseable reply: {e}")
        raise ProviderError(f"{self.name}/{self.model} could not produce the {purpose} JSON: " + " | ".join(errors))
