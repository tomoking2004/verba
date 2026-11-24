"""Render every size straight from ui/icon.svg (the source) and pack them into ui/icon.ico. Called by build.ps1."""

import io
from pathlib import Path

import resvg_py
from PIL import Image

UI = Path(__file__).resolve().parent.parent / "ui"
SIZES = [256, 64, 48, 40, 32, 24, 20, 16]


def render(svg: str, size: int) -> Image.Image:
    """Render the SVG at one size."""
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size))
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main() -> None:
    """Write ui/icon.ico."""
    svg = (UI / "icon.svg").read_text(encoding="utf-8")
    images = [render(svg, s) for s in SIZES]
    images[0].save(UI / "icon.ico", format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[1:])


if __name__ == "__main__":
    main()
