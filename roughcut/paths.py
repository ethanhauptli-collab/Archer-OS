"""Paths typed into Terminal without quotes.

zsh splits `roughcut build ~/YouTube Current Project/OCVIBE` into three
arguments. When the pieces don't exist on their own but joined with single
spaces they name a real file or folder, that is what was meant. Otherwise
the run stops before anything is created, with the command written out
correctly.
"""

from __future__ import annotations

import shlex
from pathlib import Path

MAX_PIECES = 16  # words in one folder path; bounds the search


class PathInputError(ValueError):
    """An input path that doesn't exist, with the command to type instead."""


def _exists(text: str) -> bool:
    try:
        return Path(text).expanduser().exists()
    except (OSError, ValueError):
        return False


def _starts_path(piece: str) -> bool:
    return piece.startswith(("/", "~", "./", "../"))


def rejoin(pieces: list[str]) -> list[str] | None:
    """The inputs with split paths joined back, or None if some can't be found either way.

    Pieces are joined only when they don't exist separately; among the ways to
    join, the one that joins least wins.
    """
    if all(_exists(p) for p in pieces):
        return list(pieces)
    n = len(pieces)
    best: list[list[str] | None] = [None] * (n + 1)  # best[i]: a split of pieces[i:] into existing paths
    best[n] = []
    for i in range(n - 1, -1, -1):
        for j in range(i + 1, min(n, i + MAX_PIECES) + 1):
            rest = best[j]
            if rest is None:
                continue
            joined = " ".join(pieces[i:j])
            if _exists(joined) and (best[i] is None or len(rest) + 1 > len(best[i])):
                best[i] = [joined] + rest
    return best[0]


def _guess(pieces: list[str]) -> list[str]:
    """Best guess at the paths meant: a new one starts at each piece that looks like a path."""
    groups: list[list[str]] = []
    for piece in pieces:
        if not groups or _starts_path(piece):
            groups.append([piece])
        else:
            groups[-1].append(piece)
    return [" ".join(g) for g in groups]


def _command(argv: list[str], pieces: list[str], paths: list[str]) -> str:
    """argv with the split pieces replaced by the quoted paths, as one line to type."""
    n = len(pieces)
    for k in range(len(argv) - n + 1):
        if argv[k : k + n] == pieces:
            argv = argv[:k] + paths + argv[k + n :]
            break
    return "roughcut " + " ".join(shlex.quote(a) for a in argv)


def leading_folder(pieces: list[str], *, wanted: str = "", notify=print) -> tuple[str, list[str]]:
    """A folder followed by other words (revise's notes, preset save's graphic IDs): (folder, the rest).

    The folder is the shortest run of leading pieces that names a folder holding `wanted`
    (else any existing folder), so a path typed without quotes is put back together and a
    quoted one is left alone. Raises PathInputError when no leading pieces name a folder.
    """
    found = None
    for k in range(1, min(len(pieces), MAX_PIECES) + 1):
        joined = " ".join(pieces[:k])
        try:
            folder = Path(joined).expanduser()
            is_dir = folder.is_dir()
            has = is_dir and (not wanted or (folder / wanted).is_file())
        except (OSError, ValueError):
            continue
        if has:
            found = k
            break
        if is_dir and found is None:
            found = -k  # a folder, but not the kind wanted: used only if no better one turns up
    if found is None:
        if len(pieces) > 1 and not _exists(pieces[0]):
            raise PathInputError(
                f"can't find a folder at {shlex.quote(pieces[0])}, or at that joined with the words after it. "
                "If the folder's path has spaces, put it in quotes (or drag the folder into Terminal)."
            )
        return pieces[0], pieces[1:]
    k = abs(found)
    folder = " ".join(pieces[:k])
    if k > 1:
        notify(f"Read that as {shlex.quote(folder)}. Next time put the path in quotes, or drag the folder into Terminal.")
    return folder, pieces[k:]


def resolve(pieces: list[str], argv: list[str], *, notify=print) -> list[str]:
    """Inputs as the user meant them. Raises PathInputError when one can't be found."""
    joined = rejoin(pieces)
    if joined is not None:
        for path in joined:
            if " " in path and path not in pieces:
                notify(
                    f"Read that as {shlex.quote(path)}. Next time put the path in quotes, "
                    "or drag the folder into Terminal."
                )
        return joined
    guess = _guess(pieces)
    missing = [p for p in guess if not _exists(p)]
    message = "can't find " + ", ".join(shlex.quote(p) for p in missing) + " (no such file or folder)."
    if any(p not in pieces for p in missing):  # joined from pieces the shell split: show the quotes
        message += (
            "\nA path with spaces needs quotes (or drag the folder into Terminal), like this:\n  "
            + _command(argv, pieces, guess)
        )
    raise PathInputError(message)
