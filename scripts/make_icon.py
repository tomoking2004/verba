"""Render every size straight from ui/icon.svg (the source) and pack them into ui/icon.ico, and into
ui/icons/<theme>.ico for each of the other colour themes (verba/themes.py dresses the SVG). Called by build.ps1."""

import io
import sys
from pathlib import Path

import resvg_py
from PIL import Image

sys.dont_write_bytecode = True  # Importing verba must not leave __pycache__ in the repository.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from verba.config import THEME_IDS  # noqa: E402
from verba.themes import icon_path, icon_svg  # noqa: E402

SIZES = [256, 64, 48, 40, 32, 24, 20, 16]


def render(svg: str, size: int) -> Image.Image:
    """Render the SVG at one size."""
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size))
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main() -> None:
    """Write the icon of every theme."""
    for theme in THEME_IDS:
        svg, path = icon_svg(theme), icon_path(theme)
        path.parent.mkdir(exist_ok=True)
        images = [render(svg, s) for s in SIZES]
        images[0].save(path, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[1:])


if __name__ == "__main__":
    main()
