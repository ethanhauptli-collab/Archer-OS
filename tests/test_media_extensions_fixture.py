"""The Mac app counts footage by file name before a build (RoughcutKit/Footage.swift); it must read the same types the engine does."""

import json
from pathlib import Path

from roughcut import media

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures" / "media-extensions.json").read_text()
)


def _without_dots(extensions) -> list[str]:
    return sorted(ext.lstrip(".") for ext in extensions)


def test_the_apps_footage_types_are_the_engines():
    assert FIXTURE["video"] == _without_dots(media.VIDEO_EXTS)
    assert FIXTURE["audio"] == _without_dots(media.AUDIO_EXTS)
    assert FIXTURE["still"] == _without_dots(media.IMAGE_EXTS)


def test_the_apps_skipped_folders_are_the_engines():
    assert FIXTURE["skipFolderSuffixes"] == list(media.SKIP_DIR_SUFFIXES)
