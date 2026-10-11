"""`roughcut update`: pull the latest code into the checkout roughcut runs from.

roughcut is installed editable (`pip install -e`) from a git clone, so new
code takes effect as soon as it's pulled. pip runs again only when
pyproject.toml changed (new dependencies or commands), using the same
Python that runs roughcut, so no activated venv or `pip` on PATH is needed.
Then `roughcut doctor` runs in a fresh process, on the new code.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path("/Applications/Roughcut.app")

# Optional extras (pyproject.toml) and the module that shows each one is installed.
EXTRAS = {"mac": "mlx_whisper", "whisper": "faster_whisper", "openai": "openai", "graphics": "playwright", "dev": "pytest"}


def checkout() -> Path:
    """The folder roughcut is installed from (the git clone, for an editable install)."""
    import roughcut

    return Path(roughcut.__file__).resolve().parents[1]


def version_in(repo: Path) -> str:
    try:
        text = (repo / "roughcut" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "?"
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    return m.group(1) if m else "?"


def installed_extras() -> list[str]:
    return [name for name, module in EXTRAS.items() if importlib.util.find_spec(module) is not None]


def pip_command(repo: Path, extras: list[str]) -> list[str]:
    target = f"{repo}[{','.join(extras)}]" if extras else str(repo)
    return [sys.executable, "-m", "pip", "install", "-e", target]


def _git(repo: Path, *args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def _pull_hint(repo: Path, err: str) -> str:
    q = shlex.quote(str(repo))
    if "would be overwritten" in err or "local changes" in err:
        return (
            f"Files in {repo} were edited by hand. Keep the edits with `git -C {q} stash`, or throw them away "
            f"with `git -C {q} checkout -- .`, then run `roughcut update` again."
        )
    if "fast-forward" in err or "divergent" in err:
        return f"Your copy has commits of its own. Look with `git -C {q} status` and sort it out there."
    if "no tracking information" in err:
        return f"This copy isn't following a branch on GitHub. Run `git -C {q} branch -u origin/main`, then update again."
    return f"Run `git -C {q} pull` to see the full message."


def update(repo: Path | None = None, *, runner=subprocess.run, out=print) -> int:
    repo = (repo or checkout()).resolve()
    if shutil.which("git") is None:
        out("roughcut update needs git. Install Apple's command line tools with `xcode-select --install`.")
        return 1
    top = _git(repo, "rev-parse", "--show-toplevel", runner=runner)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo:
        out(
            f"roughcut runs from {repo}, which isn't a git clone, so there's nothing to pull. "
            "Update it the way you installed it, or clone the repository and run `pip install -e` in the clone."
        )
        return 1
    before = _git(repo, "rev-parse", "HEAD", runner=runner).stdout.strip()
    old_version = version_in(repo)
    out(f"Updating roughcut in {repo} ...")
    pull = _git(repo, "pull", "--ff-only", runner=runner)
    if pull.returncode != 0:
        err = (pull.stderr or pull.stdout or "").strip()
        said = [ln.strip() for ln in err.splitlines() if ln.strip() and not ln.startswith("hint:")][:4]
        out("git pull didn't work:\n  " + "\n  ".join(said) + "\n" + _pull_hint(repo, err))
        return 1
    after = _git(repo, "rev-parse", "HEAD", runner=runner).stdout.strip()
    if before == after:
        out(f"Already up to date: roughcut {old_version} ({after[:7]}).")
        return 0
    changed = _git(repo, "diff", "--name-only", before, after, runner=runner).stdout.split()
    commits = _git(repo, "log", "--format=%h %s", "--no-merges", f"{before}..{after}", runner=runner).stdout.splitlines()
    if "pyproject.toml" in changed:
        cmd = pip_command(repo, installed_extras())
        out("Dependencies changed; reinstalling: " + " ".join(shlex.quote(c) for c in cmd))
        if runner(cmd).returncode != 0:
            out("The reinstall failed (see above). The new code is in place; fix the problem and run:\n  " + " ".join(shlex.quote(c) for c in cmd))
            return 1
    new_version = version_in(repo)
    label = f"roughcut {old_version} → {new_version}" if new_version != old_version else f"roughcut {new_version}"
    out(f"Updated {label} ({before[:7]} → {after[:7]}), {len(commits)} change{'s' if len(commits) != 1 else ''}:")
    for line in commits[:20]:
        out("  " + line)
    if len(commits) > 20:
        out(f"  ... and {len(commits) - 20} more")
    if any(p.startswith("mac/") for p in changed) and APP.exists():
        # Not rebuilt here: an ad-hoc signed rebuild makes macOS ask for Keychain and folder access again.
        out("The Mac app changed too. Rebuild it with:\n  " + shlex.quote(str(repo / "mac" / "scripts" / "build-app.sh")) + " --install")
    out("\nChecking the setup with the new code (roughcut doctor):")
    # A fresh process, so the checks run the code just pulled; run from the clone so
    # `-m roughcut` can't pick up some other folder named roughcut.
    if runner([sys.executable, "-m", "roughcut", "doctor"], cwd=str(repo)).returncode != 0:
        out("The update itself worked; fix what doctor flagged above before the next run.")
    return 0
