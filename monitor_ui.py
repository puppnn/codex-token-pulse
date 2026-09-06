"""Orbit: an iOS-inspired, portrait Canvas view over Token Pulse's existing state.

The ring represents hourly usage, never an invented quota or interpolated samples.
Collection, account attribution and refresh scheduling remain in monitor.py.
"""
from __future__ import annotations

import math
import os
import json
import logging
import time
import tkinter.font as tkfont
from collections import OrderedDict
from types import SimpleNamespace

from monitor_materials import CanvasMaterials
from monitor_glass import GlassRenderer, PALETTES, rgb
from monitor_window import DesktopCompositor


class WorkspaceUI:
    BG = "#10141D"
    PANEL = "#1B2230"
    LINE = "#2A3241"
    GRID = "#232C3B"
    TEXT = "#F3F6FC"
    SECONDARY = "#C0CADB"
    MUTED = "#909EB4"
    CYAN = "#AEDAFF"
    VIOLET = "#BEB2F5"
    PINK = "#E2BDDF"
    LIVE = "#91D9C3"
    WARN = "#E9C293"
    ERROR = "#FF9EAE"

    def __init__(self, app, api):
        self.a, self.m, self.c = app, api, app.canvas
        self.library_open = False
        self.active_only = False
        self.compact_analysis = "models"
        self.focused = None
        self.models_rect = None
        self.ring_geometry = None
        self._surface_cache = {}
        self._ring_cache = {}
        self.paint = CanvasMaterials(self.c, api)
        self._fit_fonts = {}
        self._animation_id = None
        self._nav_position = self._nav_target = self._nav_from = None
        self._nav_started = 0.
        self._nav_width = None
        self._pressed = None
        self._press_until = 0.
        self._recent_text = None
        self._number_started = 0.
        self._number_y = 0.
        self.reduced_motion = self._prefers_reduced_motion()
        self.glass = GlassRenderer() if api.Image is not None else None
        self.appearance_open = False
        self._appearance_path = api.APP_DIR / "monitor_appearance.json"
        self.theme_name = "silver"
        self.material_name = "liquid"
        self.blur_radius = 4.0
        self.refraction_strength = 1.0
        self._slider_drag = None
        self._sliders = {}
        self._light = self._light_target = 0.
        self._glass_cache = OrderedDict()
        self._glass_refs = {}
        self._ring_lens = None
        self._lens_drag = False
        self._control_press = False
        self._demo_position = None
        self._demo_pressure = self._demo_released_at = 0.
        self._dock_stretch = 0.
        self._last_glass_frame = 0.
        try:
            saved = json.loads(self._appearance_path.read_text(encoding="utf-8"))
            self.theme_name = saved.get("theme") if saved.get("theme") in PALETTES else "silver"
            self.material_name = saved.get('material', 'liquid') if saved.get('material', 'liquid') in {'liquid', 'alpha', 'opaque'} else 'liquid'
            self.blur_radius = self._bounded_setting(saved.get('blur', 4.), 4., 10.)
            self.refraction_strength = self._bounded_setting(saved.get('refraction', 1.), 1., 2.)
            self.reduced_motion |= not saved.get("motion", True)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._set_palette(self.theme_name)
        self.desktop = getattr(app, '_desktop_compositor', None)
        if self.desktop is None and api.Image is not None:
            self.desktop = DesktopCompositor.create(app.root, self.c, self._desktop_failed, self.material_name)
            app._desktop_compositor = self.desktop
        self._apply_optics()
        if self.material_name == 'opaque' and not (self.desktop and self.desktop.active):
            app.WINDOW_ALPHA = 1.0
            app.root.attributes('-alpha', 1.0)
        families = set(tkfont.families(root=app.root))
        display = "Segoe UI Semibold" if "Segoe UI Semibold" in families else "Segoe UI"
        specs = {
            "title": (display, 20, "normal"),
            "page_title": ("Microsoft YaHei UI", 28, "bold"),
            "hero": (display, 50, "normal"),
            "number": (display, 42, "normal"),
            "value": (display, 24, "normal"),
            "row_value": (display, 17, "normal"),
            "heading": ("Microsoft YaHei UI", 15, "bold"),
            "body": ("Microsoft YaHei UI", 14, "normal"),
            "strong": ("Microsoft YaHei UI", 13, "bold"),
            "caption": ("Microsoft YaHei UI", 12, "normal"),
            "data": ("Segoe UI", 13, "normal"),
            "micro": ("Segoe UI", 11, "normal"),
            "nav": ("Microsoft YaHei UI", 12, "normal"),
        }
        self.fonts = {key: tkfont.Font(root=app.root, family=family, size=-size, weight=weight)
                      for key, (family, size, weight) in specs.items()}
        app.root.bind("<Tab>", lambda _e: self.move_focus(1))
        app.root.bind("<Shift-Tab>", lambda _e: self.move_focus(-1))
        app.root.bind("<Return>", lambda _e: self.activate_focus())
        app.root.bind("<Left>", lambda _e: self.adjust_focus(-1))
        app.root.bind("<Right>", lambda _e: self.adjust_focus(1))
        app.root.bind("<Home>", lambda _e: self.adjust_focus(-1, endpoint=True))
        app.root.bind("<End>", lambda _e: self.adjust_focus(1, endpoint=True))
        app.root.bind("<Destroy>", self._on_destroy, add="+")
        app.root.protocol("WM_DELETE_WINDOW", app.close_app)
        app.root.bind("<Escape>", self._dismiss_appearance, add="+")

    def _set_palette(self, name):
        self.theme_name, self.palette = name, PALETTES[name]
        p = self.palette
        self.BG, self.TEXT, self.SECONDARY, self.MUTED = p.background, p.text, p.secondary, p.muted
        self.PANEL, self.LINE, self.GRID = p.panel, p.line, self.blend(p.background, p.line, .58)
        self.CYAN, self.VIOLET, self.PINK = p.accent, p.accent2, self.blend(p.accent2, p.text, .25)
        self.LIVE = "#91D9C3" if p.dark else "#267B67"
        self.WARN, self.ERROR = ("#E9C293", "#FF9EAE") if p.dark else ("#906127", "#BC3D54")
        self._surface_cache.clear()
        self._ring_cache.clear()
        self._glass_cache.clear()
        self._text_colors = {}

    @staticmethod
    def _bounded_setting(value, default, maximum):
        try:
            value = float(value)
            return min(maximum, max(0., value)) if math.isfinite(value) else default
        except (TypeError, ValueError, OverflowError):
            return default

    def _apply_optics(self):
        if self.desktop:
            self.desktop.blur_radius = self.blur_radius
            self.desktop.refraction_strength = self.refraction_strength

    def _save_appearance(self):
        try:
            self._appearance_path.write_text(json.dumps({
                "theme": self.theme_name, "motion": not self.reduced_motion, "material": self.material_name,
                "blur": self.blur_radius, "refraction": self.refraction_strength,
            }), encoding="utf-8")
        except OSError:
            pass

    def _desktop_failed(self, error):
        logging.getLogger('tokenpulse.monitor.glass').warning('Using opaque fallback: %s', error)
        self._surface_cache.clear()
        self._glass_cache.clear()
        self._text_colors.clear()
        self._orbit_key = self._appearance_key = None
        self.c.configure(bg=self.m.Theme.transparent)
        self.a.root.attributes('-transparentcolor', self.m.Theme.transparent)
        self.a.root.attributes('-alpha', 1.0 if self.material_name == 'opaque' else self.a.WINDOW_ALPHA)
        self.a.root.after_idle(self.a._draw)

    def _dismiss_appearance(self, _event=None):
        if self.appearance_open:
            self.appearance_open = False
            self._lens_drag = False
            self._slider_drag = None
            self._demo_pressure = self._demo_released_at = 0.
            self.a._draw()
            return "break"

    def _glass(self, name, x, y, width, height, radius, *, source=None, source_key=None, pressure=0.):
        pressure = 0. if self.reduced_motion else pressure
        if name == 'demo':
            stretch, squash = round(pressure * 14), round(pressure * 6)
            x, y = x - stretch / 2, y + squash / 2
            width, height = width + stretch, height - squash
        if self.desktop and self.desktop.active and self.desktop.refraction:
            radius = min(radius, width / 2, height / 2)
            self.desktop.lenses[name] = (x, y, width, height, radius, pressure)
            self.c.delete('liquid_' + name)
            return
        if self.material_name != 'liquid':
            tag = 'liquid_' + name
            self.c.delete(tag)
            # Flat materials have no fabricated lens displacement.
            self.box(x, y, x + width, y + height, self.PANEL, radius, self.LINE, tags=(tag,))
            if name == 'demo':
                self.text(x + width / 2, y + height / 2, 'Aa', 'value', self.SECONDARY, 'center', tags=(tag,))
            return
        if self.glass is None:
            return self.box(x, y, x + width, y + height, self.PANEL, radius, self.LINE)
        light = round(self._light * 6) / 6
        pressure = round(pressure * 6) / 6
        key = (self.theme_name, self.a.WIDTH, self.a.HEIGHT, round(x), round(y), round(width), round(height), radius, light, pressure, source_key, self.blur_radius, self.refraction_strength)
        if key not in self._glass_cache:
            picture = self.glass.render(source if source is not None else self._backdrop, x, y, width, height, radius,
                                        self.palette, light=light, pressure=pressure,
                                        blur_radius=self.blur_radius, strength=self.refraction_strength)
            if pressure:
                # A small anisotropic deformation makes the pressure response
                # visible without changing the control's hit target.
                picture = picture.resize((picture.width + round(pressure * 4), picture.height - round(pressure * 2)), self.m.Image.LANCZOS)
            self._glass_cache[key] = self.m.ImageTk.PhotoImage(picture, master=self.c)
            if len(self._glass_cache) > 64:
                self._glass_cache.popitem(last=False)
        self._glass_cache.move_to_end(key)
        image = self._glass_cache[key]
        self._glass_refs[name] = image
        tag = "liquid_" + name
        if self.c.find_withtag(tag):
            self.c.itemconfigure(tag, image=image)
            self.c.coords(tag, x - 12 - pressure * 2, y - 12 + pressure)
        else:
            self.c.create_image(x - 12 - pressure * 2, y - 12 + pressure, image=image, anchor="nw", tags=(tag,))

    def finish(self):
        self.navigation()
        self._draw_following_lens()

    def appearance(self):
        l, r, t = self.left, self.right, self.top
        self.ring_geometry = None
        self._sliders.clear()
        self.text(l, t + 4, "外观", "page_title")
        self.text(l, t + 49, "配色", "caption", self.MUTED)
        self.button("appearance_done", r - 52, t + 6, r, t + 36, "完成", quiet=True)
        top, row_h, gap = t + 73, (34 if self.small else 42), 8
        cell_w = (r - l - 2 * gap) / 3
        for index, (key, palette) in enumerate(PALETTES.items()):
            x, y = l + index % 3 * (cell_w + gap), top + index // 3 * (row_h + gap)
            selected = self.theme_name == key
            name = "theme_" + key
            self.a._btn_rects[name] = (x, y, x + cell_w, y + row_h)
            self.box(x, y, x + cell_w, y + row_h, self.PANEL, 12,
                     self.CYAN if selected or self.focused == name else self.LINE)
            self.dot(x + 16, y + row_h / 2, 9, palette.background)
            self.dot(x + 19, y + row_h / 2 + 2, 5, palette.wave)
            self.text(x + 32, y + row_h / 2, palette.name, "caption", self.TEXT, "w")
        material_y = self.bottom - 214
        demo_y = top + 2 * row_h + gap + 12
        demo_h = min(144, material_y - 30 - demo_y)
        self._demo_rect = (l, demo_y, r, demo_y + demo_h)
        self.box(l, demo_y, r, demo_y + demo_h, '', 18, self.LINE)
        live_glass = self.material_name == 'liquid' and self.desktop and self.desktop.active and self.desktop.refraction
        if self.glass and (live_glass or self.material_name in {'alpha', 'opaque'}):
            px, py = self._demo_position or ((l + r) / 2, demo_y + demo_h / 2)
            self._demo_position = (max(l + 64, min(r - 64, px)), max(demo_y + 28, min(demo_y + demo_h - 28, py)))
            caption = {'liquid': '拖动透镜 · 实时桌面预览', 'alpha': '拖动预览 · 半透明，无折射',
                       'opaque': '拖动预览 · 纯色，不透底'}[self.material_name]
        else:
            self._demo_position = None
            caption = {'alpha': '半透明材质 · 无曲面折射', 'opaque': '纯色材质 · 不透出桌面'}.get(self.material_name, '曲面折射暂不可用，已回退')
            self.text((l + r) / 2, demo_y + demo_h / 2, 'Token Pulse', 'value', self.SECONDARY, 'center')
        if self.material_name == 'alpha' and not (self.desktop and self.desktop.active):
            caption = '半透明未启用 · 纯色回退'
        self.text(l, demo_y + demo_h + 6, caption, 'caption', self.MUTED)
        self.text(l, material_y, '窗口材质', 'caption', self.MUTED)
        step = (r - l) / 3
        for index, (key, label) in enumerate((('liquid', '曲面折射'), ('alpha', '半透明'), ('opaque', '纯色'))):
            x = l + index * step
            self.button('material_' + key, x, material_y + 23, x + step - (4 if index < 2 else 0),
                        material_y + 55, label, selected=self.material_name == key)
        self._appearance_slider('blur', '背景柔焦', self.bottom - 147, self.blur_radius / 10, f'{self.blur_radius * 10:.0f}%', bool(live_glass))
        self._appearance_slider('refraction', '折射强度', self.bottom - 91, self.refraction_strength / 2, f'{self.refraction_strength * 100:.0f}%', bool(live_glass))
        if not live_glass:
            self.a._add_tooltip(l, self.bottom - 149, r, self.bottom - 43, '两项调节在曲面折射材质启用后生效，切换材质会保留数值。')
        y = self.bottom - 35
        self.a._btn_rects['appearance_motion'] = (l, y, r, self.bottom)
        self.text(l, y, '交互动效 · ' + ('关闭' if self.reduced_motion else '开启'), 'body')
        self.text(l, y + 20, '按压回弹与' + ('随光变化' if live_glass else '界面过渡'), 'caption', self.MUTED)
        self.box(r - 57, y + 2, r, y + 30, self.LINE if self.reduced_motion else self.CYAN, 14,
                 self.TEXT if self.focused == 'appearance_motion' else '')
        self.dot(r - (43 if self.reduced_motion else 14), y + 16, 10, '#FFFFFF')

    def _appearance_slider(self, name, label, y, fraction, value, enabled):
        l, r = self.left, self.right
        color = self.CYAN if enabled else self.MUTED
        self.text(l, y, label, 'body', self.TEXT if enabled else self.MUTED)
        self.text(r, y, value, 'data', color, 'ne')
        x1, x2, cy = l + 10, r - 10, y + 31
        self.box(x1, cy - 2, x2, cy + 2, self.LINE, 2)
        x = x1 + (x2 - x1) * fraction
        if x > x1:
            self.box(x1, cy - 2, x, cy + 2, color if enabled else self.LINE, 2)
        self.dot(x, cy + 1, 9, self.LINE)
        self.dot(x, cy, 8, '#FFFFFF' if enabled else self.PANEL)
        if self.focused == 'slider_' + name and enabled:
            self.c.create_oval(x - 11, cy - 11, x + 11, cy + 11, outline=self.CYAN, width=1)
        if enabled:
            self._sliders[name] = (x1, cy - 14, x2, cy + 14)
            self.a._btn_rects['slider_' + name] = (l, cy - 14, r, cy + 14)

    def _set_slider(self, name, fraction, *, save=False):
        fraction = min(1., max(0., fraction))
        if name == 'blur':
            self.blur_radius = round(fraction * 10, 1)
        else:
            self.refraction_strength = round(fraction * 40) / 20
        self._apply_optics()
        if save:
            self._save_appearance()
        self.a._draw()

    def adjust_focus(self, direction, endpoint=False):
        name = (self.focused or '').removeprefix('slider_')
        if self.appearance_open and name in self._sliders:
            fraction = self.blur_radius / 10 if name == 'blur' else self.refraction_strength / 2
            fraction = float(direction > 0) if endpoint else fraction + direction * (.01 if name == 'blur' else .025)
            self._set_slider(name, fraction, save=True)
            return 'break'

    def _draw_following_lens(self):
        if not self.glass:
            return
        if self.appearance_open and self._demo_position:
            x, y = self._demo_position
            self._glass("demo", x - 57, y - 24, 114, 48, 24, pressure=self._demo_pressure)
        elif self.ring_geometry and self._ring_lens:
            x, y = self._ring_lens
            self._glass("hour", x - 23, y - 23, 46, 46, 23, source=self._orbit_source, source_key=self._orbit_key)
        else:
            self.c.delete("liquid_hour")

    def pointer_press(self, x, y):
        self._control_press = False
        if self.appearance_open:
            for name, (l, t, r, b) in self._sliders.items():
                if l - 10 <= x <= r + 10 and t <= y <= b:
                    self._slider_drag = name
                    self.focused = 'slider_' + name
                    self._set_slider(name, (x - l) / (r - l))
                    return True
        if self.appearance_open and self.glass and self._demo_position:
            px, py = self._demo_position
            if abs(x - px) <= 57 and abs(y - py) <= 28:
                self._lens_drag = True
                self._demo_pressure = 0. if self.reduced_motion else .85
                self._demo_released_at = 0.
                self._demo_drag_offset = (x - px, y - py)
                self._follow_pointer_light(x, y)
                self._draw_following_lens()
                return True
        return False

    def pointer_drag(self, x, y):
        if self._slider_drag:
            l, _t, r, _b = self._sliders[self._slider_drag]
            self._set_slider(self._slider_drag, (x - l) / (r - l))
            return True
        if not self._lens_drag:
            return self._control_press
        l, t, r, b = self._demo_rect
        dx, dy = self._demo_drag_offset
        self._demo_position = (max(l + 64, min(r - 64, x - dx)), max(t + 28, min(b - 28, y - dy)))
        self._follow_pointer_light(x, y)
        self._draw_following_lens()
        self._request_animation()
        return True

    def pointer_release(self):
        self._control_press = False
        if self._slider_drag:
            self._slider_drag = None
            self._save_appearance()
        if self._lens_drag:
            self._lens_drag = False
            if not self.reduced_motion:
                self._demo_released_at = time.monotonic()
                self._request_animation()
            self._draw_following_lens()

    def _follow_pointer_light(self, x, y):
        if self.reduced_motion:
            return
        if self.desktop and self.desktop.refraction:
            self.desktop.pointer = (float(x), float(y))
        if self.glass:
            left, right = (self._demo_rect[0], self._demo_rect[2]) if self.appearance_open else (0, self.a.WIDTH)
            self._light_target = max(-1., min(1., 2 * (x - left) / (right - left) - 1))
            if abs(self._light_target - self._light) > .02:
                self._request_animation()

    def pointer_motion(self, x, y):
        self._follow_pointer_light(x, y)
        if not self.glass:
            return
        before = self._ring_lens
        self._ring_lens = None
        if self.ring_geometry and not self.appearance_open:
            cx, cy, inner, outer, _bins = self.ring_geometry
            distance = math.hypot(x - cx, y - cy)
            if inner <= distance <= outer:
                angle = math.atan2(y - cy, x - cx)
                radius = (inner + outer) / 2 + 3
                self._ring_lens = (cx + math.cos(angle) * radius, cy + math.sin(angle) * radius)
        if self._ring_lens != before:
            if self.desktop:
                self.desktop.lenses.pop('hour', None)
            self._draw_following_lens()

    def pointer_leave(self):
        self._ring_lens = None
        if self.desktop:
            self.desktop.lenses.pop('hour', None)
        self.c.delete("liquid_hour")

    @staticmethod
    def blend(first, second, fraction):
        fraction = max(0., min(1., fraction))
        aa, bb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (first, second))
        return "#" + "".join(f"{round(a + (b - a) * fraction):02x}" for a, b in zip(aa, bb))

    def legible(self, color):
        if color in self._text_colors:
            return self._text_colors[color]
        def luminance(value):
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
        background = self.BG
        desktop = getattr(self, 'desktop', None)
        if desktop is not None and desktop.active and self.material_name != 'opaque':
            # Keep labels legible on the darkest/lightest possible desktop.
            # This does not need screen capture or change the selected palette.
            opacity = (184 if self.palette.dark else 166) / 255
            background = self.blend('#FFFFFF' if self.palette.dark else '#000000', self.BG, opacity)
        base = luminance(background)
        for amount in (0., .15, .3, .45, .6, .8, 1.):
            result = self.blend(color, self.TEXT, amount)
            value = luminance(result)
            if (max(base, value) + .05) / (min(base, value) + .05) >= 4.5:
                self._text_colors[color] = result
                return result
        if desktop is not None and desktop.active and self.material_name != 'opaque':
            for amount in (.15, .3, .6, 1.):
                result = self.blend(self.TEXT, '#FFFFFF' if self.palette.dark else '#000000', amount)
                value = luminance(result)
                if (max(base, value) + .05) / (min(base, value) + .05) >= 4.5:
                    self._text_colors[color] = result
                    return result
        return self.TEXT

    def text(self, x, y, value, role="body", color=None, anchor="nw", maxw=None, **kw):
        value = full = str(value)
        font = self.fitted_font(role, value, maxw)
        if maxw is not None and font.measure(value) > maxw:
            left, right = 0, len(value)
            while left < right:
                mid = (left + right + 1) // 2
                if font.measure(value[:mid] + "…") <= maxw:
                    left = mid
                else:
                    right = mid - 1
            value = value[:left] + "…"
        item = self.c.create_text(x, y, text=value, anchor=anchor, font=font,
                                  fill=self.legible(color or self.TEXT), **kw)
        if value != full:
            self.a._add_tooltip(*self.c.bbox(item), full)
        return item

    def fitted_font(self, role, value, maxw):
        font = self.fonts[role]
        if maxw and role in {"hero", "number", "value", "row_value"} and font.measure(value) > maxw:
            size = max(12, int(abs(font.cget("size")) * maxw / font.measure(value)) - 1)
            key = (role, size)
            if key not in self._fit_fonts:
                self._fit_fonts[key] = tkfont.Font(root=self.a.root, family=font.cget("family"),
                                                  size=-size, weight=font.cget("weight"))
            font = self._fit_fonts[key]
        return font

    def box(self, x1, y1, x2, y2, fill=None, radius=16, outline="", **kw):
        fill = fill if fill is not None else self.PANEL
        if self.desktop is not None and self.desktop.active and self.material_name != 'opaque' and fill == self.PANEL:
            fill = tuple(int(fill[i:i + 2], 16) for i in (1, 3, 5)) + (100 if self.desktop.refraction else 155,)
        return self.paint.rounded(x1, y1, x2, y2, fill,
                                  radius, outline, **kw)

    def icon(self, name, x, y, color, **kw):
        tags = kw.get('tags', ())
        kw['tags'] = ((tags,) if isinstance(tags, str) else tuple(tags)) + ('refraction_ink',)
        return self.paint.icon(name, x, y, color, **kw)

    def dot(self, x, y, radius, color):
        return self.box(x - radius, y - radius, x + radius, y + radius, color, radius)

    def button(self, name, x1, y1, x2, y2, label="", selected=False, tip="", quiet=False, icon=None):
        self.a._btn_rects[name] = (x1, y1, x2, y2)
        active = selected or self.a._hover_btn == name
        pressed = self._pressed == name and time.monotonic() < self._press_until
        inset = 1 if pressed else 0
        if active or pressed or not quiet or self.focused == name:
            self.box(x1 + inset, y1 + inset, x2 - inset, y2 - inset,
                     self.blend(self.PANEL, self.CYAN, .21 if pressed else .13) if active else self.PANEL,
                     min(15, (y2 - y1) / 2), self.CYAN if self.focused == name else "",
                     material=False, shadow=False)
        color = self.TEXT if selected else self.SECONDARY
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2 + inset
        if icon:
            self.icon(icon, cx, cy, self.CYAN if selected else color)
        else:
            self.text(cx, cy, label, "caption", color, "center")
        if tip:
            self.a._add_tooltip(x1, y1, x2, y2, tip)

    def _surface(self, width, height):
        m = self.m
        if m.Image is None:
            return None
        desktop = self.desktop is not None and self.desktop.active and self.material_name != 'opaque'
        curved = desktop and self.desktop.refraction
        key = (width, height, self.theme_name, desktop, curved, self.material_name)
        if key in self._surface_cache:
            return self._surface_cache[key]
        self._surface_cache.clear()
        scale = self.glass.SCALE
        w, h = int(width * scale), int(height * scale)
        self._backdrop = (m.Image.new('RGBA', (w, h), self.BG) if self.material_name == 'opaque'
                          else self.glass.backdrop(width, height, self.palette, desktop=desktop))
        if curved:
            # The GPU supplies the material. The Canvas holds foreground only.
            result = m.ImageTk.PhotoImage(m.Image.new('RGBA', (width, height)), master=self.c)
            self._surface_cache[key] = result
            return result
        picture = self._backdrop.copy()
        # One continuous, antialiased outer edge with a quieter lower highlight.
        border = m.ImageDraw.Draw(picture)
        border.rounded_rectangle((1, 1, w - 3, h - 3), radius=30 * scale,
                                 outline=(255, 255, 255, 230) if desktop else self.blend(self.LINE, self.TEXT, .12), width=2 if desktop else 1)
        if desktop:
            border.rounded_rectangle((4, 4, w - 6, h - 6), radius=29 * scale,
                                     outline=rgb(self.TEXT) + (40,), width=1)
        mask = m.Image.new("L", (w, h), 0)
        m.ImageDraw.Draw(mask).rounded_rectangle((1, 1, w - 3, h - 3), radius=30 * scale, fill=255)
        from PIL import ImageChops
        picture.putalpha(ImageChops.multiply(picture.getchannel('A'), mask))
        result = m.ImageTk.PhotoImage(picture.resize((width, height), m.Image.LANCZOS), master=self.c)
        self._surface_cache[key] = result
        return result

    def status(self):
        a = self.a
        sync = str((a.state.usage_sync or {}).get("state") or "") if a.state else ""
        if a.error or (a.state and a.state.error):
            return "刷新失败", self.ERROR
        if a._refresh_pending:
            return "刷新已排队", self.SECONDARY
        if a._loading:
            return "正在刷新", self.SECONDARY
        if getattr(a, "_live_usage_verification_pending", False):
            return "正在核对用量", self.WARN
        if sync in {"timeout", "error", "unavailable", "stale", "partial"}:
            return self.m.usage_sync_label(a.state.usage_sync) or "等待同步", self.WARN
        if not a.state:
            return "等待数据", self.MUTED
        active = sum(int(row.get("current") or 0) for row in a.state.active_accounts or [])
        return ("正在记录" if active else "等待请求"), (self.LIVE if active else self.MUTED)

    def shell(self):
        a, c, m = self.a, self.c, self.m
        W, H = a.WIDTH, a.HEIGHT
        self.small = H < 720
        self.paint.begin_frame()
        self.left, self.right, self.top, self.bottom = 24, W - 24, 82, H - 90
        self.models_rect = self.ring_geometry = None
        self._glass_refs.clear()
        if self.desktop and self.desktop.refraction:
            self.desktop.lenses.clear()
            self.desktop.glass_tint = tuple(value / 255 for value in rgb(self.BG))
            self.desktop.glass_dark = self.palette.dark
        surface = self._surface(W, H)
        if surface is not None:
            c.create_image(0, 0, anchor="nw", image=surface)
        else:
            self.box(1, 1, W - 2, H - 2, self.BG, 30, self.LINE)
        self.box(24, 26, 39, 41, "", 7.5, self.VIOLET)
        self.dot(37, 27, 2, self.CYAN)
        self.text(49, 21, "Token Pulse", "title")
        controls = [("appearance", "palette", "外观与配色"), ("toggle_layout", "width", "收窄窗口" if W >= 420 else "放宽窗口"),
                    ("btn_refresh", "refresh", "刷新数据"), ("btn_pin", "pin", "取消置顶" if a._pinned else "保持置顶"),
                    ("btn_close", "close", "关闭")]
        for index, (name, icon, tip) in enumerate(controls):
            x = W - 169 + index * 30
            self.button(name, x, 21, x + 27, 48, icon=icon, tip=tip,
                        selected=name == "appearance" and self.appearance_open or name == "btn_pin" and a._pinned, quiet=True)
        status, color = self.status()
        self.dot(27, 62, 2, color)
        self.text(36, 54, status, "caption", color, maxw=W - 170)
        if a.error or (a.state and a.state.error):
            a._add_tooltip(22, 51, W - 120, 72, str(a.error or a.state.error))
        stamp = m.datetime.fromtimestamp(a.state.updated_at, m.CN_TZ).strftime("%H:%M:%S") if a.state and a.state.updated_at else "尚未更新"
        self.text(self.right, 54, stamp, "micro", self.MUTED, "ne")
        source = str(a.state.source_label if a.state else "本地会话")
        a._add_tooltip(24, 21, W - 150, 47, "Token Pulse · Orbit\n数据来源：" + source)
        self.box(W - 29, H - 16, W - 20, H - 14, self.MUTED, 1)
        return self.top

    def navigation(self):
        a = self.a
        y = a.HEIGHT - 74
        left, right = 24, a.WIDTH - 24
        pressure = max(0., (self._press_until - time.monotonic()) / .14) if (self._pressed or "").startswith("main_") else 0.
        self._glass("dock", left, y, right - left, 50, 25, pressure=pressure)
        selected = "stats" if a._main_tab == "stats" else ("library" if self.library_open else "accounts")
        step = (right - left - 10) / 3
        target = left + 5 + ("accounts", "stats", "library").index(selected) * step
        if self._nav_position is None or self._nav_width != a.WIDTH or self.reduced_motion:
            self._nav_position = self._nav_target = target
        elif self._nav_target != target:
            self._nav_from, self._nav_target = self._nav_position, target
            self._nav_started = time.monotonic()
            self._request_animation()
        self._nav_width = a.WIDTH
        xx = self._nav_position
        self.box(xx - self._dock_stretch / 2, y + 5, xx + step + self._dock_stretch / 2, y + 45,
                 self.blend(self.PANEL, self.CYAN, .12), 20, tags=("nav_selection",))
        for index, (key, label, icon) in enumerate((("accounts", "实时", "live"), ("stats", "统计", "stats"), ("library", "账号", "accounts"))):
            x = left + 5 + index * step
            name = "main_" + key
            a._btn_rects[name] = (x, y + 4, x + step, y + 46)
            if self.focused == name:
                self.box(x + 1, y + 5, x + step - 1, y + 45, "", 20, self.CYAN)
            elif a._hover_btn == name and key != selected:
                self.box(x, y + 5, x + step, y + 45, self.blend(self.PANEL, self.CYAN, .07), 20)
            color = self.TEXT if key == selected else self.MUTED
            self.icon(icon, x + step / 2 - 22, y + 25, color, tags=("nav_foreground",))
            self.text(x + step / 2 + 11, y + 25, label, "nav", color, "center", tags=("nav_foreground",))

    def ranges(self, x1, y, x2, kind):
        if kind == "accounts":
            items = [("今日", "today"), ("5 小时", "5h"), ("7 天", "7d"), ("30 天", "30d")]
            rows = self.a._filter_account_display_rows(list(self.a.state.top_accounts or [])) if self.a.state else []
            if any(self.m.account_has_cycle_quota_window(row) for row in rows):
                items.append(("周期", "cycle"))
            prefix, selected = "rank_", self.a._account_range
        else:
            items = [("今日", "24h"), ("7 天", "7d"), ("30 天", "30d"), ("全部", "all")]
            prefix, selected = "usage_range_", self.a._usage_range
        self._glass("range", x1, y, x2 - x1, 34, 17)
        step = (x2 - x1 - 6) / len(items)
        for index, (label, value) in enumerate(items):
            x = x1 + 3 + index * step
            self.button(prefix + value, x, y + 3, x + step - 2, y + 31, label, selected == value, quiet=True)

    def hourly(self):
        # The existing summary merges authoritative hourly data with recent events.
        bins = [dict(hour=h, tokens=0, requests=0, failure=False) for h in range(24)]
        for row in self.a._usage_range_summary("24h").get("series", []):
            try:
                hour = int(row.get("hour"))
                if not 0 <= hour < 24:
                    continue
                bins[hour]["tokens"] += max(0, int(row.get("tokens") or 0))
                bins[hour]["requests"] += max(0, int(row.get("requests") or 0))
                bins[hour]["failure"] |= bool(row.get("failure"))
            except (TypeError, ValueError):
                continue
        return bins

    def ring(self, cx, cy, outer, bins):
        c, m = self.c, self.m
        inner = outer * (.81 if self.small else .73)
        if self.focused == "open_timeline":
            self.box(cx - inner + 6, cy - inner + 6, cx + inner - 6, cy + inner - 6,
                     "", inner - 6, self.CYAN)
        maximum = max((r["tokens"] for r in bins), default=0)
        segments = []
        for index in range(72):
            hour = index // 3
            angle = index / 72 * math.tau - math.pi / 2
            value = bins[hour]["tokens"]
            length = 5 + (outer - inner - 5) * value / maximum if maximum and value else 5
            tint = self.blend(self.CYAN, self.VIOLET, min(1, index / 47))
            if index > 47:
                tint = self.blend(self.VIOLET, self.PINK, (index - 47) / 24)
            color = self.ERROR if bins[hour]["failure"] else (tint if value else self.LINE)
            segments.append((inner * math.cos(angle), inner * math.sin(angle),
                             (inner + length) * math.cos(angle), (inner + length) * math.sin(angle), color))
        if m.Image is not None:
            size = math.ceil(outer * 2 + 10)
            key = (size, self.theme_name, tuple((r["tokens"], r["failure"]) for r in bins))
            if key not in self._ring_cache:
                self._ring_cache.clear()
                scale, mid = 3, size / 2
                picture = m.Image.new("RGBA", (size * scale, size * scale))
                draw = m.ImageDraw.Draw(picture)
                for x1, y1, x2, y2, color in segments:
                    coords = tuple((v + mid) * scale for v in (x1, y1, x2, y2))
                    draw.line(coords, fill=color, width=7)
                    for xx, yy in ((coords[0], coords[1]), (coords[2], coords[3])):
                        draw.ellipse((xx - 3, yy - 3, xx + 3, yy + 3), fill=color)
                self._ring_image = picture.resize((size, size), m.Image.LANCZOS)
                self._ring_cache[key] = m.ImageTk.PhotoImage(self._ring_image, master=self.c)
            source_key = (key, self.a.WIDTH, self.a.HEIGHT)
            if getattr(self, "_orbit_key", None) != source_key:
                self._orbit_source = self._backdrop.copy()
                self._orbit_source.alpha_composite(self._ring_image.resize((size * 2, size * 2), m.Image.LANCZOS),
                                                  (round((cx - size / 2) * 2), round((cy - size / 2) * 2)))
                self._orbit_key = source_key
            c.create_image(cx - size / 2, cy - size / 2, image=self._ring_cache[key], anchor="nw")
        else:
            for x1, y1, x2, y2, color in segments:
                c.create_line(cx + x1, cy + y1, cx + x2, cy + y2, fill=color, width=2, capstyle="round")
        for label, dx, dy in (("00", 0, -1), ("06", 1, 0), ("12", 0, 1), ("18", -1, 0)):
            label_radius = outer + (7 if label == "12" else 14)
            self.text(cx + label_radius * dx, cy + label_radius * dy, label,
                      "micro", self.MUTED, "center")
        now = m.datetime.now(m.CN_TZ)
        angle = (now.hour + now.minute / 60) / 24 * math.tau - math.pi / 2
        x, y = cx + (outer + 5) * math.cos(angle), cy + (outer + 5) * math.sin(angle)
        self.dot(x, y, 2.5, self.CYAN)
        self.ring_geometry = (cx, cy, inner - 5, outer + 8, bins)
        self.a._btn_rects["open_timeline"] = (cx - inner + 9, cy - inner + 9, cx + inner - 9, cy + inner - 9)
        self.a._add_tooltip(cx - inner + 9, cy - inner + 9, cx + inner - 9, cy + inner - 9,
                            "最近 10 秒新增 Token\n点击查看今日的详细用量趋势")

    def ring_tooltip(self, x, y):
        if self.ring_geometry is None:
            return ""
        cx, cy, inner, outer, bins = self.ring_geometry
        radius = math.hypot(x - cx, y - cy)
        if not inner <= radius <= outer:
            return ""
        fraction = ((math.atan2(y - cy, x - cx) + math.pi / 2) % math.tau) / math.tau
        hour = min(23, int(fraction * 24))
        row = bins[hour]
        message = f"{hour:02d}:00–{hour + 1:02d}:00 · 今日\n{self.m.exact_token_count(row['tokens'])} Token\n{row['requests']:,} 次请求"
        return message + ("\n该时段有错误记录" if row["failure"] else "")

    def accounts(self):
        if self.appearance_open:
            self.appearance()
            return
        if self.library_open:
            self.account_library()
            return
        a, m, c = self.a, self.m, self.c
        l, r, t = self.left, self.right, self.top
        hero_h = 218 if self.small else 274
        cx, cy = (l + r) / 2, t + hero_h / 2 - 8
        outer = min(128, (r - l - 58) / 2, (hero_h - 38) / 2)
        bins = self.hourly()
        self.ring(cx, cy, outer, bins)
        level, tokens = a._token_flow_snapshot()
        self.text(cx, cy - (40 if self.small else 52), "10 秒内 Token" if self.small else "最近 10 秒 Token",
                  "caption", self.SECONDARY, "center")
        self._number_y = cy - (1 if self.small else 7)
        self._number_maxw = outer * 1.38
        self._number_role = "number" if self.small else "hero"
        self.text(cx, self._number_y, m.compact_number(tokens) if a.state else "—",
                  "number" if self.small else "hero", anchor="center",
                  maxw=outer * 1.38, tags=("orbit_recent_tokens",))
        active = list(a.state.active_accounts or []) if a.state else []
        concurrency = sum(int(row.get("current") or 0) for row in active)
        pill_y = cy + (44 if self.small else 48)
        pill_half = 53 if self.small else 65
        label = f"{m.compact_number(concurrency)} 个活动请求" if concurrency else "等待新请求"
        self.box(cx - pill_half, pill_y - 13, cx + pill_half, pill_y + 13,
                 self.blend(self.BG, self.CYAN, .08), 13)
        self.dot(cx - pill_half + 12, pill_y, 2, self.LIVE if concurrency else self.MUTED)
        self.text(cx + 5, pill_y, label, "caption", self.SECONDARY, "center",
                  maxw=2 * pill_half - 24)
        latest_status, model, ago, _color = a._latest_status() if a.state and a.state.latest_request else ("", "", "", "")
        info = "\n".join(f"{m.ranking_account_display_name(str(row.get('name') or '-'))} · {row.get('current', 0)} 并发" for row in active)
        if model:
            info += f"\n最近请求 · {model}\n{latest_status} · {ago}"
        a._add_tooltip(cx - 65, pill_y - 15, cx + 65, pill_y + 15, info.strip() or "尚无活动请求")
        ring_note = "今日 · 每小时用量" if sum(row["tokens"] for row in bins) else "今日暂无小时明细"
        self.text(cx, t + hero_h - 2, ring_note, "caption", self.MUTED, "center")
        y = t + hero_h + 16
        self.daily_metrics(l, y, r)
        header_y = y + 73
        self.text(l, header_y, "账号", "heading")
        self.button("open_library", r - 75, header_y - 3, r, header_y + 23, "查看全部", quiet=True)
        rows = a._filter_account_display_rows(list(a.state.top_accounts or [])) if a.state else []
        top = header_y + 32
        a._active_scroll_rect = (l, top, r, self.bottom)
        visible = self.visible_rows("active", rows, top, self.bottom, 62, r + 11)
        if visible:
            self.box(l - 7, top - 7, r + 7, top + len(visible) * 62 + 5, self.PANEL, 18)
        if not rows:
            self.text(l, top + 15, "新的用量记录会显示在这里", "caption", self.MUTED, maxw=r - l)
        for index, row in enumerate(visible):
            self.account_row(row, l, top + index * 62, r, compact=True)
        self.update_live()

    def daily_metrics(self, l, y, r):
        a, m = self.a, self.m
        unpriced = int((a.state.client_usage or {}).get('unpriced_tokens') or 0) if a.state else 0
        items = [("今日用量", m.compact_number(a.state.today_tokens if a.state else 0)),
                 ("成本 · 含未定价" if unpriced else "预估成本",
                  self.cost_text({'cost': a.state.today_account_cost if a.state else 0, 'unpriced_tokens': unpriced})),
                 ("请求次数", m.compact_number(a.state.today_requests if a.state else 0))]
        if a.state is None:
            items = [(label, "—") for label, _value in items]
        step = (r - l) / 3
        for index, (label, value) in enumerate(items):
            x = l + index * step + (12 if index else 0)
            if index:
                self.c.create_line(x - 12, y + 7, x - 12, y + 43, fill=self.LINE)
            self.text(x, y, label, "caption", self.MUTED)
            self.text(x, y + 20, value, "value", self.TEXT, maxw=step - (15 if index else 10))
            self.a._add_tooltip(x, y, l + (index + 1) * step, y + 51, label + "\n" + value)

    def visible_rows(self, tab, rows, top, bottom, row_h, right):
        a = self.a
        capacity = max(0, int((bottom - top) // row_h))
        limit = max(0, len(rows) - capacity) * row_h if capacity else 0
        a._scroll_limits[tab] = limit
        a._ui_scroll_steps[tab] = row_h
        offset = max(0, min(int(a._scroll_offsets.get(tab, 0)), limit))
        a._scroll_offsets[tab] = offset
        first = offset // row_h
        visible = rows[first:first + capacity]
        a._draw_list_scrollbar(tab, int(right - 3), int(top), int(bottom - 4), len(visible), len(rows), limit)
        return visible

    def quota(self, row, compact=False):
        source = row.get("window_5h") if compact else row
        if not isinstance(source, dict):
            source = {}
        try:
            used = float(source["utilization"])
            if not math.isfinite(used):
                used = None
        except (ValueError, TypeError, KeyError):
            used = None
        available = bool(source.get("quota_available", used is not None)) and used is not None
        stale = bool(source.get("quota_stale"))
        ratio = max(0, min(1, 1 - used / 100)) if available else None
        color = self.WARN if stale else self.ERROR if available and used >= 90 else self.WARN if available and used >= 70 else self.VIOLET
        if source.get("quota_unlimited"):
            return "无 5h 限制", "", None, self.MUTED
        if available:
            label = f"剩余 {ratio:.0%}"
            if compact:
                label = "5h " + label
            if source.get("quota_idle"):
                label = "满额待使用"
        elif source.get("historical_fallback"):
            label = "近 7 日历史"
        elif (source.get("window_cycle") or {}).get("quota_available"):
            label = "周期额度"
        else:
            label = "额度待同步" if not compact else ""
        if stale:
            reset = "额度待刷新"
        elif source.get("quota_reset_unavailable"):
            reset = "重置时间待同步"
        elif available and not source.get("quota_idle"):
            reset = self.m.quota_reset_text(str(source.get("resets_at") or ""))
        else:
            reset = ""
        return label, reset, ratio, color

    def account_row(self, row, x, y, right, compact=False):
        a, m, c = self.a, self.m, self.c
        name = m.ranking_account_display_name(str(row.get("name") or "—"))
        plan = m.account_type_label(row, str(row.get("name") or ""))
        health = str(row.get("health_badge") or "")
        speed = str(row.get("speed_badge") or "")
        color = (self.CYAN, self.VIOLET, self.PINK)[sum(map(ord, name)) % 3]
        self.box(x + 1, y + 8, x + 34, y + 41, self.blend(self.PANEL, color, .09), 16.5)
        self.text(x + 17.5, y + 24.5, name[:1].upper(), "strong", color, "center")
        active = self.active_only and self.library_open and not compact
        amount = f"{row.get('current', 0)} 并发" if active else m.compact_number(row.get("tokens", 0))
        amount_w = max(64, self.fonts["row_value"].measure(amount) + 10)
        self.text(x + 44, y + 3, name, "body", self.TEXT, maxw=right - x - 48 - amount_w)
        self.text(right, y + 1, amount, "row_value", self.TEXT, "ne")
        quota_mode = not compact and a._account_range in {"5h", "7d", "cycle"} and not active
        label, reset, ratio, quota_color = self.quota(row, compact=compact or not quota_mode)
        detail = " · ".join(v for v in (plan, label if compact else speed, health) if v) or "账号"
        cost = f"上限 {row.get('max', 1)}" if active else self.cost_text(row)
        cost_w = self.fonts["caption"].measure(cost) + 12
        self.text(x + 44, y + 26, detail, "caption", self.MUTED,
                  maxw=right - x - 47 - cost_w)
        self.text(right, y + 28, cost, "caption", self.MUTED, "ne")
        line_y = y + 52
        if ratio is not None and not active:
            self.box(x + 44, line_y - 1, right, line_y + 2, self.LINE, 1.5)
            if ratio > 0:
                self.box(x + 44, line_y - 1, x + 44 + (right - x - 44) * ratio,
                         line_y + 2, quota_color, 1.5)
        elif not compact:
            c.create_line(x + 41, line_y, right, line_y, fill=self.LINE)
        if not compact and not active:
            max_status_width = self.fonts["caption"].measure(label)
            self.text(x + 41, y + 63, label, "caption", quota_color, maxw=right - x - 45)
            self.text(right, y + 63, reset, "caption", self.MUTED, "ne",
                      maxw=max(0, right - x - 55 - max_status_width))
        tip = f"{name}\n{' · '.join(v for v in (plan, speed, health) if v)}\n{m.exact_token_count(row.get('tokens', 0))} Token · {self.cost_text(row)}\n{label} {reset}"
        tip += self.unpriced_note(row)
        a._add_tooltip(x, y, right, y + (58 if compact else 82), tip)

    def account_library(self):
        a, m = self.a, self.m
        # Normalize a disappeared cycle window before drawing the selected tab.
        range_rows = a._account_rows_for_range()[0]
        l, r, t = self.left, self.right, self.top
        self.text(l, t + 4, "账号", "page_title")
        self.text(l, t + 58, "用量与额度", "caption", self.MUTED)
        req = a.state.latest_request if a.state else None
        if req:
            status, model, ago, color = a._latest_status()
            self.text(r, t + 12, status + " · " + ago, "caption", color, "ne", maxw=r - l - 110)
            self.text(r, t + 37, model, "data", self.SECONDARY, "ne", maxw=r - l - 110)
            a._add_tooltip(l + 110, t, r, t + 62, f"最近请求\n{model}\n{a.state.latest_account_name or '—'}\n{status} · {ago}")
        self.ranges(l, t + 86, r, "accounts")
        self.button("account_filter_all", l, t + 132, l + 60, t + 159, "全部", not self.active_only, quiet=True)
        self.button("account_filter_active", l + 65, t + 132, l + 125, t + 159, "活跃", self.active_only, quiet=True)
        rows = list(a.state.active_accounts or []) if self.active_only and a.state else range_rows
        if self.active_only and not a.state:
            rows = []
        self.text(r, t + 138, f"{len(rows)} 个账号", "caption", self.MUTED, "ne")
        row_h = 66 if self.active_only else 92
        top = t + 174
        visible = self.visible_rows("accounts", rows, top, self.bottom, row_h, r + 7)
        if not rows:
            self.text(l, top + 22, "暂无活跃账号" if self.active_only else "该范围暂无记录", "body", self.MUTED)
        for index, row in enumerate(visible):
            self.account_row(row, l, top + index * row_h, r)

    def stats(self):
        if self.appearance_open:
            self.appearance()
            return
        a = self.a
        l, r, t = self.left, self.right, self.top
        summary = a._usage_range_summary(a._usage_range)
        self.ranges(l, t, r, "stats")
        summary_top = t + 47
        self.stats_summary(l, summary_top, r, summary)
        chart_top = summary_top + (95 if self.small else 113)
        chart_h = 92 if self.small else 141
        self.trend(l, chart_top, r, chart_top + chart_h, summary)
        mix_top = chart_top + chart_h + 17
        mix_bottom = self.mix(l, mix_top, r, summary)
        list_top = mix_bottom + (8 if self.small else 16)
        self.button("analysis_models", l, list_top, l + 85, list_top + 27, "模型用量",
                    self.compact_analysis == "models", quiet=True)
        self.button("analysis_accounts", l + 91, list_top, l + 176, list_top + 27, "账号成本",
                    self.compact_analysis == "accounts", quiet=True)
        top = list_top + 38
        if self.compact_analysis == "models":
            models = a._top_models(a._usage_range)
            selected = self.visible_rows("stats", models, top, self.bottom, 42, r + 7)
            maximum = max((float(v) for _model, v in models), default=1) or 1
            for index, (model, value) in enumerate(selected):
                y = top + index * 42
                label = self.m.compact_number(value)
                reserve = self.fonts["data"].measure(label) + 15
                self.text(l, y, model, "data", self.SECONDARY, maxw=r - l - reserve)
                self.text(r, y, label, "data", self.TEXT, "ne")
                self.box(l, y + 26, r, y + 29, self.LINE, 1.5)
                if value > 0:
                    self.box(l, y + 26, l + (r - l) * value / maximum, y + 29, self.VIOLET, 1.5)
                a._add_tooltip(l, y, r, y + 32, f"{model}\n{self.m.exact_token_count(value)} Token")
            if not models:
                self.text(l, top + 8, "暂无模型记录", "caption", self.MUTED)
        else:
            rows = sorted(a._filter_account_display_rows(a._usage_range_providers(a._usage_range)),
                          key=lambda row: (-float(row.get("cost") or 0), -int(row.get("tokens") or 0)))
            selected = self.visible_rows("stats", rows, top, self.bottom, 56, r + 7)
            for index, row in enumerate(selected):
                y = top + index * 56
                name = self.m.ranking_account_display_name(str(row.get("name") or "—"))
                value = self.cost_text(row)
                self.text(l, y, name, "body", maxw=r - l - self.fonts["data"].measure(value) - 15)
                self.text(r, y + 1, value, "data", self.CYAN, "ne")
                detail = self.m.compact_number(row.get("tokens", 0)) + " Token · " + self.m.compact_number(row.get("requests", 0)) + " 次请求"
                self.text(l, y + 24, detail, "caption", self.MUTED)
                self.c.create_line(l, y + 46, r, y + 46, fill=self.LINE)
                a._add_tooltip(l, y, r, y + 48, f"{name}\n{self.m.exact_token_count(row.get('tokens', 0))} Token\n{value}" + self.unpriced_note(row))
            if not rows:
                self.text(l, top + 8, "暂无账号记录", "caption", self.MUTED)

    def cost_text(self, row):
        cost = float(row.get('cost') or 0)
        if int(row.get('unpriced_tokens') or 0):
            return self.m.money(cost) + ' +' if cost > 0 else '未定价'
        return self.m.money(cost)

    def unpriced_note(self, row):
        count = int(row.get('unpriced_tokens') or 0)
        return f'\n另有 {self.m.exact_token_count(count)} Token 未定价' if count else ''

    def stats_summary(self, x, y, right, summary):
        a, m = self.a, self.m
        self.text(x, y, "累计用量", "caption", self.MUTED)
        self.text(x - 2, y + 18, m.compact_number(summary["tokens"]), "number",
                  maxw=(right - x) * .60)
        self.text(right, y, "成本 · 含未定价" if summary.get('unpriced_tokens') else "预估成本", "caption", self.MUTED, "ne")
        self.text(right, y + 26, self.cost_text(summary), "value", self.VIOLET, "ne", maxw=(right - x) * .37)
        self.text(right, y + 64, m.compact_number(summary["requests"]) + " 次请求", "caption", self.MUTED, "ne")
        badge, color, visible = a._token_delta_badge_visual()
        self.text(x, y + 76, badge, "data", color, state="normal" if visible else "hidden", tags=("token_delta_badge",))
        a._add_tooltip(x, y, right, y + 76, f"{m.exact_token_count(summary['tokens'])} Token\n{int(summary['requests']):,} 次请求\n{self.cost_text(summary)}" + self.unpriced_note(summary))

    def time_label(self, row, fallback):
        if self.a._usage_range == "24h":
            return f"{int(row.get('hour') if row.get('hour') is not None else fallback):02d}:00"
        return str(row.get("date") or "—")[5:]

    def trend(self, x, y, right, bottom, summary):
        a, m, c = self.a, self.m, self.c
        self.text(x, y, "用量趋势", "heading")
        label = {"24h": "按小时", "7d": "近 7 天", "30d": "近 30 天", "all": "全部历史"}.get(a._usage_range, "")
        self.text(right, y + 4, label, "caption", self.MUTED, "ne")
        series = [row for row in summary.get("series", []) if isinstance(row, dict)]
        top, base, left = y + 35, bottom - 21, x + 37
        if not series:
            self.text(x, top + 12, "新的用量记录会显示在这里", "caption", self.MUTED, maxw=right - x)
            return
        capacity = max(1, min(len(series), int((right - left) // 8)))
        groups = [series[i * len(series) // capacity:(i + 1) * len(series) // capacity] for i in range(capacity)]
        values = [sum(max(0, float(row.get("tokens") or 0)) for row in group) for group in groups]
        maximum = max(values) or 1
        for ratio in (0, .5, 1):
            yy = base - (base - top) * ratio
            c.create_line(left, yy, right, yy, fill=self.GRID)
            self.text(left - 8, yy, m.compact_number(maximum * ratio), "micro", self.MUTED, "e")
        step = (right - left) / capacity
        for index, (group, value) in enumerate(zip(groups, values)):
            xx = left + (index + .5) * step
            height = (base - top) * value / maximum
            failed = any(row.get("failure") for row in group)
            color = self.ERROR if failed else self.blend(self.CYAN, self.VIOLET, index / max(1, capacity - 1))
            if value > 0:
                bar_w = max(2, step - 4)
                self.box(xx - bar_w / 2, base - max(2, height), xx + bar_w / 2, base,
                         color, min(3, bar_w / 2))
            elif failed:
                self.dot(xx, base, 2, self.ERROR)
            title = self.time_label(group[0], index)
            if len(group) > 1:
                title += " – " + self.time_label(group[-1], index)
            tip = f"{title}\n{m.exact_token_count(value)} Token\n{sum(int(row.get('requests') or 0) for row in group):,} 次请求"
            if failed:
                tip += "\n含错误记录"
            a._add_tooltip(xx - step / 2, top - 4, xx + step / 2, base + 4, tip)
        for index in sorted({0, capacity // 2, capacity - 1}):
            xx = left + index * step
            self.text(xx, base + 8, self.time_label(groups[index][0], index), "micro", self.MUTED,
                      "ne" if index == capacity - 1 else "nw")

    def mix(self, x, y, right, summary):
        mix = self.a._summary_token_mix(summary)
        items = [("输入", mix["input"], self.CYAN), ("缓存读取", mix["cached"], self.VIOLET),
                 ("缓存写入", mix["cache_create"], self.WARN), ("输出", mix["output"], self.PINK)]
        if mix.get("unknown", 0):
            items.append(("未分类", mix["unknown"], self.MUTED))
        self.text(x, y, "Token 构成", "heading")
        base = mix["input"] + mix["cached"] + mix["cache_create"]
        rate = f"{mix['cached'] / base:.0%}" if base else "—"
        self.text(right, y + 4, "缓存命中 " + rate, "caption", self.MUTED, "ne")
        total = sum(value for _name, value, _color in items)
        self.box(x, y + 29, right, y + 35, self.LINE, 3)
        cursor = x
        for name, value, color in items:
            width = (right - x) * value / total if total else 0
            if width:
                self.c.create_rectangle(cursor, y + 29, cursor + width, y + 35, fill=color, outline="")
                self.a._add_tooltip(cursor, y + 27, cursor + width, y + 38, f"{name}\n{self.m.exact_token_count(value)} Token")
            cursor += width
        col = (right - x - 20) / 2
        for index, (name, value, color) in enumerate(items):
            xx, yy = x + index % 2 * (col + 20), y + 46 + index // 2 * 21
            self.c.create_oval(xx, yy + 4, xx + 4, yy + 8, fill=color, outline="")
            self.text(xx + 11, yy, name, "caption", self.SECONDARY)
            self.text(xx + col, yy, self.m.compact_number(value), "data", self.TEXT, "ne")
        return y + 46 + math.ceil(len(items) / 2) * 21

    def handle_button(self, name):
        a = self.a
        self._control_press = name in a._btn_rects
        if name in a._btn_rects and not self.reduced_motion:
            self._pressed, self._press_until = name, time.monotonic() + .14
            self._request_animation()
        if name in {"appearance", "appearance_done"}:
            self.appearance_open = name != "appearance_done" and not self.appearance_open
            self._demo_position = None
            self._lens_drag = False
            self._slider_drag = None
            self._demo_pressure = self._demo_released_at = 0.
            self._ring_lens = None
            a._tooltip_text = ""
            a._draw()
            return True
        if name and name.startswith("theme_") and name[6:] in PALETTES:
            self._set_palette(name[6:])
            self._save_appearance()
            a._draw()
            return True
        if name == "appearance_motion":
            self.reduced_motion = not self.reduced_motion or self._prefers_reduced_motion()
            if self.reduced_motion:
                if self._animation_id is not None:
                    a.root.after_cancel(self._animation_id)
                    self._animation_id = None
                self._light = self._light_target = self._dock_stretch = 0.
                self._demo_pressure = self._demo_released_at = 0.
                self._pressed = None
                self._press_until = self._number_started = 0.
                self._nav_position = self._nav_target
                if self.desktop:
                    self.desktop.pointer = (0., 0.)
            elif self.glass and self.appearance_open and self._demo_position:
                self._demo_pressure = .85
                self._demo_released_at = time.monotonic()
                self._request_animation()
            self._save_appearance()
            a._draw()
            return True
        if name and name.startswith('material_') and name[9:] in {'liquid', 'alpha', 'opaque'}:
            if self.material_name == name[9:]:
                return True
            self.material_name = name[9:]
            self._lens_drag = False
            self._slider_drag = None
            self._demo_pressure = self._demo_released_at = 0.
            if self.desktop and self.desktop.active:
                self.desktop.set_material(self.material_name)
            elif self.material_name == 'opaque':
                a.WINDOW_ALPHA = 1.0
                a.root.attributes('-alpha', 1.0)
            self._surface_cache.clear()
            self._glass_cache.clear()
            self._text_colors.clear()
            self._save_appearance()
            a._draw()
            return True
        if name in {"main_accounts", "main_stats", "main_library", "open_library", "open_timeline"}:
            self.appearance_open = False
            self._lens_drag = False
            self._slider_drag = None
            self._demo_pressure = self._demo_released_at = 0.
            self._ring_lens = None
            self.library_open = name in {"main_library", "open_library"}
            if name == "open_timeline":
                a._usage_range = "24h"
            a._switch_main_tab("stats" if name in {"main_stats", "open_timeline"} else "accounts")
            return True
        if name == "toggle_layout":
            width, height = (390 if a.WIDTH >= 420 else 420), a.HEIGHT
            a._apply_window_size(width, height)
            x, y = a.root.winfo_x(), a.root.winfo_y()
            area = a._work_area_for_window(x, y, width)
            x = max(area.left, min(x, area.right - width))
            y = max(area.top, min(y, area.bottom - height))
            a.root.geometry(self.m.format_tk_geometry(width, height, x, y))
        elif name in {"account_filter_all", "account_filter_active"}:
            self.active_only = name == "account_filter_active"
            a._scroll_offsets["accounts"] = 0
        elif name in {"analysis_accounts", "analysis_models"}:
            self.compact_analysis = name.removeprefix("analysis_")
            a._scroll_offsets["stats"] = 0
        else:
            return False
        a._resizing = False
        a._draw()
        return True

    def move_focus(self, direction):
        names = list(self.a._btn_rects)
        if names:
            index = names.index(self.focused) if self.focused in names else (-1 if direction > 0 else 0)
            self.focused = names[(index + direction) % len(names)]
            self.a._draw()
        return "break"

    def activate_focus(self):
        if (self.focused or '').startswith('slider_'):
            return 'break'  # Arrow keys adjust sliders without starting a drag.
        box = self.a._btn_rects.get(self.focused)
        if box:
            x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            self.a._on_press(SimpleNamespace(x=x, y=y, x_root=self.a.root.winfo_rootx() + x,
                                             y_root=self.a.root.winfo_rooty() + y))
        return "break"

    def update_live(self):
        if self.a._main_tab != "accounts" or self.library_open or self.appearance_open or not self.c.find_withtag("orbit_recent_tokens"):
            return False
        _level, tokens = self.a._token_flow_snapshot()
        value = self.m.compact_number(tokens) if self.a.state else "—"
        if value != self._recent_text and self._recent_text is not None and not self.reduced_motion:
            self._number_started = time.monotonic()
            self._request_animation()
        self._recent_text = value
        self.c.itemconfigure("orbit_recent_tokens", text=value,
                             font=self.fitted_font(self._number_role, value, self._number_maxw))
        return True

    @staticmethod
    def _prefers_reduced_motion():
        if os.environ.get("TOKEN_MONITOR_REDUCE_MOTION", "").lower() in {"1", "true", "yes"}:
            return True
        if os.name == "nt":
            import ctypes
            enabled = ctypes.c_int(1)
            # SPI_GETCLIENTAREAANIMATION reads the Windows accessibility setting.
            if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
                return not bool(enabled.value)
        return False

    def _request_animation(self):
        if not self.reduced_motion and self._animation_id is None and not self.a.closed:
            self._animation_id = self.a.root.after(16, self._animate)

    def _animate(self):
        self._animation_id = None
        if self.a.closed:
            return
        now, moving = time.monotonic(), False
        if self._nav_position is not None and self._nav_position != self._nav_target:
            progress = 1. if self.reduced_motion else min(1., (now - self._nav_started) / .32)
            eased = 1 - math.exp(-7 * progress) * math.cos(10 * progress) if progress < 1 else 1
            position = self._nav_from + (self._nav_target - self._nav_from) * eased
            self._dock_stretch = min(25., abs(self._nav_target - self._nav_from) * .17) * math.sin(math.pi * progress)
            self.c.delete("nav_selection")
            y, step = self.a.HEIGHT - 74, (self.a.WIDTH - 58) / 3
            self.box(position - self._dock_stretch / 2, y + 5, position + step + self._dock_stretch / 2, y + 45,
                     self.blend(self.PANEL, self.CYAN, .12), 20, tags=("nav_selection",))
            self.c.tag_raise("nav_foreground")
            self._nav_position = position
            if progress == 1:
                self._nav_position = self._nav_target
            else:
                moving = True
        glass_dirty = bool(self._pressed and self._pressed.startswith("main_") and now < self._press_until)
        if self._demo_released_at:
            progress = 1. if self.reduced_motion else min(1., (now - self._demo_released_at) / .5)
            self._demo_pressure = .85 * math.exp(-6.5 * progress) * math.cos(10 * progress) if progress < 1 else 0.
            if progress == 1:
                self._demo_released_at = 0.
            else:
                moving = True
            glass_dirty = True
        if not self.reduced_motion and abs(self._light_target - self._light) > .015:
            self._light += (self._light_target - self._light) * .32
            moving = True
            glass_dirty = True
        elif self.reduced_motion:
            self._light = self._light_target = 0.
        if glass_dirty and (not moving or now - self._last_glass_frame >= .025):
            self._last_glass_frame = now
            pressure = max(0., (self._press_until - now) / .14) if (self._pressed or "").startswith("main_") else 0.
            self._glass("dock", 24, self.a.HEIGHT - 74, self.a.WIDTH - 48, 50, 25, pressure=pressure)
            self._draw_following_lens()
        if self._number_started and self.c.find_withtag("orbit_recent_tokens"):
            progress = 1. if self.reduced_motion else min(1., (now - self._number_started) / .18)
            eased = 1 - (1 - progress) ** 3
            coords = self.c.coords("orbit_recent_tokens")
            self.c.coords("orbit_recent_tokens", coords[0], self._number_y + 2 * (1 - eased))
            self.c.itemconfigure("orbit_recent_tokens", fill=self.legible(self.blend(self.SECONDARY, self.TEXT, eased)))
            if progress == 1:
                self._number_started = 0.
            else:
                moving = True
        if self._pressed:
            if now >= self._press_until or self.reduced_motion:
                self._pressed = None
                self.a._draw()
            else:
                moving = True
        if moving:
            self._request_animation()

    def _on_destroy(self, event):
        if event.widget == self.a.root and self._animation_id is not None:
            self.a.root.after_cancel(self._animation_id)
            self._animation_id = None
