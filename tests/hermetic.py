"""Keeps the test suite off the machine it runs on.

The suite used to depend on whoever ran it: a Mac with saved Keychain entries, a signed-in Claude Code, an
exported API key, a proxy, or a cache folder full of earlier runs gave different answers than CI (two tests
failed on the owner's Mac for exactly that reason). Every test now runs in a sandbox that `hermetic_sandbox`
(an autouse fixture, wired in conftest.py) builds around it:

  * **Environment.** Secrets and settings the code reads are removed (`ANTHROPIC_*`, `OPENAI_*`, `CLAUDE*`,
    `ROUGHCUT_*`, `XDG_*`, `GIT_*`, cloud and proxy variables, anything named like a token or key); `HOME`,
    the temp folder and so the cache, preset, font and Venture Studio folders are a fresh folder of the
    test's own; proxies are off (`NO_PROXY=*`, which also keeps Python on a Mac from asking macOS for its
    system proxy). `os.environ` and the working folder are put back after each test, so a command that sets
    a variable itself (`main()` loads saved keys into it) cannot leak into the next test.
  * **Tools that look at the machine.** `security` (the Keychain), `caffeinate`, `osascript` and `open` are
    stand-ins first on `PATH`: the Keychain is empty and per test, the rest do nothing. `find_claude()` finds
    only a `claude` a test put in its own folder, never the Claude Code installed on the machine.
  * **A guard** (a Python audit hook, active only while a test runs) refuses what a test must not reach, with
    a loud `HermeticViolation` (a `PermissionError`, so code that already copes with a missing tool or share
    behaves as it does on CI) and, because code may swallow that error, also fails the test at teardown:
      - the network: any connection or name lookup that is not loopback (and no unix socket outside the
        test's folder);
      - programs: only ffmpeg, ffprobe, git, sips, this Python, Playwright's driver, the stand-ins above and
        anything inside the test's own folders may start;
      - files: nothing is written outside the test's folders (or a `__pycache__`), and nothing under the real
        home folder is read or listed, apart from the repository, the Python installation and Playwright's
        browsers.
  * `@pytest.mark.real_environment` marks a check that needs the real machine (a doctor smoke): it is skipped
    unless `ROUGHCUT_TESTS_REAL=1`, and then runs without any of the above.

A test that proves the guard works wraps the forbidden call in `expecting_violations()`.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import re
import shlex
import shutil
import site
import stat
import sys
import sysconfig
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

REAL_FLAG = "ROUGHCUT_TESTS_REAL"
MARKER = "real_environment"
REPO = Path(__file__).resolve().parents[1]

# The machine's own state, read once, before anything is redirected.
REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def _playwright_browsers() -> str | None:
    """Where Playwright keeps its browsers on this machine (a redirected HOME must not hide them)."""
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit:
        return explicit
    if sys.platform == "darwin":
        default = os.path.join(REAL_HOME, "Library", "Caches", "ms-playwright")
    else:
        default = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(REAL_HOME, ".cache"), "ms-playwright")
    return default if os.path.isdir(default) else None


PLAYWRIGHT_BROWSERS = _playwright_browsers()

# ------------------------------------------------------------------ environment

SCRUB_PREFIXES = (
    "ANTHROPIC_", "OPENAI_", "OLLAMA_", "OPENROUTER_", "CLAUDE", "ROUGHCUT_", "XDG_", "GIT_",
    "GH_", "GITHUB_", "AWS_", "CLOUDSDK_", "GOOGLE_", "HF_", "HUGGINGFACE",
)  # fmt: skip
# Python takes any `<scheme>_proxy` variable as a proxy for that scheme (a tool's `DOCKER_HTTPS_PROXY` included).
SCRUB_PATTERN = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|_PROXY$", re.IGNORECASE)
# Where a program is, not what it holds or does: left as the machine has it. (ROUGHCUT_WRITE_FIXTURES is the
# developer's switch for rewriting a checked-in sample on purpose.)
KEEP = frozenset({"ROUGHCUT_CHROMIUM", REAL_FLAG, "ROUGHCUT_WRITE_FIXTURES"})


def scrubbed(env: dict[str, str]) -> dict[str, str]:
    """`env` without the variables the code reads from the machine, and with proxies off."""
    out = {k: v for k, v in env.items() if k in KEEP or not (k.upper().startswith(SCRUB_PREFIXES) or SCRUB_PATTERN.search(k))}
    out["NO_PROXY"] = out["no_proxy"] = "*"
    out["GIT_CONFIG_NOSYSTEM"] = "1"  # no /etc/gitconfig or Xcode credential helper
    out["GIT_TERMINAL_PROMPT"] = "0"
    return out


# ------------------------------------------------------------------ stand-ins for tools that look at the machine

# `security`: macOS's Keychain tool. An empty store per test, in the JSON file named by $FAKE_KEYCHAIN; every
# command line is logged to "<store>.argv" so a test can check a key never appears in one.
FAKE_SECURITY = r'''import json, os, sys
store_path = os.environ["FAKE_KEYCHAIN"]
store = json.load(open(store_path)) if os.path.exists(store_path) else {}
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
DO_NOTHING = ("caffeinate", "osascript", "open")  # keep the Mac awake, show a banner, hand a file to an app


def make_stand_ins(folder: Path) -> Path:
    """The programs a test finds first on PATH. Wrappers are shell scripts that run this Python, so a long
    interpreter path (a shebang line has a length limit) never breaks them."""
    folder.mkdir(parents=True, exist_ok=True)
    body = folder / "security.py"
    body.write_text(FAKE_SECURITY)
    script = folder / "security"
    script.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(body))} \"$@\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    for name in DO_NOTHING:
        path = folder / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return folder


# ------------------------------------------------------------------ the guard


class HermeticViolation(PermissionError):
    """A test reached for something outside its sandbox (the real network, a program, the machine's files)."""


class _State:
    def __init__(self) -> None:
        self.active = False
        self.test = ""
        self.violations: list[str] = []
        self.own: tuple[str, ...] = ()  # the test's folders: may be written, run from, connected to
        self.readable: tuple[str, ...] = ()  # under the real home, but fine to read


_STATE = _State()


def _inside(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _refuse(kind: str, detail: str) -> None:
    message = f"hermetic test violation ({kind}) in {_STATE.test}: {detail}"
    _STATE.violations.append(message)
    raise HermeticViolation(message)


def _absolute(path) -> str | None:
    if isinstance(path, int) or path is None:
        return None
    try:
        text = os.fsdecode(path)
        return os.path.normpath(text if os.path.isabs(text) else os.path.join(os.getcwd(), text))
    except (TypeError, ValueError, OSError):
        return None


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_DEV = ("/dev/",)


def _may_write(path: str) -> bool:
    return _inside(path, _STATE.own) or path.startswith(_DEV) or f"{os.sep}__pycache__{os.sep}" in path or path.endswith(f"{os.sep}__pycache__")


def _write_check(event: str, path) -> None:
    p = _absolute(path)
    if p is None:
        return
    if not _may_write(p):
        _refuse("file", f"{event} {p} is outside the test's own folders")
    if not p.startswith(_DEV):  # /dev/stdout is a link into /proc
        real = os.path.realpath(p)  # a link inside the test's folders can lead anywhere
        if real != p and not _may_write(real):
            _refuse("file", f"{event} {p} leads through a link to {real}, outside the test's own folders")


def _read_check(event: str, path) -> None:
    p = _absolute(path)
    if p is None or not REAL_HOME or REAL_HOME == os.sep:
        return
    for q in (p, os.path.realpath(p)) if _inside(p, _STATE.own) else (p,):  # a link in the test's folders can lead to the home folder
        if _inside(q, (REAL_HOME,)) and not _inside(q, _STATE.own) and not _inside(q, _STATE.readable):
            through = "" if q == p else f" (through a link to {q})"
            _refuse("file", f"{event} {p}{through} is in the real home folder, which tests must not read (HOME is the test's own folder)")


def _on_open(args) -> None:
    path, _mode, flags = args[0], args[1], args[2]
    if isinstance(flags, int) and flags & _WRITE_FLAGS:
        _write_check("write", path)
    else:
        _read_check("read", path)


def _on_listing(event):
    return lambda args: _read_check(event, args[0] if args else None)


def _held_directory(fd: int) -> str | None:
    """Where a directory the program holds open is: Linux says through /proc, macOS through F_GETPATH. None when the
    platform cannot say (the call is then not judged)."""
    try:
        if sys.platform == "darwin":
            import fcntl

            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0]) or None
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ImportError, AttributeError, ValueError, TypeError):
        return None


def _on_mutation(event: str, *targets: tuple[int, int | None]):
    """`targets`: (position of a path, position of its dir_fd). A path relative to a directory the program holds open
    (what shutil.rmtree does to everything under the folder it was asked to remove) is judged by that directory."""

    def check(args) -> None:
        for path, dir_fd in targets:
            if path >= len(args):
                continue
            held = args[dir_fd] if dir_fd is not None and dir_fd < len(args) else None  # None or negative (AT_FDCWD): not given
            if held is None or not isinstance(held, int) or held < 0:
                _write_check(event, args[path])
                continue
            name = args[path]
            folder = _held_directory(held) if isinstance(name, (str, bytes, os.PathLike)) else None
            if folder is not None:
                _write_check(event, os.path.join(folder, os.fsdecode(name)))

    return check


def _on_sqlite(args) -> None:
    """sqlite3 opens its file in C, so no `open` event is seen: judge the database by its name. A plain name is opened
    read-write and created when missing; a `file:` URI with mode=ro (or immutable=1) is a read; memory is not a file."""
    target = args[0] if args else None
    if not isinstance(target, (str, bytes, os.PathLike)):
        return
    try:
        text = os.fsdecode(target)
    except (TypeError, ValueError):
        return
    if text in ("", ":memory:"):
        return
    if text.startswith("file:"):
        parts = urlsplit(text)
        query = parse_qs(parts.query)
        path = unquote(parts.path)
        if not path or path == ":memory:" or "memory" in query.get("mode", []):
            return
        if "ro" in query.get("mode", []) or "1" in query.get("immutable", []):
            _read_check("sqlite3", path)
        else:
            _write_check("sqlite3", path)
        return
    _write_check("sqlite3", text)


# Network ---------------------------------------------------------------------

_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def is_local_host(host) -> bool:
    """Loopback (or "any", which connects to this machine), by name or number. No lookup is ever made."""
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    text = str(host).strip().strip("[]").split("%")[0].lower()
    if text == "" or text in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _address_check(event: str, address) -> None:
    if isinstance(address, tuple):
        if not is_local_host(address[0]):
            _refuse("network", f"{event} to {address[0]}:{address[1] if len(address) > 1 else ''}; only loopback is allowed")
    elif isinstance(address, (str, bytes)) and address[:1] not in ("", b"", "\0", b"\0"):  # a unix socket (not an abstract one)
        p = _absolute(address)
        if p is not None and not _inside(p, _STATE.own):
            _refuse("network", f"{event} to the unix socket {p}, outside the test's own folders")


def _on_connect(args) -> None:
    _address_check("connect", args[1])


def _on_sendto(args) -> None:
    _address_check("sendto", args[1])


def _on_lookup(args) -> None:
    if not is_local_host(args[0]):
        _refuse("network", f"name lookup of {args[0]!r}; only loopback is allowed")


def _on_name_info(args) -> None:
    """socket.getnameinfo((address, port, ...), flags): a reverse lookup of the address."""
    address = args[0]
    _on_lookup((address[0] if isinstance(address, tuple) and address else address,))


# Programs --------------------------------------------------------------------

ALLOWED_PROGRAMS = frozenset({"ffmpeg", "ffprobe", "git", "sips"})  # local tools, run on the test's own files


@functools.lru_cache(maxsize=1)
def _python_roots() -> tuple[str, ...]:
    """Where this Python and its libraries are (readable even when they are under the home folder)."""
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(os.path.realpath(sys.executable))}
    roots |= {sysconfig.get_paths().get(key, "") for key in ("stdlib", "platstdlib", "purelib", "platlib")}
    return tuple(sorted({os.path.realpath(r) for r in roots if r}))


@functools.lru_cache(maxsize=1)
def _package_roots() -> tuple[str, ...]:
    """Installed packages: Playwright's driver (a node program) lives in one."""
    paths = sysconfig.get_paths()
    return tuple(sorted({os.path.realpath(paths[key]) for key in ("purelib", "platlib") if paths.get(key)}))


_PYTHON = re.compile(r"^python[0-9.]*$")


def which(name: str, search: str | None = None) -> str | None:
    """`shutil.which`, but not the one a test may have replaced: the guard must see what would really start."""
    for folder in (os.environ.get("PATH", "") if search is None else search).split(os.pathsep):
        candidate = os.path.join(folder or os.curdir, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _resolve_program(name, env, cwd) -> str | None:
    text = os.fsdecode(name)
    if os.sep in text:
        return os.path.join(os.fsdecode(cwd), text) if cwd and not os.path.isabs(text) else text
    try:
        folders = os.get_exec_path(env)  # what subprocess searches: env's PATH, else os.defpath, never this process's PATH
    except (TypeError, ValueError, AttributeError):
        folders = os.get_exec_path(None)
    return which(text, os.pathsep.join(os.fsdecode(f) for f in folders))


def _check_program(event: str, name, env=None, cwd=None) -> None:
    if name is None:
        return
    found = _resolve_program(name, env, cwd)
    if found is None:
        return  # not installed: nothing runs, the caller gets FileNotFoundError
    real = os.path.realpath(found)
    if _inside(real, _STATE.own) or _inside(os.path.normpath(found), _STATE.own):
        return
    if os.path.basename(found) in ALLOWED_PROGRAMS or os.path.basename(real) in ALLOWED_PROGRAMS:
        return
    if _PYTHON.match(os.path.basename(found)) or _PYTHON.match(os.path.basename(real)) or _inside(real, _package_roots()):
        return  # a Python (of a venv too), or a program an installed package brings (Playwright's driver)
    _refuse("program", f"{event} would start {found}; only ffmpeg, ffprobe, git, sips, this Python and programs inside the test's folders may run")


def _on_popen(args) -> None:
    _check_program("subprocess", args[0], args[3] if len(args) > 3 else None, args[2] if len(args) > 2 else None)


def _on_exec(args) -> None:
    _check_program("exec", args[0], args[2] if len(args) > 2 else None)


def _on_system(args) -> None:
    _refuse("program", f"os.system({args[0]!r}) starts a shell")


_HANDLERS = {
    "open": _on_open,
    "os.listdir": _on_listing("list"),
    "os.scandir": _on_listing("list"),
    "os.mkdir": _on_mutation("mkdir", (0, 2)),
    "os.rmdir": _on_mutation("rmdir", (0, 1)),
    "os.remove": _on_mutation("remove", (0, 1)),
    "os.rename": _on_mutation("rename", (0, 2), (1, 3)),
    "os.symlink": _on_mutation("symlink", (1, 2)),
    "os.link": _on_mutation("link", (1, 3)),
    "os.chmod": _on_mutation("chmod", (0, 2)),
    "os.chown": _on_mutation("chown", (0, 3)),
    "os.utime": _on_mutation("utime", (0, 3)),
    "os.truncate": _on_mutation("truncate", (0, None)),
    "os.setxattr": _on_mutation("setxattr", (0, None)),
    "os.removexattr": _on_mutation("removexattr", (0, None)),
    "shutil.copyfile": _on_mutation("copy", (1, None)),
    "shutil.rmtree": _on_mutation("rmtree", (0, 1)),
    "shutil.move": _on_mutation("move", (1, None)),
    "sqlite3.connect": _on_sqlite,
    "socket.connect": _on_connect,
    "socket.sendto": _on_sendto,
    "socket.sendmsg": _on_sendto,
    "socket.getaddrinfo": _on_lookup,
    "socket.gethostbyname": _on_lookup,
    "socket.gethostbyaddr": _on_lookup,
    "socket.getnameinfo": _on_name_info,
    "subprocess.Popen": _on_popen,
    "os.posix_spawn": _on_exec,
    "os.exec": _on_exec,
    "os.system": _on_system,
}


_BUSY = threading.local()


def _audit(event: str, args: tuple) -> None:
    if not _STATE.active:
        return
    handler = _HANDLERS.get(event)
    if handler is None or getattr(_BUSY, "on", False):
        return
    _BUSY.on = True  # what the check itself does (resolving a path, a program) is not judged
    try:
        handler(args)
    finally:
        _BUSY.on = False


sys.addaudithook(_audit)  # permanent for the process; refuses nothing outside a test


@contextmanager
def expecting_violations():
    """For the tests of the guard itself: what the guard refused inside the block is handed back (and does not
    fail the test at teardown)."""
    mark = len(_STATE.violations)
    refused: list[str] = []
    try:
        yield refused
    finally:
        refused.extend(_STATE.violations[mark:])
        del _STATE.violations[mark:]


# ------------------------------------------------------------------ the sandbox


class Sandbox:
    """What a test is given by the `hermetic_sandbox` fixture."""

    def __init__(self, root: Path, stand_ins: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.tmp = root / "tmp"
        self.stand_ins = stand_ins
        self.keychain = self.home / ".fake-keychain.json"  # the stand-in `security`'s store
        for folder in (self.home, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)


def _own_roots(base: Path, sandbox: Sandbox) -> tuple[str, ...]:
    roots = {str(base), os.path.realpath(base), str(sandbox.root), os.path.realpath(sandbox.root)}
    if os.environ.get("ROUGHCUT_WRITE_FIXTURES"):  # the developer's own switch: rewrite a checked-in sample
        roots.add(str(REPO / "mac" / "Tests"))
    return tuple(sorted(roots))


def _readable_roots() -> tuple[str, ...]:
    roots = set(_python_roots()) | {str(REPO)}
    roots |= {p for p in sys.path if p and os.path.isabs(p)}
    try:
        roots |= set(site.getsitepackages()) | {site.getusersitepackages()}
    except Exception:  # noqa: BLE001 - some embedded Pythons have no site module
        pass
    if PLAYWRIGHT_BROWSERS and PLAYWRIGHT_BROWSERS != "0":
        roots.add(os.path.realpath(PLAYWRIGHT_BROWSERS))
    return tuple(sorted({os.path.realpath(r) for r in roots if r} | {r for r in roots if r}))


@pytest.fixture(scope="session")
def _stand_in_folder(tmp_path_factory) -> Path:
    return make_stand_ins(tmp_path_factory.mktemp("stand-ins"))


@pytest.fixture(autouse=True)
def _restored_process_state():
    """The environment and working folder as they were before the test, whatever the code under test set."""
    env, cwd = dict(os.environ), os.getcwd()
    yield
    os.chdir(cwd)
    os.environ.clear()
    os.environ.update(env)


@pytest.fixture(autouse=True)
def hermetic_sandbox(request, _restored_process_state, monkeypatch, tmp_path_factory, _stand_in_folder):
    if request.node.get_closest_marker(MARKER):
        yield None  # a check of the real machine: no sandbox
        return
    sandbox = Sandbox(tmp_path_factory.mktemp("sandbox"), _stand_in_folder)
    base = tmp_path_factory.getbasetemp()

    env = scrubbed(dict(os.environ))
    for name in [k for k in os.environ if k not in env]:
        monkeypatch.delenv(name)
    for name, value in env.items():
        if os.environ.get(name) != value:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox.tmp))  # Python's temp files; programs it starts keep the machine's TMPDIR
    monkeypatch.setenv("PATH", f"{sandbox.stand_ins}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_KEYCHAIN", str(sandbox.keychain))
    if PLAYWRIGHT_BROWSERS:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", PLAYWRIGHT_BROWSERS)
    # Venture Studio "isn't installed" unless a test points this at a fake one.
    monkeypatch.setenv("ROUGHCUT_STUDIO_DIR", str(sandbox.root / "no-venture-studio"))

    from roughcut.providers import claude_code

    def find_claude() -> str | None:
        """Only a `claude` the test itself put on PATH: never the Claude Code installed on this machine."""
        found = shutil.which("claude")
        return found if found and _inside(os.path.realpath(found), _STATE.own) else None

    monkeypatch.setattr(claude_code, "find_claude", find_claude)

    _STATE.test = request.node.nodeid
    _STATE.own = _own_roots(base, sandbox)
    _STATE.readable = _readable_roots()
    _STATE.violations = []
    _STATE.active = True
    try:
        yield sandbox
    finally:
        _STATE.active = False
    if _STATE.violations:
        found, _STATE.violations = _STATE.violations, []
        pytest.fail("the test reached outside its sandbox:\n  " + "\n  ".join(found), pytrace=False)


def apply_real_environment_marker(items) -> None:
    """Skip the checks of the real machine unless asked (ROUGHCUT_TESTS_REAL=1)."""
    if os.environ.get(REAL_FLAG) == "1":
        return
    skip = pytest.mark.skip(reason=f"checks the real machine: set {REAL_FLAG}=1 to run it")
    for item in items:
        if item.get_closest_marker(MARKER):
            item.add_marker(skip)
