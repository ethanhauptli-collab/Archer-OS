"""Keys saved once (app or `roughcut key set`) work everywhere.

The first Mac setup failed because the key lived in one place (the app's
Keychain entry, or an unreloaded ~/.zshrc) and Terminal looked in another.
macOS's `security` tool is replaced by a fake that keeps a JSON store and logs
its command lines, so we can check the key never appears in argv.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import anthropic  # imported before the fixture pretends to be macOS (urllib then wants _scproxy)
import httpx2
import pytest

from roughcut import keys
from roughcut.cli import main

FAKE_SECURITY = r'''#!{python}
import json, os, sys
store_path = os.environ["FAKE_KEYCHAIN"]
store = json.load(open(store_path)) if os.path.exists(store_path) else {{}}
with open(store_path + ".argv", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")

def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None

def run(args):
    cmd = args[0]
    if cmd == "find-generic-password":
        key = opt(args, "-s") + "/" + opt(args, "-a")
        if key not in store:
            sys.exit(44)
        print(store[key])
    elif cmd == "add-generic-password":
        store[opt(args, "-s") + "/" + opt(args, "-a")] = opt(args, "-w")
    elif cmd == "delete-generic-password":
        key = opt(args, "-s") + "/" + opt(args, "-a")
        if store.pop(key, None) is None:
            sys.exit(44)

if sys.argv[1] == "-i":
    for line in sys.stdin:
        if line.strip():
            run(line.split())
else:
    run(sys.argv[1:])
json.dump(store, open(store_path, "w"))
'''

KEY = "sk-ant-api03-" + "a" * 40


@pytest.fixture
def keychain(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "security"
    fake.write_text(FAKE_SECURITY.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    store = tmp_path / "keychain.json"
    monkeypatch.setenv("FAKE_KEYCHAIN", str(store))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(keys.sys, "platform", "darwin")
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return store


def argv_log(store: Path) -> str:
    p = Path(str(store) + ".argv")
    return p.read_text() if p.exists() else ""


def test_round_trip_never_puts_the_key_on_a_command_line(keychain):
    keys.keychain_set("ANTHROPIC_API_KEY", KEY)
    assert keys.keychain_get("ANTHROPIC_API_KEY") == KEY
    assert json.loads(keychain.read_text()) == {"com.roughcut.app/ANTHROPIC_API_KEY": KEY}
    assert KEY not in argv_log(keychain)
    assert keys.keychain_delete("ANTHROPIC_API_KEY")
    assert keys.keychain_get("ANTHROPIC_API_KEY") is None


def test_rejects_things_that_are_not_keys(keychain):
    with pytest.raises(keys.KeychainError):
        keys.keychain_set("ANTHROPIC_API_KEY", "sk-ant-api03 with spaces; rm -rf")


def test_keychain_fills_the_environment_but_exports_win(keychain, monkeypatch):
    keys.keychain_set("ANTHROPIC_API_KEY", KEY)
    assert keys.load_into_environ() == {"ANTHROPIC_API_KEY": "keychain"}
    assert os.environ["ANTHROPIC_API_KEY"] == KEY
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-from-zshrc-xxxxxxxxxxxx")
    assert keys.load_into_environ()["ANTHROPIC_API_KEY"] == "environment"
    assert os.environ["ANTHROPIC_API_KEY"].endswith("from-zshrc-xxxxxxxxxxxx")


def test_not_on_a_mac_means_no_keychain(monkeypatch):
    monkeypatch.setattr(keys.sys, "platform", "linux")
    assert not keys.keychain_available()
    assert keys.keychain_get("ANTHROPIC_API_KEY") is None


def test_key_set_verifies_then_saves(keychain, monkeypatch, capsys):
    import roughcut.cli as cli

    monkeypatch.setattr("getpass.getpass", lambda prompt: f"  {KEY}\n")
    monkeypatch.setattr(cli, "check_anthropic_key", lambda offline=False, pasted=None: ("ok", ""))
    assert main(["key", "set"]) == 0
    assert "Saved sk-ant-api03" in capsys.readouterr().out
    assert keys.keychain_get("ANTHROPIC_API_KEY") == KEY


def test_key_set_refuses_a_rejected_key(keychain, monkeypatch, capsys):
    import roughcut.cli as cli

    monkeypatch.setattr("getpass.getpass", lambda prompt: KEY)
    monkeypatch.setattr(cli, "check_anthropic_key", lambda offline=False, pasted=None: ("rejected", "Anthropic rejected the key."))
    assert main(["key", "set"]) == 1
    assert "Not saved" in capsys.readouterr().out
    assert keys.keychain_get("ANTHROPIC_API_KEY") is None


def test_key_set_explains_a_subscription_token(keychain, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: "sk-ant-oat01-" + "b" * 40)
    assert main(["key", "set"]) == 1
    assert "sign-in token, not an API key" in capsys.readouterr().out
    assert keys.keychain_get("ANTHROPIC_API_KEY") is None


def test_doctor_and_status_find_a_keychain_key(keychain, capsys, monkeypatch):
    import roughcut.cli as cli

    keys.keychain_set("ANTHROPIC_API_KEY", KEY)
    assert main(["doctor", "--json", "--offline"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["anthropic_key"] is True and report["anthropic_key_source"] == "keychain"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "check_anthropic_key", lambda offline=False, pasted=None: ("ok", ""))
    assert main(["key", "status"]) == 0
    out = capsys.readouterr().out
    assert "from the keychain" in out and "accepted" in out


def test_set_warns_when_an_old_key_is_exported(keychain, monkeypatch, capsys):
    import roughcut.cli as cli

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-old-old-old-old-old-old-old")
    monkeypatch.setattr("getpass.getpass", lambda prompt: KEY)
    monkeypatch.setattr(cli, "check_anthropic_key", lambda offline=False, pasted=None: ("ok", ""))
    assert main(["key", "set"]) == 0
    assert "a different ANTHROPIC_API_KEY is exported" in capsys.readouterr().out


def test_remove_does_not_invent_an_export(keychain, capsys):
    keys.keychain_set("ANTHROPIC_API_KEY", KEY)
    assert main(["key", "remove"]) == 0
    out = capsys.readouterr().out
    assert "Removed" in out and "still exported" not in out


# A new key saved, but runs still rejected: the second Mac setup.


def _zshrc(tmp_path, monkeypatch, line="export ANTHROPIC_API_KEY=sk-ant-api03-old-old-old-old-old-old-old\n"):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("alias ll='ls -l'\n" + line)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_rejection_names_the_export_that_hides_the_saved_key(keychain, tmp_path, monkeypatch):
    from roughcut.providers.anthropic_provider import key_problem_hint

    _zshrc(tmp_path, monkeypatch)
    keys.keychain_set("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-old-old-old-old-old-old-old")
    hint = key_problem_hint()
    assert "~/.zshrc is overriding the key you saved" in hint
    assert "sed -i '' '/ANTHROPIC_API_KEY/d' ~/.zshrc;" in hint and "unset ANTHROPIC_API_KEY" in hint
    # Same key in both places (the Keychain filled the environment): nothing to blame.
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert "overriding" not in key_problem_hint()


def test_set_names_the_file_with_the_old_export(keychain, tmp_path, monkeypatch, capsys):
    import roughcut.cli as cli

    _zshrc(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-old-old-old-old-old-old-old")
    monkeypatch.setattr("getpass.getpass", lambda prompt: KEY)
    checked = []
    monkeypatch.setattr(cli, "check_anthropic_key", lambda offline=False, pasted=None: checked.append(pasted) or ("ok", ""))
    assert main(["key", "set"]) == 0
    out = capsys.readouterr().out
    assert checked == [KEY]  # the pasted key is checked, not the exported one
    assert "~/.zshrc is overriding" in out
    assert os.environ["ANTHROPIC_API_KEY"].endswith("old-old")  # we don't pretend the shell changed


@pytest.mark.parametrize("pasted", ["sk-ant-api03-Hd2...wAA", "sk-ant-api03-Hd2…wAA", "sk-ant-api03-****wAA"])
def test_set_explains_a_shortened_key_without_calling_anthropic(keychain, monkeypatch, capsys, pasted):
    import roughcut.cli as cli

    monkeypatch.setattr("getpass.getpass", lambda prompt: pasted)
    monkeypatch.setattr(cli, "check_anthropic_key", lambda **kw: pytest.fail("should not call Anthropic"))
    assert main(["key", "set"]) == 1
    assert "shortened key" in capsys.readouterr().out
    assert keys.keychain_get("ANTHROPIC_API_KEY") is None


def test_pasted_key_rejection_talks_about_the_pasted_key(keychain, tmp_path, monkeypatch):
    from roughcut.cli import check_anthropic_key

    seen = []

    def handler(request):
        seen.append(request.headers.get("x-api-key"))
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    real = anthropic.Anthropic

    def fake(**kw):
        kw.update(base_url="https://api.anthropic.test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
        return real(**kw)

    monkeypatch.setattr(anthropic, "Anthropic", fake)
    _zshrc(tmp_path, monkeypatch)
    keys.keychain_set("ANTHROPIC_API_KEY", "sk-ant-api03-saved-saved-saved-saved-saved")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-old-old-old-old-old-old-old")
    status, message = check_anthropic_key(pasted=KEY)
    assert seen == [KEY]
    assert status == "rejected"
    assert "the key you pasted" in message and "invalid x-api-key" in message and "overriding" not in message
