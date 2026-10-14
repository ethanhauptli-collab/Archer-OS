"""A pytest plugin that makes the machine running the tests look like a lived-in Mac.

Loaded with `-p mac_host` (and `tests/` on PYTHONPATH) by `test_hermetic.py`, which runs a few tests under it and
checks they notice nothing. `$MAC_HOST_DIR` is the stand-in Mac's folder, made by the caller:

  * `sys.platform` is "darwin", so the Keychain, `caffeinate` and the Mac paths are in play;
  * `bin/` (first on PATH) holds a `security` with an API key saved for the app's Keychain service, a `claude`
    that is signed in with a subscription, and `caffeinate`, `osascript` and `open`; each logs a line to
    `$MAC_HOST_DIR/host.log` when it actually runs, so an empty log means no test reached the machine;
  * the caller sets HOME, exported keys, a proxy, Roughcut and Venture Studio folders to the stand-in's own.

Nothing here is used by the suite itself.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

# Modules that look at the platform when first imported (urllib asks macOS for its proxy settings).
import anthropic  # noqa: F401
import httpx2  # noqa: F401
import urllib.request  # noqa: F401

HOST = Path(os.environ["MAC_HOST_DIR"])
LOG = HOST / "host.log"
BIN = HOST / "bin"
SAVED_KEY = "sk-ant-api03-" + "h" * 40

_COMMON = f"""import json, os, sys
def note():
    with open({str(LOG)!r}, "a") as log:
        log.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
note()
"""
SECURITY = _COMMON + f"""
store = {{"com.roughcut.app/ANTHROPIC_API_KEY": {SAVED_KEY!r}, "com.roughcut.app/OPENAI_API_KEY": "sk-" + "o" * 40}}
args = sys.argv[1:]
if args[:1] == ["find-generic-password"]:
    key = args[args.index("-s") + 1] + "/" + args[args.index("-a") + 1]
    if key in store:
        print(store[key])
        sys.exit(0)
sys.exit(44)
"""
CLAUDE = _COMMON + """
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    sys.exit(0)
sys.exit(1)
"""
IDLE = _COMMON

BIN.mkdir(parents=True, exist_ok=True)
for _name, _body in (("security", SECURITY), ("claude", CLAUDE), ("caffeinate", IDLE), ("osascript", IDLE), ("open", IDLE)):
    _path = BIN / _name
    _path.write_text(f"#!{sys.executable}\n{_body}")
    _path.chmod(_path.stat().st_mode | stat.S_IEXEC)
os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"
sys.platform = "darwin"


def pytest_report_header(config):
    return f"mac_host: pretending to be a Mac with saved keys and Claude Code in {HOST}"
