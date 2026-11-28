"""The colours of a theme, read from ui/style.css (the one place of every value), and the icon dressed in them."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from .config import THEME_IDS, UI_DIR

# The variables of ui/style.css that colour the icon. ui/icon.svg is drawn in their values of the default theme.
ICON_VARIABLES = ("icon-ground-a", "icon-ground-b", "icon-metal-a", "icon-metal-b", "gold-hi")
_BLOCK = r"^{}\s*\{{(.*?)^\}}"  # A rule set whose closing brace starts a line
_VAR = re.compile(r"--([\w-]+):\s*([^;]+);")


@cache
def palette(theme: str) -> dict[str, str]:
    """Return the variables of a theme: those of :root, overridden by the theme's own rule set."""
    css = (UI_DIR / "style.css").read_text(encoding="utf-8")
    values = {}
    for selector in (r":root", rf':root\[data-theme="{re.escape(theme)}"\]'):
        block = re.search(_BLOCK.format(selector), css, re.M | re.S)
        if block:
            values.update((name, value.strip()) for name, value in _VAR.findall(block.group(1)))
    return values


@cache
def icon_svg(theme: str) -> str:
    """Return ui/icon.svg in the colours of a theme."""
    svg = (UI_DIR / "icon.svg").read_text(encoding="utf-8")
    drawn, colours = palette(THEME_IDS[0]), palette(theme)
    variable = {drawn[name]: name for name in ICON_VARIABLES}  # Each colour of the file, by the variable it is
    return re.sub("|".join(map(re.escape, variable)), lambda m: colours[variable[m.group(0)]], svg)


def icon_path(theme: str) -> Path:
    """Return the icon file of a theme (made by scripts/make_icon.py), for the window and the taskbar."""
    return UI_DIR / ("icon.ico" if theme == THEME_IDS[0] else f"icons/{theme}.ico")
