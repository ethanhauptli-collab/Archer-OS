"""Checks of the machine the tests run on: skipped unless ROUGHCUT_TESTS_REAL=1.

The rest of the suite runs in a sandbox (tests/hermetic.py) with no Keychain, Claude Code, key or network of the
machine's. These do the opposite, on purpose, to see that `roughcut doctor` works where it will be used:

    ROUGHCUT_TESTS_REAL=1 .venv/bin/python -m pytest tests/test_real_environment.py -v

They read the machine's Keychain and look for its Claude Code, and the second asks Anthropic whether the key it
finds is accepted (one free request). Nothing is written; outputs go in the test's folder. What they assert
holds on any machine (keys present or not, signed in or not): the report is complete, doctor's exit code means
what it says, and no secret is printed.
"""

from __future__ import annotations

import json
import os

import pytest

from roughcut import keys
from roughcut.cli import main

pytestmark = pytest.mark.real_environment

REPORT_KEYS = (
    "version", "python", "executable", "ffmpeg", "ffprobe", "transcriber", "anthropic_key", "anthropic_key_status", "anthropic_key_message",
    "anthropic_key_source", "openai_key", "openai_installed", "claude_code", "claude_code_auth", "graphics", "venture_studio",
)  # fmt: skip
STATUSES = {"ok", "rejected", "missing", "unreachable", "not checked"}


def _secrets() -> list[str]:
    found = [os.environ.get(account, "").strip() for account in keys.ACCOUNTS]
    found += [keys.keychain_get(account) or "" for account in keys.ACCOUNTS]
    return [s for s in found if len(s) > 8]


def _check(report: dict, code: int, printed: str) -> None:
    for key in REPORT_KEYS:
        assert key in report, key
    assert report["anthropic_key_status"] in STATUSES
    assert (code == 0) == bool(report["ffmpeg"] and report["ffprobe"])  # doctor's exit code is about ffmpeg only
    assert report["anthropic_key_source"] in (None, "environment", "keychain")
    assert not [s for s in _secrets() if s in printed], "doctor printed a secret"


def test_doctor_reads_this_machine_without_asking_anthropic(capsys):
    code = main(["doctor", "--json", "--offline"])
    printed = capsys.readouterr().out
    report = json.loads(printed)
    _check(report, code, printed)
    assert report["anthropic_key_status"] in ("missing", "not checked")  # --offline never asks


def test_doctor_asks_anthropic_about_the_key_it_finds(capsys):
    code = main(["doctor", "--json"])
    printed = capsys.readouterr().out
    _check(json.loads(printed), code, printed)
    assert main(["doctor"]) in (0, 1)
    assert "Claude API key" in capsys.readouterr().out
