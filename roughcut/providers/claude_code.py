"""Claude through Claude Code (`claude -p`), so runs count against your Claude subscription.

Anthropic's terms let you sign the unmodified Claude Code program in with
your own Pro/Max plan and run it from your own scripts; calling the API
directly with subscription credentials is not allowed. So this provider
never touches tokens: it hands each request to `claude -p` and reads back
the structured result.

Claude Code uses an API key whenever one is set, which would bill API credit
instead of the plan, so keys are removed from its environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .anthropic_provider import resolve_model
from .base import ImagePart, Part, ProviderAuthError, ProviderError, TextPart, Usage, UsageLog, extract_json

SIGN_IN_HELP = (
    "Sign Claude Code in with your subscription: run `claude` in Terminal and type /login "
    "(or run `claude setup-token` for a long-lived token)."
)
INSTALL_HELP = "Claude Code isn't installed. Install it (npm install -g @anthropic-ai/claude-code), then sign in with your subscription."
_HIDDEN_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_EXTENSIONS = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def find_claude() -> str | None:
    home = Path.home()
    for candidate in (
        shutil.which("claude"),
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def subscription_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _HIDDEN_KEYS:
        env.pop(key, None)
    return env


def auth_status(binary: str, run=subprocess.run) -> dict:
    """`claude auth status --json`: {"loggedIn": bool, "authMethod": "claude.ai" | "oauth_token" | "api_key" ...}."""
    try:
        proc = run([binary, "auth", "status", "--json"], capture_output=True, text=True, env=subscription_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProviderError(f"Couldn't run Claude Code ({e}).") from e
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


class ClaudeCodeProvider:
    name = "claude-code"
    supports_vision = True

    def __init__(self, model: str | None = None, effort: str | None = "high", *, binary: str | None = None, run=subprocess.run, timeout: float = 1800):
        self.binary = binary or find_claude()
        if not self.binary:
            raise ProviderError(INSTALL_HELP)
        self.model = resolve_model(model)
        self.effort = effort
        self._run = run
        self.timeout = timeout
        self.usage = UsageLog()

    def verify(self) -> str:
        info = auth_status(self.binary, self._run)
        if not info.get("loggedIn"):
            raise ProviderAuthError(f"Claude Code isn't signed in. {SIGN_IN_HELP}")
        if info.get("authMethod") == "api_key":
            raise ProviderAuthError(
                "Claude Code is signed in with an API key, so this would bill API credit, not your subscription. "
                f"Run `claude auth logout` first. {SIGN_IN_HELP}"
            )
        return f"{self.model} via Claude Code"

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
        with tempfile.TemporaryDirectory(prefix="roughcut-") as tmp:
            chunks: list[str] = []
            images = 0
            for p in parts:
                if isinstance(p, TextPart):
                    chunks.append(p.text)
                elif isinstance(p, ImagePart):
                    path = Path(tmp) / f"image-{images:03d}{_EXTENSIONS.get(p.media_type, '.jpg')}"
                    path.write_bytes(p.data)
                    chunks.append(f"[image: {path}]")
                    images += 1
            prompt = "\n\n".join(chunks)
            if images:
                prompt += "\n\nEach [image: ...] line is a picture on disk. Look at every one with the Read tool before answering."
            cmd = [
                self.binary, "-p",
                "--output-format", "json",
                "--json-schema", json.dumps(schema),
                "--model", self.model,
                "--system-prompt", system,
                "--no-session-persistence",
                "--strict-mcp-config",
                "--tools", "Read" if images else "",
            ]
            if images:
                cmd += ["--add-dir", tmp, "--allowedTools", "Read"]
            if effort or self.effort:
                cmd += ["--effort", effort or self.effort]
            t0 = time.monotonic()
            try:
                proc = self._run(cmd, input=prompt, capture_output=True, text=True, env=subscription_env(), cwd=tmp, timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                raise ProviderError(f"Claude Code took longer than {self.timeout / 60:.0f} minutes on the {purpose} request.") from e
            except OSError as e:
                raise ProviderError(f"Couldn't run Claude Code ({e}).") from e

        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {}
        if proc.returncode != 0 or data.get("is_error") or not data:
            message = str(data.get("result") or proc.stderr or out or f"exit code {proc.returncode}").strip()[:500]
            lowered = message.lower()
            if any(k in lowered for k in ("/login", "not logged in", "log in", "authentication", "oauth token", "invalid api key")):
                raise ProviderAuthError(f"Claude Code couldn't use your account: {message} {SIGN_IN_HELP}")
            if "limit" in lowered:
                raise ProviderError(f"Your Claude plan's usage limit was reached during the {purpose} request: {message}")
            raise ProviderError(f"Claude Code failed on the {purpose} request: {message}")

        u = data.get("usage") or {}
        self.usage.add(
            Usage(
                model=f"{self.model} (subscription)",  # not API-billed, so no cost estimate
                purpose=purpose,
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
                seconds=time.monotonic() - t0,
            )
        )
        structured = data.get("structured_output")
        if isinstance(structured, dict):
            return structured
        try:
            return extract_json(str(data.get("result") or ""))
        except ValueError as e:
            raise ProviderError(f"{purpose}: Claude Code returned no JSON ({e}).") from e
