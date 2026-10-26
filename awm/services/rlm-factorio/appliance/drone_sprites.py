"""Build the drone body's sprite sheets from the base construction robot.

The robot sheets hold 16 directions in two rows (idle, in motion); the
character prototype wants 8 directions per animation, and 18 for running with
a gun. Each output sheet is one row with one frame per direction, painted
near-black gray. Rerun only when the art changes:

    mamba run -n awm python appliance/drone_sprites.py [image]
"""
import io
import subprocess
import sys
from pathlib import Path

from PIL import Image

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "rlm-factorio/appliance:2.1.20"
SRC = "/opt/factorio/data/base/graphics/entity/construction-robot"
OUT = Path(__file__).parent / "mods" / "game-bot-control" / "graphics"
ROBOT_DIRECTIONS = 16


def extract(name: str) -> Image.Image:
    png = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", IMAGE, f"{SRC}/{name}"],
        check=True, capture_output=True).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def paint_dark(frame: Image.Image) -> Image.Image:
    """Grayscale the body near-black; keep the red beacon light."""
    out = frame.copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, a = px[x, y]
            if a and r > 150 and g < 90 and b < 90:
                continue
            lum = (299 * r + 587 * g + 114 * b) // 1000
            v = 14 + lum * 30 // 100
            px[x, y] = (v, v, v + 4, a)
    return out


def sheet(src: Image.Image, row: int, directions: int, dark: bool) -> Image.Image:
    w, h = src.width // ROBOT_DIRECTIONS, src.height // 2
    out = Image.new("RGBA", (w * directions, h))
    for i in range(directions):
        d = round(i * ROBOT_DIRECTIONS / directions) % ROBOT_DIRECTIONS
        frame = src.crop((d * w, row * h, (d + 1) * w, (row + 1) * h))
        out.paste(paint_dark(frame) if dark else frame, (i * w, 0))
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    body = extract("construction-robot.png")
    shadow = extract("construction-robot-shadow.png")
    for name, row, directions in (("idle", 0, 8), ("running", 1, 8),
                                  ("running-gun", 1, 18)):
        sheet(body, row, directions, True).save(OUT / f"drone-{name}.png", optimize=True)
        sheet(shadow, row, directions, False).save(
            OUT / f"drone-{name}-shadow.png", optimize=True)
    print(f"wrote {len(list(OUT.glob('drone-*.png')))} sheets to {OUT}")


if __name__ == "__main__":
    main()
