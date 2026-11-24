"""Writing and deleting files in a folder that a sync client such as OneDrive may be watching."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable


HELD_WAIT = 0.25  # Seconds between attempts while the file is held


def write_text(path: Path, text: str) -> None:
    """Replace the file with text in one step, so that a reader never sees it half written."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    patiently(lambda: os.replace(tmp, path))


def patiently(action: Callable[[], object], attempts: int = 20) -> None:
    """Run action, retrying after a short wait while a sync client holds the file."""
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(HELD_WAIT)
