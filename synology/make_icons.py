"""Render the package / desktop icons. Run once; the PNGs are committed."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 1024


def render():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # vertical gradient inside a rounded square
    grad = Image.new("RGBA", (S, S))
    top, bottom = (92, 150, 255), (40, 92, 214)
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    w = 56
    # antenna
    d.line([(390, 250), (512, 360)], fill=white, width=w, joint="curve")
    d.line([(634, 250), (512, 360)], fill=white, width=w, joint="curve")
    for x, y in ((390, 250), (634, 250)):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=white)
    # screen
    d.rounded_rectangle([210, 360, 814, 790], radius=70, outline=white, width=w)
    # play glyph
    d.polygon([(450, 470), (450, 680), (630, 575)], fill=white)
    return img


def main():
    big = render()
    big.resize((256, 256), Image.LANCZOS).save(HERE / "PACKAGE_ICON_256.PNG")
    big.resize((64, 64), Image.LANCZOS).save(HERE / "PACKAGE_ICON.PNG")
    out = HERE / "ui" / "images"
    out.mkdir(parents=True, exist_ok=True)
    for size in (16, 24, 32, 48, 64, 72, 256):
        big.resize((size, size), Image.LANCZOS).save(out / f"tv-adb-{size}.png")


if __name__ == "__main__":
    main()
