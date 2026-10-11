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
