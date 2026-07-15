from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "token-pulse.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def icon_image(size: int) -> Image.Image:
    scale = size / 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = round(18 * scale)
    radius = round(48 * scale)
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill="#151B1F",
        outline="#3B4A50",
        width=max(1, round(5 * scale)),
    )

    center = size / 2
    points = [
        (42, 137),
        (77, 137),
        (96, 103),
        (119, 178),
        (144, 77),
        (164, 137),
        (214, 137),
    ]
    scaled_points = [(round(x * scale), round(y * scale)) for x, y in points]
    draw.line(
        scaled_points,
        fill="#55E3B0",
        width=max(1, round(10 * scale)),
        joint="curve",
    )
    glow = max(1, round(4 * scale))
    for x, y in scaled_points:
        draw.ellipse((x - glow, y - glow, x + glow, y + glow), fill="#8AE9C8")
    return image


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    images = [icon_image(size) for size in SIZES]
    images[-1].save(
        OUTPUT,
        format="ICO",
        append_images=images[:-1],
        sizes=[(size, size) for size in SIZES],
    )


if __name__ == "__main__":
    main()
