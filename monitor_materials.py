"""Small, cached antialiased primitives for the desktop Canvas.

Geometry stays in logical Canvas coordinates; hit testing stays with the view.
Pillow is optional, just as it is for the monitor's other drawing helpers.
"""
from __future__ import annotations

from collections import OrderedDict
import math


class CanvasMaterials:
    SCALE = 3

    def __init__(self, canvas, api):
        self.canvas, self.api = canvas, api
        self.cache = OrderedDict()
        self.frame = []

    def begin_frame(self):
        self.frame.clear()

    def _image(self, key, width, height, draw):
        if key not in self.cache:
            image = self.api.Image.new("RGBA", (width * self.SCALE, height * self.SCALE))
            draw(image, self.api.ImageDraw.Draw(image), self.SCALE)
            self.cache[key] = self.api.ImageTk.PhotoImage(
                image.resize((width, height), self.api.Image.LANCZOS), master=self.canvas)
            if len(self.cache) > 192:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        image = self.cache[key]
        self.frame.append(image)
        return image

    def rounded(self, x1, y1, x2, y2, fill, radius, outline="", *, material=False,
                shadow=False, tags=()):
        w, h = max(1, math.ceil(x2 - x1)), max(1, math.ceil(y2 - y1))
        radius = min(radius, w / 2, h / 2)
        if self.api.Image is None:
            points = (x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
                      x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
                      x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1)
            return self.canvas.create_polygon(points, smooth=True, fill=fill,
                                               outline=outline, tags=tags)
        pad = 10 if shadow else 2
        key = ("rounded", w, h, fill, radius, outline, material, shadow)

        def render(image, draw, s):
            bounds = (pad * s, pad * s, (pad + w) * s - 1, (pad + h) * s - 1)
            if shadow:
                layer = self.api.Image.new("RGBA", image.size)
                self.api.ImageDraw.Draw(layer).rounded_rectangle(
                    (bounds[0], bounds[1] + 3 * s, bounds[2], bounds[3] + 3 * s),
                    radius=radius * s, fill=(0, 0, 0, 72))
                image.alpha_composite(layer.filter(self.api.ImageFilter.GaussianBlur(4 * s)))
            if fill:
                draw.rounded_rectangle(bounds, radius=radius * s, fill=fill)
            if material and fill:
                mask = self.api.Image.new("L", image.size)
                self.api.ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=radius * s, fill=255)
                light = self.api.Image.new("RGBA", image.size)
                ld = self.api.ImageDraw.Draw(light)
                for yy in range(h * s):
                    fraction = yy / max(1, h * s - 1)
                    alpha = round(12 * (1 - fraction) ** 2)
                    ld.line((bounds[0], bounds[1] + yy, bounds[2], bounds[1] + yy),
                            fill=(225, 236, 255, alpha))
                clipped = self.api.Image.new("RGBA", image.size)
                clipped.paste(light, mask=mask)
                image.alpha_composite(clipped)
            if outline:
                draw.rounded_rectangle(bounds, radius=radius * s, outline=outline, width=2)

        image = self._image(key, w + pad * 2, h + pad * 2, render)
        return self.canvas.create_image(x1 - pad, y1 - pad, image=image, anchor="nw", tags=tags)

    def icon(self, name, x, y, color, tags=()):
        # The same 20 px grid and 1.65 px stroke govern every control icon.
        paths = {
            "close": [(-4, -4, 4, 4), (4, -4, -4, 4)],
            "width": [(-7, 0, 7, 0), (-4, -3, -7, 0, -4, 3), (4, -3, 7, 0, 4, 3)],
            "pin": [(-3.5, -6, 3.5, -6, 2.5, -1, 5, 2, -5, 2, -2.5, -1, -3.5, -6), (0, 2, 0, 7)],
            "stats": [(-5, 5, -5, 0), (0, 5, 0, -6), (5, 5, 5, -3)],
            "refresh": [(2, -6, 6, -6, 6, -2)],
            "chevron": [(-2, -4, 2, 0, -2, 4)],
            "accounts": [(-6, 6, -6, 4, -4, 1, 4, 1, 6, 4, 6, 6)],
            "live": [],
        }
        paths = paths.get(name, [])
        if self.api.Image is None:
            for path in paths:
                coords = [value + (x if i % 2 == 0 else y) for i, value in enumerate(path)]
                self.canvas.create_line(*coords, fill=color, width=1.65,
                                        capstyle="round", joinstyle="round", tags=tags)
            if name in {"live", "accounts"}:
                radius, cy = (6, y) if name == "live" else (3, y - 4)
                self.canvas.create_oval(x - radius, cy - radius, x + radius, cy + radius,
                                        outline=color, width=1.65, tags=tags)
            if name == "refresh":
                self.canvas.create_arc(x - 6, y - 6, x + 6, y + 6, start=35, extent=290,
                                       style="arc", outline=color, width=1.65, tags=tags)
            if name == "palette":
                for dx, dy in ((0, -5), (-4.5, 3), (4.5, 3)):
                    self.canvas.create_oval(x + dx - 3, y + dy - 3, x + dx + 3, y + dy + 3,
                                            outline=color, width=1.3, tags=tags)
            return

        def render(image, draw, s):
            for path in paths:
                pairs = [((path[i] + 12) * s, (path[i + 1] + 12) * s) for i in range(0, len(path), 2)]
                draw.line(pairs, fill=color, width=5, joint="curve")
                for xx, yy in pairs:
                    draw.ellipse((xx - 2, yy - 2, xx + 2, yy + 2), fill=color)
            if name == "live":
                draw.ellipse((6 * s, 6 * s, 18 * s, 18 * s), outline=color, width=5)
                draw.ellipse((10 * s, 10 * s, 14 * s, 14 * s), fill=color)
            elif name == "accounts":
                draw.ellipse((9 * s, 5 * s, 15 * s, 11 * s), outline=color, width=5)
            elif name == "refresh":
                draw.arc((6 * s, 6 * s, 18 * s, 18 * s), start=25, end=312, fill=color, width=5)
            elif name == "palette":
                for xx, yy in ((12, 7), (7.5, 15), (16.5, 15)):
                    draw.ellipse(((xx - 3.1) * s, (yy - 3.1) * s, (xx + 3.1) * s, (yy + 3.1) * s), outline=color, width=4)

        image = self._image(("icon", name, color), 24, 24, render)
        self.canvas.create_image(x, y, image=image, tags=tags)
