"""Curved glass optics and translucent surfaces; no desktop capture.

A cached Snell refraction mesh is sampled by Pillow in C. This is an optical
approximation for our Canvas renderer, not Apple's native material.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError:
    Image = None


@dataclass(frozen=True)
class Palette:
    name: str
    background: str
    text: str
    secondary: str
    muted: str
    panel: str
    line: str
    accent: str
    accent2: str
    wave: str
    wave2: str
    dark: bool = False


PALETTES = {
    "silver": Palette("冰川银", "#EDF2F7", "#1B3046", "#4E6379", "#63768B", "#F5F8FB", "#CFDBE6", "#296CA8", "#7776B1", "#B7CCDF", "#DFE9F0"),
    "midnight": Palette("极夜蓝", "#111D2F", "#F0F5FC", "#C4D3E7", "#9CACBF", "#1A2B41", "#324760", "#9CD7FD", "#BEBAEB", "#2B5780", "#1A354D", True),
    "smoke": Palette("烟晶灰", "#252B33", "#F0F3F6", "#CDD3DC", "#A5AFBC", "#303842", "#4B5662", "#D2DFEE", "#B5BED4", "#647586", "#424F5C", True),
    "sea": Palette("海盐青", "#E5F1EC", "#163F3B", "#42665F", "#55776F", "#F1F8F4", "#C4DAD1", "#1E8278", "#6C9994", "#9EC7BE", "#D7E9D9"),
    "iris": Palette("鸢尾紫", "#29253E", "#F6F1FF", "#D5CDE9", "#B2A7C9", "#37324E", "#514862", "#D2BBFF", "#E6BCCF", "#76619D", "#443456", True),
    "rose": Palette("玫瑰雾", "#F2E8E8", "#563746", "#785767", "#8A6976", "#FAF2F1", "#DFCACE", "#A55273", "#A58B79", "#D8A9B5", "#EAD6CE"),
}


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, amount):
    return tuple(round(x + (y - x) * amount) for x, y in zip(rgb(a), rgb(b)))


def edge(x, y, width, height, radius):
    """Signed rounded-rectangle distance and outward unit normal."""
    qx, qy = abs(x - width / 2) - (width / 2 - radius), abs(y - height / 2) - (height / 2 - radius)
    ax, ay = max(qx, 0), max(qy, 0)
    length = math.hypot(ax, ay)
    distance = length + min(max(qx, qy), 0) - radius
    nx, ny = ((ax / length, ay / length) if length else (1., 0.) if qx > qy else (0., 1.))
    return distance, nx * (-1 if x < width / 2 else 1), ny * (-1 if y < height / 2 else 1)


class GlassRenderer:
    SCALE = 2

    def __init__(self):
        self._meshes = OrderedDict()
        self._surfaces = OrderedDict()

    @staticmethod
    def _retain(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def backdrop(self, width, height, palette, *, desktop=False):
        key = (width, height, palette, desktop)
        if key in self._surfaces:
            return self._surfaces[key]
        s = self.SCALE
        w, h = width * s, height * s
        if desktop:
            # The tint is translucent. DWM supplies the changing desktop behind
            # it; opaque text is composited independently by monitor_window.
            image = Image.new('RGBA', (w, h), rgb(palette.background) + (184 if palette.dark else 166,))
            return self._retain(self._surfaces, key, image, 6)
        image = Image.new("RGB", (w, h), palette.background)
        d = ImageDraw.Draw(image)
        # A quiet center for data, with an asymmetric glass-lit fold below it.
        for yy in range(h):
            fade = (yy / h) ** 2 * (.07 if palette.dark else .12)
            d.line((0, yy, w, yy), fill=mix(palette.background, palette.wave, fade))
        soft = Image.new("RGBA", (w, h))
        sd = ImageDraw.Draw(soft)
        sd.ellipse((w * .53, -h * .3, w * 1.5, h * .55), fill=rgb(palette.wave) + (36,))
        image = image.convert("RGBA")
        image.alpha_composite(soft.filter(ImageFilter.GaussianBlur(48 * s)))
        d = ImageDraw.Draw(image)
        # Curved, continuous strands provide visible material cues behind the dock.
        for index, (offset, thickness, tint) in enumerate(((-10, 29, .72), (15, 14, .4), (35, 5, .3))):
            points = []
            for xx in range(-10, w + 11, 3):
                u = xx / w
                yy = h - (42 + 130 * (u - .12) ** 2 + offset) * s
                points.append((xx, yy))
            half = thickness * s / 2
            d.polygon([(x, y - half) for x, y in points] + [(x, y + half) for x, y in reversed(points)],
                      fill=mix(palette.background, palette.wave if index != 1 else palette.wave2, tint))
            if index == 0:
                d.line([(x, y - thickness * s / 2) for x, y in points],
                       fill=mix(palette.background, "#FFFFFF", .2 if palette.dark else .85), width=s)
        return self._retain(self._surfaces, key, image, 6)

    def _geometry(self, width, height, radius, strength=1.):
        key = (width, height, radius, strength)
        if key in self._meshes:
            self._meshes.move_to_end(key)
            return self._meshes[key]
        s, bezel = self.SCALE, min(19., height * .36, radius * .75)
        w, h, r = width * s, height * s, radius * s
        pad = 12 * s

        def refract(x, y):
            distance, nx, ny = edge(x / s, y / s, width, height, radius)
            t = min(1., max(.001, -distance / bezel))
            if distance > 0 or t >= 1:
                return x + pad, y + pad
            surface = max(.0001, 1 - (1 - t) ** 4)
            slope = (22 / bezel) * (1 - t) ** 3 / surface ** .75
            normal_z = 1 / math.sqrt(1 + slope * slope)
            eta = 1 / 1.5
            term = eta * normal_z - math.sqrt(1 - eta * eta * (1 - normal_z * normal_z))
            lateral = term * slope * normal_z
            vertical = -eta + term * normal_z
            shift = (7 + 22 * surface ** .25) * lateral / -vertical * strength
            return x + nx * shift * s + pad, y + ny * shift * s + pad

        step, mesh = 6, []
        # The flat middle is translationally invariant; only the curved ends
        # need dense horizontal cells.
        cuts = sorted(set([0, w, int(r), w - int(r)] + list(range(0, math.ceil(r), step)) +
                          list(range(w - math.ceil(r), w, step))))
        points = {}
        for y in range(0, h, step):
            for x, x2 in zip(cuts, cuts[1:]):
                y2 = min(h, y + step)
                quad = []
                for point in ((x, y), (x, y2), (x2, y2), (x2, y)):
                    if point not in points:
                        points[point] = refract(*point)
                    quad.extend(points[point])
                mesh.append(((x, y, x2, y2), tuple(quad)))
        mask = Image.new("L", (w, h))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
        highlights, opposite = Image.new("RGBA", (w, h)), Image.new("RGBA", (w, h))
        pixels, back = highlights.load(), opposite.load()
        for y in range(h):
            for x in range(w):
                distance, nx, ny = edge((x + .5) / s, (y + .5) / s, width, height, radius)
                inside = -distance
                if not 0 <= inside <= 5:
                    continue
                rim = max(0, 1 - abs(inside - .65) / .9)
                soft = math.exp(-((inside - 2.8) / 1.3) ** 2) * .18
                light = max(0., -nx * .65 - ny * .76) ** 4
                other = max(0., nx * .76 + ny * .65) ** 6
                pixels[x, y] = (255, 255, 255, round((rim + soft) * (35 + 205 * light)))
                back[x, y] = (244, 250, 255, round((rim + soft) * (28 + 178 * other)))
        shape = (mesh, mask, highlights, opposite)
        return self._retain(self._meshes, key, shape, 28)

    def render(self, source, x, y, width, height, radius, palette, light=0., pressure=0.,
               blur_radius=4., strength=1.):
        s, pad = self.SCALE, 12 * self.SCALE
        width, height = max(4, int(round(width))), max(4, int(round(height)))
        radius = round(min(radius, width / 2, height / 2), 1)
        mesh, mask, first, second = self._geometry(width, height, radius, strength)
        xx, yy, w, h = int(round(x * s)), int(round(y * s)), width * s, height * s
        crop = source.crop((xx - pad, yy - pad, xx + w + pad, yy + h + pad)).convert("RGBA")
        lens = crop.transform((w, h), Image.Transform.MESH, mesh, Image.Resampling.BICUBIC)
        # Diffuse only transmission; highlights are applied afterwards.
        lens = lens.filter(ImageFilter.GaussianBlur(blur_radius * s)) if blur_radius else lens
        tint = Image.new("RGBA", (w, h), (235, 246, 255, 15 if palette.dark else 29))
        lens.alpha_composite(tint)
        lens.alpha_composite(Image.blend(first, second, max(0., min(1., .3 + light * .22))))
        if pressure > 0:
            glow = Image.new("RGBA", (w, h), (255, 255, 255, round(min(1., pressure) * 19)))
            lens.alpha_composite(glow)
        lens.putalpha(ImageChops.multiply(lens.getchannel('A'), mask))
        result = Image.new("RGBA", (w + pad * 2, h + pad * 2))
        shadow = Image.new("RGBA", result.size)
        shadow_alpha = Image.new("L", result.size)
        shadow_alpha.paste(mask.point(lambda v: round(v * (.25 if palette.dark else .16))), (pad, pad + 4 * s))
        shadow.putalpha(shadow_alpha.filter(ImageFilter.GaussianBlur(5 * s)))
        result.alpha_composite(shadow)
        result.alpha_composite(lens, (pad, pad))
        return result.resize((width + 24, height + 24), Image.Resampling.LANCZOS)
