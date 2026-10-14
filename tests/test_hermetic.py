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
import sqlite3
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
NOWHERE = Path(REAL_HOME if HAS_REAL_HOME else "/") / "hermetic-probe-no-such-folder"  # nothing is ever there: a call the guard misses fails harmlessly


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


def test_the_report_header_says_when_the_read_guard_cannot_work():
    normal = hermetic.report_header("/Users/someone")
    assert normal.startswith("hermetic: every test runs in a sandbox") and "read guard" not in normal
    off = hermetic.report_header(os.sep)  # HOME is /: every path is "inside the home folder"
    assert "read guard is off" in off and "HOME is /" in off


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


# ------------------------------------------------------------------ what a guard built on `open`, `connect` and `Popen` would miss


def _refused_by_the_guard(attempt) -> list[str]:
    """What the guard refused while `attempt` ran. A call it lets through fails harmlessly: nothing exists where these point."""
    with expecting_violations() as refused:
        try:
            attempt()
        except (OSError, sqlite3.Error):  # HermeticViolation is an OSError; the rest reached the system call
            pass
    return refused


def test_sqlite_is_judged_by_the_file_it_opens(tmp_path):
    """sqlite opens its file in C: Python reports `sqlite3.connect` and no `open`. Venture Studio's live catalog is
    reached this way if the code ever opens it instead of its verified copy."""
    refused = _refused_by_the_guard(lambda: sqlite3.connect(str(NOWHERE / "x.db")))  # a plain name is read-write-create
    assert len(refused) == 1 and "outside the test's own folders" in refused[0], refused
    assert _refused_by_the_guard(lambda: sqlite3.connect(f"file:{NOWHERE}/x.db?mode=rwc", uri=True))
    if HAS_REAL_HOME:
        refused = _refused_by_the_guard(lambda: sqlite3.connect(f"file:{NOWHERE}/x.db?mode=ro&immutable=1", uri=True))
        assert len(refused) == 1 and "real home folder" in refused[0], refused  # read-only is a read
    for fine in (
        lambda: sqlite3.connect(":memory:").close(),
        lambda: sqlite3.connect("").close(),  # a private temporary database
        lambda: sqlite3.connect("file::memory:?cache=shared", uri=True).close(),
        lambda: sqlite3.connect(tmp_path / "mine.db").close(),  # a Path, in the test's own folder
        lambda: sqlite3.connect(f"file:{tmp_path / 'mine.db'}?mode=ro&immutable=1", uri=True).close(),
        lambda: sqlite3.connect(f"file:{REPO / 'pyproject.toml'}?mode=ro", uri=True).close(),  # the repository is read-only territory
    ):
        assert _refused_by_the_guard(fine) == []


def test_changes_to_a_file_that_open_does_not_report_are_judged_too(tmp_path):
    attempts = [
        lambda: os.truncate(NOWHERE / "x", 0),
        lambda: os.chown(NOWHERE / "x", os.getuid(), os.getgid()),
    ]
    if hasattr(os, "setxattr"):  # Linux
        attempts += [lambda: os.setxattr(NOWHERE / "x", "user.hermetic", b"1"), lambda: os.removexattr(NOWHERE / "x", "user.hermetic")]
    for attempt in attempts:
        refused = _refused_by_the_guard(attempt)
        assert len(refused) == 1 and "outside the test's own folders" in refused[0], refused
    mine = tmp_path / "mine.txt"
    mine.write_text("abc")
    os.truncate(mine, 1)
    os.chown(mine, os.getuid(), os.getgid())
    assert mine.read_text() == "a"


def test_a_link_in_the_sandbox_does_not_lead_out_of_it(tmp_path):
    out = tmp_path / "out"
    out.symlink_to("/")
    refused = _refused_by_the_guard(lambda: (out / "hermetic-probe-no-such-folder" / "x").write_text("x"))
    assert len(refused) == 1 and "through a link" in refused[0], refused
    if HAS_REAL_HOME:
        home = tmp_path / "home-link"
        home.symlink_to(REAL_HOME)
        assert _refused_by_the_guard(lambda: os.listdir(home))
        assert _refused_by_the_guard(lambda: (home / ".zshrc").read_bytes())
    # a link between the test's own folders is fine, both ways round
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    (tmp_path / "alias" / "f.txt").write_text("x")
    assert (tmp_path / "real" / "f.txt").read_text() == "x" and sorted(os.listdir(tmp_path / "alias")) == ["f.txt"]
    (tmp_path / "repository").symlink_to(REPO)
    assert _refused_by_the_guard(lambda: os.listdir(tmp_path / "repository")) == []  # a link out of the sandbox to somewhere readable is just a read


def test_the_other_ways_to_send_or_look_up_are_refused():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        refused = _refused_by_the_guard(lambda: udp.sendmsg([b"x"], [], 0, ("192.0.2.1", 53)))
        assert len(refused) == 1 and "(network)" in refused[0], refused
    finally:
        udp.close()
    numeric = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV  # no lookup is made even if the guard misses
    for attempt in (lambda: socket.gethostbyaddr("192.0.2.1"), lambda: socket.getnameinfo(("192.0.2.1", 80), numeric)):
        refused = _refused_by_the_guard(attempt)
        assert len(refused) == 1 and "(network)" in refused[0], refused
    assert _refused_by_the_guard(lambda: socket.getnameinfo(("127.0.0.1", 80), numeric)) == []


def test_a_change_relative_to_a_directory_the_program_holds_is_judged_by_that_directory(tmp_path):
    """What shutil.rmtree does inside the folder it removes; os.remove(name, dir_fd=...) is the same call."""
    held = os.open(os.sep, os.O_RDONLY)
    try:
        refused = _refused_by_the_guard(lambda: os.remove("hermetic-probe-no-such-file", dir_fd=held))
        assert len(refused) == 1 and "outside the test's own folders" in refused[0], refused
        assert _refused_by_the_guard(lambda: os.rename("hermetic-probe-no-such-file", "hermetic-probe-other", src_dir_fd=held, dst_dir_fd=held))
        assert _refused_by_the_guard(lambda: os.mkdir("hermetic-probe-no-such-folder/x", dir_fd=held))  # its parent is missing: harmless if let through
    finally:
        os.close(held)
    (tmp_path / "f").write_text("x")
    (tmp_path / "d").mkdir()
    mine = os.open(tmp_path, os.O_RDONLY)
    try:
        os.rename("f", "g", src_dir_fd=mine, dst_dir_fd=mine)
        os.remove("g", dir_fd=mine)
        os.rmdir("d", dir_fd=mine)
    finally:
        os.close(mine)
    assert os.listdir(tmp_path) == []


def test_a_held_directory_is_found_on_a_mac_too(monkeypatch):
    """macOS has no /proc: F_GETPATH says where the directory is. Not runnable here as itself, so its answer is faked."""
    import types

    asked = []

    def answers(fd, op, buffer):
        asked.append((fd, op, len(buffer)))
        return b"/private/var/folders/ab/cd\0" + b"\0" * 100

    def cannot(fd, op, buffer):
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(hermetic.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(F_GETPATH=50, fcntl=answers))
    assert hermetic._held_directory(7) == "/private/var/folders/ab/cd" and asked == [(7, 50, 1024)]
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(F_GETPATH=50, fcntl=cannot))
    assert hermetic._held_directory(7) is None  # not judged, rather than refused for a reason that is not the test's
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace())  # a Python without the constant
    assert hermetic._held_directory(7) is None


def test_a_program_is_looked_up_where_python_will_look_for_it(tmp_path, monkeypatch):
    """With no PATH in `env` Python searches os.defpath: a stand-in first on the test's PATH is not what would run."""
    stub = tmp_path / "bin" / "sh"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    refused = _refused_by_the_guard(lambda: subprocess.run(["sh", "-c", "true"], env={"X": "1"}, capture_output=True))
    assert len(refused) == 1 and "(program)" in refused[0], refused
    assert subprocess.run(["sh"], env={"PATH": str(stub.parent)}, capture_output=True).returncode == 0  # the test's own program


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


def _project(tmp_path: Path, **modules: str) -> Path:
    """A throwaway pytest project that uses the sandbox the way tests/conftest.py does: one line, `pytest_plugins`."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "conftest.py").write_text(f"import sys\nsys.path.insert(0, {str(REPO / 'tests')!r})\npytest_plugins = ['hermetic']\n")
    for name, source in modules.items():
        (project / f"test_{name}.py").write_text(textwrap.dedent(source))
    return project


def test_a_refusal_the_code_swallows_still_fails_the_test_and_nothing_leaks_into_the_next(tmp_path):
    project = _project(tmp_path, probe='''
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
    ''')
    done = _run_pytest([str(project)], dict(os.environ))
    report = done.stdout + done.stderr
    assert "3 passed, 1 error" in report, report  # ...but the sandbox still reports what was reached
    assert "ERROR at teardown of test_c_copes_with_a_network_error" in report and "hermetic test violation (network)" in report, report
    assert done.returncode != 0
    # and says what to do about it, not only what happened
    assert "loopback listener" in report and f"@pytest.mark.{hermetic.MARKER}" in report and "tests/hermetic.py" in report, report


# ------------------------------------------------------------------ fixtures wider than a test

WIDE_PROBE = '''
    import json, os, shutil, socket, tempfile
    from pathlib import Path

    import pytest
    from hermetic import HermeticViolation, expecting_violations

    MAC_HOME = os.environ["PROBE_MAC_HOME"]
    BASETEMP = os.path.realpath(os.environ["PROBE_BASETEMP"])
    WATCHED = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "ROUGHCUT_CONFIG_DIR")


    def seen():
        """What the code running right now can see of the machine."""
        from roughcut import studio

        return {
            "keys": [k for k in WATCHED if os.environ.get(k)],
            "no_proxy": os.environ.get("NO_PROXY"),
            "home": str(Path.home()),
            "studio_installed": studio.installed(),
            "security": shutil.which("security") or "",
            "tmp": os.path.realpath(tempfile.gettempdir()),
        }


    def problems(saw):
        found = []
        if saw["keys"]:
            found.append("could see " + ", ".join(saw["keys"]))
        if saw["no_proxy"] != "*":
            found.append("proxies were not turned off")
        if saw["home"].startswith(MAC_HOME):
            found.append("HOME was the machine's")
        if saw["studio_installed"]:
            found.append("Venture Studio was reachable")
        if Path(saw["security"]).parent.name[:9] != "stand-ins":
            found.append("`security` was not the stand-in: " + saw["security"])
        if not saw["tmp"].startswith(BASETEMP):
            found.append("Python's temp folder was the machine's")
        return found


    @pytest.fixture(scope="session")
    def session_saw():
        return seen()


    @pytest.fixture(scope="module")
    def module_saw():
        return seen()


    @pytest.fixture(scope="module")
    def sets_a_variable():
        os.environ["PROBE_FLAG"] = "kept"
        return 1


    @pytest.fixture(scope="module")
    def seen_at_teardown(tmp_path_factory):
        base = tmp_path_factory.getbasetemp()
        yield 1
        (base / "teardown-saw.json").write_text(json.dumps(problems(seen())))


    @pytest.fixture(scope="module")
    def late():
        return seen()


    @pytest.fixture(scope="class")
    def class_saw():
        return seen()


    class TestAClass:
        def test_a_class_fixture(self, class_saw):
            assert problems(class_saw) == []


    def test_a_session_fixture(session_saw):
        assert problems(session_saw) == []


    def test_a_module_fixture(module_saw):
        assert problems(module_saw) == []


    def test_a_variable_a_fixture_set_itself_is_still_there(sets_a_variable):
        assert os.environ.get("PROBE_FLAG") == "kept"


    def test_the_teardown_runs_in_the_sandbox_too(seen_at_teardown):
        assert seen_at_teardown == 1


    def test_a_fixture_asked_for_late_in_a_test_is_sandboxed_and_the_guard_stays_on(request):
        assert problems(request.getfixturevalue("late")) == []
        with expecting_violations() as refused:
            with pytest.raises(HermeticViolation):
                socket.create_connection(("hermetic-probe.invalid", 443), timeout=1)
        assert len(refused) == 1


    def test_a_test_itself():
        assert problems(seen()) == []
'''

SWALLOWED_AT_SETUP = '''
    import socket

    import pytest


    @pytest.fixture(scope="module")
    def reaches_out():
        try:
            socket.create_connection(("hermetic-probe.invalid", 443), timeout=1)
        except OSError:
            pass  # the code under test copes with an unreachable host...
        return 1


    def test_uses_it_at_setup(reaches_out):
        assert reaches_out == 1
'''

SWALLOWED_AT_TEARDOWN = '''
    import socket

    import pytest


    @pytest.fixture(scope="module")
    def reaches_out_when_done():
        yield 1
        try:
            socket.create_connection(("hermetic-probe.invalid", 443), timeout=1)
        except OSError:
            pass  # ...here too


    def test_uses_it_at_teardown(reaches_out_when_done):
        assert reaches_out_when_done == 1
'''


def _lived_in_home(tmp_path: Path) -> Path:
    """A Mac's home folder: a shell profile with a key, and Venture Studio at its default place with a real snapshot."""
    from test_studio import FakeStudio

    from roughcut import studio

    home = tmp_path / "mac-home"
    (home / "Library" / "Application Support").mkdir(parents=True)
    (home / ".zshrc").write_text("export ANTHROPIC_API_KEY=sk-ant-api03-" + "z" * 40 + "\n")
    fake = FakeStudio(tmp_path / "studio-source")
    fake.snapshot("1700000500-1-1", int(__import__("time").time()) - 3600, libraries=[("/Volumes/Some Library", "Some Library")])
    shutil.copytree(fake.dir, home / "Library" / "Application Support" / studio.BUNDLE_ID)
    return home


def _machine_env(home: Path, **extra: str) -> dict[str, str]:
    """The environment a pytest run starts with on a machine where Roughcut is in daily use (not this test's sandbox)."""
    env = {k: v for k, v in os.environ.items() if k not in ("FAKE_KEYCHAIN", "NO_PROXY", "no_proxy")}
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if "stand-ins" not in p)
    env.update(
        HOME=str(home), PROBE_MAC_HOME=str(home), ANTHROPIC_API_KEY="sk-ant-api03-" + "e" * 40, OPENAI_API_KEY="sk-" + "e" * 40,
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-" + "e" * 40, HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
        ROUGHCUT_CONFIG_DIR=str(home / "Library" / "Application Support" / "roughcut"),
    )  # fmt: skip
    env.update(extra)
    return env


def test_session_module_and_class_fixtures_run_in_the_sandbox_too(tmp_path):
    """Pytest sets up a wider fixture before any test's own autouse fixtures. `built` in test_audio_fcp.py runs a whole
    pipeline.build there; without this it read the machine's Venture Studio folder and copied its catalog snapshot."""
    home = _lived_in_home(tmp_path)
    base = tmp_path / "child-base"
    project = _project(tmp_path, wide=WIDE_PROBE)
    done = _run_pytest([str(project), f"--basetemp={base}"], _machine_env(home, PROBE_BASETEMP=str(base)))
    report = done.stdout + done.stderr
    assert done.returncode == 0 and " passed" in report and "failed" not in report and "error" not in report, report
    assert json.loads((base / "teardown-saw.json").read_text()) == [], "a fixture's teardown saw the machine"


def test_only_fixtures_the_tests_define_get_a_sandbox_of_their_own():
    import types

    from _pytest.tmpdir import TempPathFactory

    def mine():  # a fixture's function, defined in a test file
        return 1

    def fixture(scope, name, func):
        return types.SimpleNamespace(scope=scope, argname=name, func=func)

    assert hermetic._wider_than_a_test(fixture("module", "built", mine)) and hermetic._wider_than_a_test(fixture("session", "footage", mine))
    assert hermetic._wider_than_a_test(fixture("class", "shared", mine)) and hermetic._wider_than_a_test(fixture("package", "shared", mine))
    assert not hermetic._wider_than_a_test(fixture("function", "fake", mine))  # the test's own sandbox covers it
    assert not hermetic._wider_than_a_test(fixture("session", "_stand_in_folder", mine))  # this module's own helpers
    assert not hermetic._wider_than_a_test(fixture("session", "tmp_path_factory", TempPathFactory.getbasetemp))  # pytest's and plugins' own
    assert not hermetic._wider_than_a_test(fixture("module", "something", None))  # no function to place


def test_listing_what_would_be_set_up_still_works(tmp_path):
    """`--setup-plan` replaces every fixture by a dummy: the hook must not ask those dummies for anything."""
    project = _project(tmp_path, plan="""
        import pytest

        @pytest.fixture(scope="module")
        def shared():
            return 1

        def test_uses_it(shared):
            assert shared == 1
    """)
    for flag in ("--setup-plan", "--setup-only"):
        done = _run_pytest([flag, str(project)], dict(os.environ))
        report = done.stdout + done.stderr
        assert done.returncode == 0 and "SETUP    M shared" in report and "error" not in report.lower(), (flag, report)


def test_a_wide_fixture_that_swallows_a_refusal_still_fails(tmp_path):
    project = _project(tmp_path, at_setup=SWALLOWED_AT_SETUP, at_teardown=SWALLOWED_AT_TEARDOWN)
    done = _run_pytest([str(project)], dict(os.environ))
    report = done.stdout + done.stderr
    assert "1 passed, 2 errors" in report, report  # the code coped, so the tests would have passed; the fixtures did not stay in the sandbox
    assert "ERROR at setup of test_uses_it_at_setup" in report and "ERROR at teardown of test_uses_it_at_teardown" in report, report
    assert "module-scoped fixture reaches_out" in report and "module-scoped fixture reaches_out_when_done" in report, report
    assert report.count("hermetic test violation (network)") >= 2 and done.returncode != 0, report


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
    "tests/test_audio_fcp.py::test_report_warns_about_the_2398_clip_in_a_2997_cut",  # a whole build inside a module-scoped fixture
]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_lived_in_mac_changes_nothing(tmp_path):
    """The reviewer's two failures, reproduced as the stand-in for a Mac that has keys saved and Claude Code signed in:
    an exported API key and OpenAI key, a proxy, Roughcut and Venture Studio folders of its own, a home folder with a
    shell profile and cache. The tests run on it must pass, never run the Mac's `security`, `claude` or `caffeinate`,
    never read its Venture Studio (not even from a fixture shared by a whole module) and leave its home folder
    exactly as it was."""
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
    from test_studio import FakeStudio

    studio_folder = home / "Library/Application Support/com.example.studio"
    fake = FakeStudio(host / "studio-source")
    fake.snapshot("1700000500-1-1", int(__import__("time").time()) - 3600, libraries=[("/Volumes/Some Library", "Some Library")])
    shutil.copytree(fake.dir, studio_folder, dirs_exist_ok=True)  # Studio's newest snapshot: a reader that looks copies it
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
    base = host / "pytest-base"
    done = _run_pytest(["-p", "mac_host", f"--basetemp={base}", *CHILD_TESTS], env)
    report = done.stdout[-6000:] + done.stderr[-2000:]
    assert done.returncode == 0, report
    assert (host / "bin" / "security").exists(), "the stand-in Mac was not in place: " + report
    log = host / "host.log"
    assert not log.exists() or log.read_text() == "", f"a test ran the Mac's own tools:\n{log.read_text()}"
    assert _tree(home) == before, "a test touched the Mac's home folder"
    copies = sorted(str(p.relative_to(base)) for p in base.rglob("venture-studio"))
    assert not copies, f"something read the Mac's Venture Studio and copied its snapshot: {copies}"
