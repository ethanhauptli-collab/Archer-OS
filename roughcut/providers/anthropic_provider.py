"""Claude via the official Anthropic SDK."""

from __future__ import annotations

import base64
import json
import os
import time

from .base import ImagePart, Part, ProviderAuthError, ProviderError, TextPart, Usage, UsageLog

DEFAULT_MODEL = "claude-opus-5-5"
# Short names accepted for --model / --vision-model.
MODEL_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Credentials that start like an API key but can never call the Messages API with x-api-key.
SIGN_IN_TOKENS = ("sk-ant-oat", "sk-ant-ort", "sk-ant-sid")
ADMIN_KEYS = ("sk-ant-admin",)
NOT_API_KEYS = SIGN_IN_TOKENS + ADMIN_KEYS


def _mask(key: str) -> str:
    return f"{key[:12]}…{key[-4:]}" if len(key) > 20 else "(too short)"


def override_note(account: str = "ANTHROPIC_API_KEY") -> str:
    """When an exported key hides a different one saved in the Keychain, say where and how to fix it."""
    from .. import keys

    saved = keys.shadowed_keychain_key(account)
    if not saved:
        return ""
    files = keys.shell_files_setting(account)
    if files:
        fix = " ".join(f"sed -i '' '/{account}/d' {f};" for f in files)
        return (
            f"An older {account} exported in {', '.join(files)} is overriding the key you saved with "
            f"`roughcut key set` ({_mask(saved)}). Remove it with:  {fix} unset {account}  "
            "(or open a new Terminal window after removing it)."
        )
    return (
        f"{account} is set in this Terminal window and is overriding the key you saved with "
        f"`roughcut key set` ({_mask(saved)}). Run `unset {account}` or open a new window."
    )


def _api_said(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict) and body["error"].get("message"):
        return str(body["error"]["message"])
    return str(getattr(e, "message", "") or e)


def key_problem_hint(env: dict | None = None) -> str:
    """Explain the most likely reason Anthropic rejected (or never got) a key."""
    real = env is None
    env = os.environ if env is None else env
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    token = env.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    base = env.get("ANTHROPIC_BASE_URL", "").strip()
    where = "To save one, run `roughcut key set` in Terminal (or use Settings → API Keys in the Mac app); both share it."
    if base and "api.anthropic.com" not in base:
        return (
            f"ANTHROPIC_BASE_URL is set to {base}, so requests aren't going to Anthropic's API. "
            "Unset it (unset ANTHROPIC_BASE_URL) unless you meant to use a proxy."
        )
    if not key and not token:
        return f"No Claude API key is set. Create one at console.anthropic.com → API Keys. {where}"
    shown = key or token
    if shown.startswith(SIGN_IN_TOKENS):
        return (
            f"The key ({_mask(shown)}) is a Claude.ai / Claude Code sign-in token, not an API key. "
            f"Roughcut needs an API key from console.anthropic.com (API usage is billed separately from a Claude subscription). {where}"
        )
    if shown.startswith(ADMIN_KEYS):
        return (
            f"The key ({_mask(shown)}) is an Admin API key: it manages your organization but can't run Claude. "
            f"Create a regular key at console.anthropic.com → API Keys. {where}"
        )
    note = override_note() if real and key else ""
    if note:
        return f"Anthropic rejected the key {_mask(key)}. {note}"
    if key and not key.startswith("sk-ant-"):
        return f"ANTHROPIC_API_KEY ({_mask(key)}) doesn't look like an Anthropic API key; those start with sk-ant-. {where}"
    return (
        f"Anthropic rejected the key {_mask(shown)}. It may be mistyped, revoked, or in a workspace without "
        f"API credit. Check it at console.anthropic.com → API Keys. {where}"
    )


def refused_message(said: str, model: str) -> str:
    return (
        f"Anthropic accepted the key but refused the request ({said}). Check at console.anthropic.com that "
        f"the key's workspace has API credit and can use {model}."
    )


def auth_error(e: Exception, model: str) -> ProviderAuthError:
    """401: the key itself is bad. 403: the key works but isn't allowed this request."""
    said = _api_said(e)
    status = getattr(e, "status_code", None)
    if status == 403 and not override_note():
        err = ProviderAuthError(refused_message(said, model))
    else:
        err = ProviderAuthError(f"{key_problem_hint()} (Anthropic said: {said})")
    err.said, err.status = said, status
    return err


class AnthropicProvider:
    name = "anthropic"
    supports_vision = True

    def __init__(self, model: str | None = None, effort: str | None = "high", client=None, use_fallbacks: bool = True):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - it's a hard dependency
            raise ProviderError("the anthropic package is not installed: pip install anthropic") from e
        self._anthropic = anthropic
        self.model = MODEL_ALIASES.get((model or "").strip().lower(), model) if model else DEFAULT_MODEL
        self.effort = effort
        # An explicit api_key stops the SDK from also sending ANTHROPIC_AUTH_TOKEN
        # (a leftover sign-in token there would get a good key rejected).
        self.client = client or anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None, max_retries=3)
        self.use_fallbacks = use_fallbacks
        self.usage = UsageLog()

    def verify(self) -> str:
        """One free request (no tokens): is the key accepted and the model available?"""
        anthropic = self._anthropic
        try:
            info = self.client.models.retrieve(self.model)
        except TypeError as e:  # the SDK found no credentials at all
            if "authentication" not in str(e).lower():
                raise
            raise ProviderAuthError(key_problem_hint()) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise auth_error(e, self.model) from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"Model {self.model!r} isn't available to this API key. Try --model claude-opus-5-5.") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Couldn't reach the Anthropic API ({e}). Check your internet connection.") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code} while checking the key: {e}") from e
        return getattr(info, "display_name", None) or self.model

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
        if effort and not self.model.startswith("claude-haiku"):  # Haiku 4.5 rejects effort
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
        except TypeError as e:  # the SDK found no credentials at all
            if "authentication" not in str(e).lower():
                raise
            raise ProviderAuthError(key_problem_hint()) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise auth_error(e, self.model) from e
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
