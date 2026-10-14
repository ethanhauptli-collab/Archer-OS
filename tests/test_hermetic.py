"""The test suite stays on the machine's sandbox (tests/hermetic.py).

Two failures on the owner's Mac (a saved Keychain key made `doctor` find a key; the same key changed what a
rejection said) started this: the suite had never been told to ignore the Mac it ran on. These tests check the
sandbox's pieces, that the guard refuses what it should (the forbidden calls here are harmless even if it
didn't: reserved addresses, names under .invalid, paths that cannot exist), that a refusal the code under test
swallows still fails the test, and, by running a few tests on a stand-in for a lived-in Mac, that the machine's
Keychain, Claude Code, exported keys, proxy and folders change nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anthropic  # noqa: F401 - imported before a test pretends to be a Mac (urllib then wants _scproxy)
import hermetic
import pytest
from hermetic import REAL_HOME, REPO, HermeticViolation, expecting_violations

HAS_REAL_HOME = bool(REAL_HOME) and REAL_HOME != os.sep


# ------------------------------------------------------------------ the environment


def test_the_environment_the_code_reads_is_scrubbed():
    dirty = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-x", "ANTHROPIC_BASE_URL": "https://example.test", "OPENAI_API_KEY": "sk-x",
        "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDECODE": "1", "ROUGHCUT_CONFIG_DIR": "/somewhere", "ROUGHCUT_STUDIO_DIR": "/studio",
        "ROUGHCUT_CHROMIUM": "/opt/chromium", "ROUGHCUT_TESTS_REAL": "1", "XDG_CACHE_HOME": "/x", "GIT_DIR": "/x", "GITHUB_TOKEN": "t",
        "MY_SERVICE_API_KEY": "k", "DB_PASSWORD": "p", "https_proxy": "http://p:1", "ALL_PROXY": "socks5://p:1", "AWS_PROFILE": "work", "HF_HOME": "/models",
        "PATH": "/usr/bin", "HOME": "/Users/someone", "LANG": "en_US.UTF-8", "PLAYWRIGHT_BROWSERS_PATH": "/pw",
    }  # fmt: skip
    clean = hermetic.scrubbed(dirty)
    assert set(clean) == {"ROUGHCUT_CHROMIUM", "ROUGHCUT_TESTS_REAL", "PATH", "HOME", "LANG", "PLAYWRIGHT_BROWSERS_PATH",
                          "NO_PROXY", "no_proxy", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"}  # fmt: skip
    assert clean["NO_PROXY"] == clean["no_proxy"] == "*"


def test_no_key_proxy_or_setting_of_the_machine_reaches_a_test():
    assert not [k for k in os.environ if k.upper().startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE", "XDG_"))]
    assert not [k for k in os.environ if k.upper().startswith("ROUGHCUT_") and k not in hermetic.KEEP | {"ROUGHCUT_STUDIO_DIR"}]
    assert not [k for k in os.environ if hermetic.SCRUB_PATTERN.search(k) and k.upper() != "NO_PROXY"]
    # Proxies are off, and Python on a Mac is told not to ask the system for its own.
    assert urllib.request.getproxies() == {"no": "*"}
    assert urllib.request.proxy_bypass_environment("api.anthropic.com")


def test_home_caches_presets_fonts_and_studio_are_the_tests_own(hermetic_sandbox):
    from roughcut import brand_fonts, presets, studio
    from roughcut.analyze import default_cache_dir

    home = hermetic_sandbox.home
    assert Path.home() == home and Path(os.path.expanduser("~")) == home
    for folder in (default_cache_dir(), presets.config_dir(), brand_fonts.user_fonts_dir()):
        assert home in folder.parents or hermetic_sandbox.root in folder.parents, folder
    assert studio.studio_dir() == hermetic_sandbox.root / "no-venture-studio" and not studio.studio_dir().exists()
    assert studio.status(offline=True)["installed"] is False
    assert Path(tempfile.gettempdir()) == hermetic_sandbox.tmp
    if HAS_REAL_HOME:
        assert Path(REAL_HOME) not in home.parents and home != Path(REAL_HOME)


def test_the_keychain_and_claude_code_on_the_machine_are_out_of_reach(hermetic_sandbox, monkeypatch):
    from roughcut import keys
    from roughcut.providers import claude_code

    assert Path(shutil.which("security")).parent == hermetic_sandbox.stand_ins
    monkeypatch.setattr(keys.sys, "platform", "darwin")  # a Mac: the Keychain is consulted, and it is empty
    assert keys.keychain_available()
    assert keys.keychain_get("ANTHROPIC_API_KEY") is None and keys.keychain_get("OPENAI_API_KEY") is None
    assert keys.load_into_environ() == {} and "ANTHROPIC_API_KEY" not in os.environ
    assert claude_code.find_claude() is None  # none installed, as far as a test can tell


def test_a_claude_a_test_installed_itself_is_found(tmp_path, monkeypatch):
    from roughcut.providers import claude_code

    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ['PATH']}")
    assert claude_code.find_claude() == str(exe)


def test_the_standins_do_what_the_tools_would(hermetic_sandbox):
    out = subprocess.run(["security", "find-generic-password", "-s", "com.roughcut.app", "-a", "ANTHROPIC_API_KEY", "-w"], capture_output=True, text=True)
    assert out.returncode == 44 and out.stdout == ""  # "item not found", as on a Mac without the entry
    for tool in hermetic.DO_NOTHING:
        assert subprocess.run([tool, "anything"], capture_output=True).returncode == 0
    subprocess.run(["security", "-i"], input="add-generic-password -U -s s -a a -w secret\n", text=True, check=True)
    assert json.loads(hermetic_sandbox.keychain.read_text()) == {"s/a": "secret"}  # per test, inside the sandbox


# ------------------------------------------------------------------ the guard refuses


@pytest.mark.parametrize(
    "host, local",
    [("localhost", True), ("127.0.0.1", True), ("127.9.9.9", True), ("::1", True), ("[::1]", True), ("::ffff:127.0.0.1", True), ("", True), (None, True),
     ("0.0.0.0", True), ("api.anthropic.com", False), ("8.8.8.8", False), ("192.168.1.10", False), ("10.0.0.1", False), ("::ffff:8.8.8.8", False),
     ("localhost.example.com", False), (b"example.org", False)],
)  # fmt: skip
def test_what_counts_as_loopback(host, local):
    assert hermetic.is_local_host(host) is local


def test_the_network_is_refused_but_loopback_works():
    with expecting_violations() as refused:
        with pytest.raises(HermeticViolation):
            socket.create_connection(("hermetic-probe.invalid", 443), timeout=1)
        with pytest.raises(HermeticViolation):
            socket.create_connection(("192.0.2.1", 443), timeout=0.2)  # reserved for documentation: never routed
        with pytest.raises(HermeticViolation):
            socket.getaddrinfo("hermetic-probe.invalid", 80)
        with pytest.raises(HermeticViolation):
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.1", 53))
        with pytest.raises(HermeticViolation):
            socket.socket(socket.AF_UNIX).connect("/var/run/hermetic-probe.sock")
    assert len(refused) == 5 and all("hermetic test violation (network)" in r for r in refused)

    class Hello(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as reply:
            assert reply.read() == b"hello"
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/", timeout=5) as reply:
            assert reply.read() == b"hello"
    finally:
        server.shutdown()
        server.server_close()


def test_a_real_anthropic_call_is_refused_not_made():
    """What `doctor` does with a key: verify it with Anthropic. In a test that is an error, however the key got there."""
    from roughcut.cli import check_anthropic_key

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-" + "a" * 40
    with expecting_violations() as refused:
        status, _message = check_anthropic_key()
    assert status == "unreachable" and refused and all("(network)" in r for r in refused)


def test_programs_other_than_the_local_tools_are_refused(tmp_path):
    with expecting_violations() as refused:
        for cmd in (["/bin/echo", "hi"], ["/usr/bin/security", "list-keychains"], ["sh", "-c", "true"], ["/opt/homebrew/bin/claude", "auth", "status"]):
            with pytest.raises(HermeticViolation):
                subprocess.run(cmd, capture_output=True)
        with pytest.raises(HermeticViolation):
            os.system("true")
        with pytest.raises(HermeticViolation):
            subprocess.Popen(["caffeinate"], executable="/usr/bin/caffeinate")
    assert len(refused) == 6 and all("(program)" in r for r in refused)


def test_the_local_tools_and_the_tests_own_programs_run(tmp_path):
    assert subprocess.run([sys.executable, "-c", "print(40 + 2)"], capture_output=True, text=True).stdout.strip() == "42"
    for tool in ("ffmpeg", "ffprobe", "git"):
        if shutil.which(tool):
            assert subprocess.run([tool, "--version" if tool == "git" else "-version"], capture_output=True).returncode == 0
    mine = tmp_path / "mine"
    mine.write_text("#!/bin/sh\necho mine\n")
    mine.chmod(0o755)
    assert subprocess.run([str(mine)], capture_output=True, text=True).stdout.strip() == "mine"
    assert subprocess.run(["caffeinate", "-i"], capture_output=True).returncode == 0  # the stand-in


def test_files_outside_the_sandbox_are_neither_written_nor_read(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    nowhere = Path(REAL_HOME if HAS_REAL_HOME else "/") / "hermetic-probe-no-such-folder"
    with expecting_violations() as refused:
        for attempt in (
            lambda: open("/hermetic-probe-no-such-folder/x", "w"),
            lambda: open("hermetic-probe-no-such-folder/x", "w"),  # relative: the working folder is the repository
            lambda: os.mkdir(nowhere / "x"),
            lambda: os.rename(tmp_path / "gone", nowhere / "x"),
            lambda: shutil.copyfile(REPO / "pyproject.toml", nowhere / "x"),
            lambda: os.remove(nowhere / "x"),
            lambda: Path("/etc/hermetic-probe-no-such-folder/x").write_text("x"),
        ):
            with pytest.raises(HermeticViolation):
                attempt()
    assert len(refused) == 7 and all("(file)" in r and "outside the test's own folders" in r for r in refused)
    if HAS_REAL_HOME:
        with expecting_violations() as refused:
            for attempt in (
                lambda: open(Path(REAL_HOME) / ".zshrc"),
                lambda: Path(REAL_HOME, "Library", "Caches", "roughcut", "x").read_bytes(),
                lambda: os.listdir(REAL_HOME),
                lambda: list(os.scandir(Path(REAL_HOME) / ".ssh")),
            ):
                with pytest.raises(HermeticViolation):
                    attempt()
        assert len(refused) == 4 and all("real home folder" in r for r in refused)


def test_the_tests_own_folders_the_repository_and_the_python_installation_are_open(tmp_path, hermetic_sandbox):
    (tmp_path / "a.txt").write_text("a")
    shutil.copyfile(tmp_path / "a.txt", tmp_path / "b.txt")
    os.rename(tmp_path / "b.txt", tmp_path / "c.txt")
    (tmp_path / "sub").mkdir()
    shutil.rmtree(tmp_path / "sub")
    assert sorted(os.listdir(tmp_path)) == ["a.txt", "c.txt"]
    assert "roughcut" in (REPO / "pyproject.toml").read_text()  # the repository is read, never written
    assert Path(os.__file__).read_text()  # and so is the Python installation
    with open(os.devnull, "w") as sink:
        sink.write("x")
    with tempfile.TemporaryDirectory() as tmp:  # Python's own temp files land in the sandbox
        assert Path(tmp).parent == hermetic_sandbox.tmp


# ------------------------------------------------------------------ the guard fails a test that hides the refusal


def _run_pytest(args: list[str], env: dict[str, str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=600, **kw)


def test_a_refusal_the_code_swallows_still_fails_the_test_and_nothing_leaks_into_the_next(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(REPO / 'tests')!r})\n"
        "from hermetic import _restored_process_state, _stand_in_folder, hermetic_sandbox  # noqa\n"
    )
    (project / "test_probe.py").write_text(textwrap.dedent('''
        import os, socket

        def test_a_sets_things_itself():
            os.environ["SET_BY_A_TEST"] = "1"      # not through monkeypatch, as main() does with a saved key
            os.chdir("/")

        def test_b_sees_none_of_it():
            assert "SET_BY_A_TEST" not in os.environ and os.getcwd() != "/"

        def test_c_copes_with_a_network_error():
            try:
                socket.create_connection(("hermetic-probe.invalid", 443), timeout=1)
            except OSError:
                pass                                # "unreachable": the code handles it, the test passes...
    '''))
    done = _run_pytest([str(project)], dict(os.environ))
    report = done.stdout + done.stderr
    assert "3 passed, 1 error" in report, report  # ...but the sandbox still reports what was reached
    assert "ERROR at teardown of test_c_copes_with_a_network_error" in report and "hermetic test violation (network)" in report, report
    assert done.returncode != 0


def test_checks_of_the_real_machine_are_skipped_unless_asked():
    env = {k: v for k, v in os.environ.items() if k != hermetic.REAL_FLAG}
    done = _run_pytest(["-rs", "tests/test_real_environment.py"], env)
    report = done.stdout + done.stderr
    assert done.returncode == 0 and "2 skipped" in report and f"set {hermetic.REAL_FLAG}=1 to run it" in report, report


# ------------------------------------------------------------------ a lived-in Mac changes nothing


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir" for p in sorted(root.rglob("*"))}


CHILD_TESTS = [
    "tests/test_progress.py::test_doctor_json",  # failed on the owner's Mac: a saved key made doctor "reject" it
    "tests/test_providers.py::test_401_quotes_anthropic_and_403_says_the_key_was_accepted",  # failed there too: it named a saved key
    "tests/test_progress.py::test_plain_mode_prints_no_json",  # a build: keeps a Mac awake with caffeinate
    "tests/test_claude_code.py::test_signed_out_or_api_key_sign_in_stops_before_analysis",
    "tests/test_keys.py",
]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_lived_in_mac_changes_nothing(tmp_path):
    """The reviewer's two failures, reproduced as the stand-in for a Mac that has keys saved and Claude Code signed in:
    an exported API key and OpenAI key, a proxy, Roughcut and Venture Studio folders of its own, a home folder with a
    shell profile and cache. The tests run on it must pass, never run the Mac's `security`, `claude` or `caffeinate`,
    and leave its home folder exactly as it was."""
    host = tmp_path / "mac"
    home = host / "home"
    for name, text in {
        ".zshrc": "export ANTHROPIC_API_KEY=sk-ant-api03-" + "z" * 40 + "\n",
        "Library/Application Support/roughcut/presets.json": "{}",
        "Library/Application Support/com.example.studio/catalog.sqlite": "not a database",
        "Library/Caches/roughcut/loudness-v1.json": "{}",
        ".cache/roughcut/loudness-v1.json": "{}",
        ".roughcut/notes.txt": "mine",
    }.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text)
    before = _tree(home)

    env = {k: v for k, v in os.environ.items() if k not in ("FAKE_KEYCHAIN",)}
    env.update(
        MAC_HOST_DIR=str(host), PYTHONPATH=str(REPO / "tests"), HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"),
        ANTHROPIC_API_KEY="sk-ant-api03-" + "e" * 40, ANTHROPIC_BASE_URL="https://api.anthropic.com", OPENAI_API_KEY="sk-" + "e" * 40,
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-" + "e" * 40, ROUGHCUT_CONFIG_DIR=str(home / "Library/Application Support/roughcut"),
        ROUGHCUT_STUDIO_DIR=str(home / "Library/Application Support/com.example.studio"), HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
        PLAYWRIGHT_BROWSERS_PATH=hermetic.PLAYWRIGHT_BROWSERS or "",
    )  # fmt: skip
    if not hermetic.PLAYWRIGHT_BROWSERS:
        env.pop("PLAYWRIGHT_BROWSERS_PATH")
    done = _run_pytest(["-p", "mac_host", *CHILD_TESTS], env)
    report = done.stdout[-6000:] + done.stderr[-2000:]
    assert done.returncode == 0, report
    assert (host / "bin" / "security").exists(), "the stand-in Mac was not in place: " + report
    log = host / "host.log"
    assert not log.exists() or log.read_text() == "", f"a test ran the Mac's own tools:\n{log.read_text()}"
    assert _tree(home) == before, "a test touched the Mac's home folder"
