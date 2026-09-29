"""Model-provider interface: one JSON-returning call, text and image inputs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol, Union


class ProviderError(RuntimeError):
    pass


@dataclass
class TextPart:
    text: str
    cache: bool = False  # mark as a prompt-cache breakpoint where supported


@dataclass
class ImagePart:
    data: bytes
    media_type: str = "image/jpeg"


Part = Union[TextPart, ImagePart]


@dataclass
class Usage:
    model: str
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0


@dataclass
class UsageLog:
    calls: list[Usage] = field(default_factory=list)

    def add(self, usage: Usage) -> None:
        self.calls.append(usage)


class Provider(Protocol):
    name: str
    model: str
    supports_vision: bool
    usage: UsageLog

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
    ) -> dict: ...


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a reply that may be fenced or chatty."""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in model reply")
        value = json.loads(body[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model reply JSON is not an object")
    return value
