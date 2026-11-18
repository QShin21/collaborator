"""Draw the icon of the program and of its installer: the figure's face.

    python packaging/windows/make_icon.py OUT.ico

Build-time only (needs Pillow, which the Windows build installs anyway for the tray
icon). The same face is drawn at run time by banxing/ui/app.py; no image file is kept
in the repository.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def face(size=256):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 64.0

    def box(left, top, right, bottom):
        return tuple(int(round(value * unit)) for value in (left, top, right, bottom))

    draw.ellipse(box(3, 3, 61, 61), fill=(79, 140, 255, 255))
    for x in (23, 41):
        draw.ellipse(box(x - 7, 20, x + 7, 34), fill=(255, 255, 255, 255))
        draw.ellipse(box(x - 3, 24, x + 3, 30), fill=(31, 35, 40, 255))
    draw.arc(box(22, 34, 42, 48), start=20, end=160, fill=(255, 255, 255, 255), width=max(1, int(round(3 * unit))))
    return image


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    target = Path(argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    face().save(target, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
