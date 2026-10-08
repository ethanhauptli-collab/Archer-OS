"""API keys from the environment or the macOS Keychain.

The Mac app stores keys in the login Keychain (service "com.roughcut.app",
account = the environment variable name). The CLI reads the same entries, so
a key saved once works in the app and in Terminal. An exported environment
variable always wins.

macOS's `security` tool does the Keychain work. Keys are written through its
interactive mode on stdin, never on a command line where `ps` could see them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE = "com.roughcut.app"  # must match mac/Sources/Roughcut/Keychain.swift
ACCOUNTS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
_KEY_CHARS = re.compile(r"^[A-Za-z0-9_\-]+$")


class KeychainError(RuntimeError):
    pass


def keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def keychain_get(account: str) -> str | None:
    if not keychain_available():
        return None
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    value = proc.stdout.strip() if proc.returncode == 0 else ""
    return value or None


def keychain_set(account: str, value: str) -> None:
    if not keychain_available():
        raise KeychainError("The macOS Keychain isn't available here; export the variable in your shell profile instead.")
    value = value.strip()
    if not _KEY_CHARS.match(value):
        raise KeychainError("That doesn't look like an API key (unexpected characters or spaces).")
    proc = subprocess.run(
        ["security", "-i"],
        input=f"add-generic-password -U -s {SERVICE} -a {account} -l Roughcut -w {value}\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or keychain_get(account) != value:
        raise KeychainError(f"Couldn't save to the Keychain: {proc.stderr.strip() or 'unknown error'}")


def keychain_delete(account: str) -> bool:
    if not keychain_available():
        return False
    proc = subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account], capture_output=True)
    return proc.returncode == 0


def source_of(account: str) -> str | None:
    """'environment', 'keychain' or None."""
    if os.environ.get(account, "").strip():
        return "environment"
    if keychain_get(account):
        return "keychain"
    return None


SHELL_FILES = (".zshrc", ".zprofile", ".zshenv", ".bash_profile", ".bashrc", ".profile")


def shell_files_setting(account: str) -> list[str]:
    """Shell startup files that mention the variable, as "~/.zshrc" etc."""
    found = []
    for name in SHELL_FILES:
        try:
            if account in (Path.home() / name).read_text(errors="ignore"):
                found.append(f"~/{name}")
        except OSError:
            continue
    return found


def shadowed_keychain_key(account: str) -> str | None:
    """The Keychain key, when a different exported one is overriding it."""
    exported = os.environ.get(account, "").strip()
    saved = keychain_get(account) if exported else None
    return saved if saved and saved != exported else None


def load_into_environ() -> dict[str, str]:
    """Fill unset key variables from the Keychain. Returns {account: source}."""
    sources: dict[str, str] = {}
    for account in ACCOUNTS:
        if os.environ.get(account, "").strip():
            sources[account] = "environment"
            continue
        value = keychain_get(account)
        if value:
            os.environ[account] = value
            sources[account] = "keychain"
    return sources
