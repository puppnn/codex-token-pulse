"""Windows desktop compositing for the existing Tk Canvas.

An invisible Canvas copy keeps usage data and text separate from the optional
desktop refraction worker. App-only snapshots read that Canvas copy.
"""
from __future__ import annotations

import os
import logging
import sys
import tkinter as tk


class CompositedCanvas(tk.Canvas):
    """Invalidate the native surface when drawing changes, including animations."""

    _paint_callback = None
    _mirror = None
    _mirror_ids = None

    def _targets(self, args):
        return tuple(self._mirror_ids.get(int(arg), arg) if str(arg).isdigit() else arg for arg in args)

    def _changed(self):
        if self._paint_callback is not None:
            self._paint_callback()

    def _create(self, *args, **kwargs):
        result = super()._create(*args, **kwargs)
        if self._mirror is not None:
            self._mirror_ids[result] = self._mirror._create(*args, **kwargs)
        self._changed()
        return result

    def delete(self, *args):
        removed = {item for tag in args for item in self.find_withtag(tag)} if self._mirror is not None else ()
        super().delete(*args)
        if self._mirror is not None:
            self._mirror.delete(*self._targets(args))
            if 'all' in args:
                self._mirror_ids.clear()
            else:
                for item in removed:
                    self._mirror_ids.pop(item, None)
        self._changed()

    def itemconfigure(self, tagOrId, cnf=None, **kw):
        result = super().itemconfigure(tagOrId, cnf, **kw)
        if kw or isinstance(cnf, dict):
            if self._mirror is not None:
                self._mirror.itemconfigure(self._targets((tagOrId,))[0], cnf, **kw)
            self._changed()
        return result

    itemconfig = itemconfigure

    def coords(self, *args):
        result = super().coords(*args)
        if len(args) > 1:
            if self._mirror is not None:
                self._mirror.coords(self._targets(args[:1])[0], *args[1:])
            self._changed()
        return result

    def tag_raise(self, *args):
        super().tag_raise(*args)
        if self._mirror is not None:
            self._mirror.tag_raise(*self._targets(args))
        self._changed()

    def tag_lower(self, *args):
        super().tag_lower(*args)
        if self._mirror is not None:
            self._mirror.tag_lower(*self._targets(args))
        self._changed()


def recover_alpha(black, white):
    """Recover premultiplied RGBA from two Canvas mattes, retaining opaque text."""
    from PIL import Image, ImageChops

    delta = ImageChops.subtract(white.convert('RGB'), black.convert('RGB'))
    r, g, b = delta.split()
    alpha = ImageChops.invert(ImageChops.darker(ImageChops.darker(r, g), b))
    channels = [ImageChops.darker(channel, alpha) for channel in black.convert('RGB').split()]
    return Image.merge('RGBA', (*channels, alpha))


def composite_premultiplied(base, foreground):
    from PIL import Image, ImageChops
    inverse = ImageChops.invert(foreground.getchannel('A'))
    channels = [ImageChops.add(front, ImageChops.multiply(back, inverse))
                for back, front in zip(base.split(), foreground.split())]
    return Image.merge('RGBA', channels)


class DesktopCompositor:
    """Present per-pixel alpha to DWM; Tk retains all input and window handling."""

    def __init__(self, root, canvas, on_failure):
        import ctypes as c
        from ctypes import wintypes as w

        self.c, self.w = c, w
        self.root, self.canvas, self.on_failure = root, canvas, on_failure
        self.active, self._painting, self._pending = False, False, None
        self._dc = self._bitmap = self._old_bitmap = self._bits = None
        self._size = None
        self._region_size = None
        self.frame = None
        self.error = ''
        self.frames = 0
        self.refraction = False
        self.material = 'liquid'
        self.blur_radius = 4.0
        self.refraction_strength = 1.0
        self._optics = None
        self._optics_pending = None
        self._overlay = None
        self.display_frame = None
        self.lenses = {}
        self.glass_tint = (.93, .95, .97)
        self.glass_dark = False
        self.pointer = (0., 0.)
        self.u = c.WinDLL('user32', use_last_error=True)
        self.g = c.WinDLL('gdi32', use_last_error=True)
        self.dwm = c.WinDLL('dwmapi', use_last_error=True)

        class BitmapInfo(c.Structure):
            _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                        ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                        ('image_size', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                        ('colors', w.DWORD), ('important', w.DWORD)]

        class Blend(c.Structure):
            _fields_ = [('operation', w.BYTE), ('flags', w.BYTE), ('opacity', w.BYTE), ('format', w.BYTE)]

        class Accent(c.Structure):
            _fields_ = [('state', w.DWORD), ('flags', w.DWORD), ('color', w.DWORD), ('animation', w.DWORD)]

        class Composition(c.Structure):
            _fields_ = [('attribute', c.c_int), ('data', c.c_void_p), ('size', c.c_size_t)]

        self.BitmapInfo, self.Blend, self.Accent, self.Composition = BitmapInfo, Blend, Accent, Composition
        signatures = [
            (self.u.GetAncestor, [w.HWND, w.UINT], w.HWND),
            (self.u.GetWindowLongW, [w.HWND, c.c_int], w.LONG),
            (self.u.SetWindowLongW, [w.HWND, c.c_int, w.LONG], w.LONG),
            (self.u.GetLayeredWindowAttributes, [w.HWND, c.POINTER(w.DWORD), c.POINTER(w.BYTE), c.POINTER(w.DWORD)], w.BOOL),
            (self.u.GetWindowRect, [w.HWND, c.POINTER(w.RECT)], w.BOOL),
            (self.u.SetWindowRgn, [w.HWND, w.HRGN, w.BOOL], c.c_int),
            (self.u.SetThreadDpiAwarenessContext, [c.c_void_p], c.c_void_p),
            (self.u.PrintWindow, [w.HWND, w.HDC, w.UINT], w.BOOL),
            (self.u.UpdateLayeredWindow, [w.HWND, w.HDC, c.c_void_p, c.POINTER(w.SIZE), w.HDC,
                                         c.POINTER(w.POINT), w.DWORD, c.POINTER(Blend), w.DWORD], w.BOOL),
            (self.u.SetWindowCompositionAttribute, [w.HWND, c.POINTER(Composition)], w.BOOL),
            (self.g.CreateCompatibleDC, [w.HDC], w.HDC),
            (self.g.CreateDIBSection, [w.HDC, c.POINTER(BitmapInfo), w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD], w.HBITMAP),
            (self.g.SelectObject, [w.HDC, w.HANDLE], w.HANDLE),
            (self.g.DeleteObject, [w.HANDLE], w.BOOL),
            (self.g.DeleteDC, [w.HDC], w.BOOL),
            (self.g.CreateRoundRectRgn, [c.c_int] * 6, w.HRGN),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result
        enabled = w.BOOL()
        if self.dwm.DwmIsCompositionEnabled(c.byref(enabled)) != 0 or not enabled.value:
            raise OSError('Desktop composition is unavailable')
        self.hwnd = self.u.GetAncestor(root.winfo_id(), 2)
        # A non-visible drawing target keeps Tk's GDI renderer available while
        # the interactive window uses per-pixel compositing. Images and fonts
        # are shared in the same Tcl interpreter; input stays on the real Canvas.
        self._render_root = tk.Toplevel(root)
        self._render_root.title('Token Pulse render surface')
        self._render_root.overrideredirect(True)
        self._render_root.attributes('-alpha', 0.)
        self._render_root.geometry('1x1+-32000+-32000')
        self._render_canvas = tk.Canvas(self._render_root, highlightthickness=0, bd=0)
        self._render_canvas.pack(fill='both', expand=True)
        self._render_canvas.bind('<Configure>', lambda _event: self.request_frame(), add='+')
        self.canvas._mirror = self._render_canvas
        self.canvas._mirror_ids = {}
        # Tk normally uses a uniform alpha/color key. Reset the layered style
        # before switching to UpdateLayeredWindow's independent pixel alpha.
        self._style = self.u.GetWindowLongW(self.hwnd, -20)
        self.u.SetWindowLongW(self.hwnd, -20, self._style & ~0x80000)
        self.u.SetWindowLongW(self.hwnd, -20, self._style | 0x80000)
        # This compatibility accent is available on the supported Windows 10/11
        # builds, including 22000, which lacks DWMWA_SYSTEMBACKDROP_TYPE.
        accent = Accent(3, 0, 0, 0)
        data = Composition(19, c.addressof(accent), c.sizeof(accent))
        self.blur_enabled = bool(self.u.SetWindowCompositionAttribute(self.hwnd, c.byref(data)))
        self.active = True
        self.canvas._paint_callback = self.request_frame
        self.root.bind('<Destroy>', self._destroyed, add='+')
        self.root.bind('<Map>', lambda _event: self.request_frame(), add='+')
        self.canvas.bind('<Configure>', lambda _event: self.request_frame(), add='+')

    @classmethod
    def create(cls, root, canvas, on_failure, material='liquid'):
        if os.name != 'nt' or os.environ.get('TOKEN_MONITOR_DESKTOP_GLASS', '1').lower() in {'0', 'false', 'no'}:
            return None
        if sys.getwindowsversion().build < 17763:
            return None
        try:
            from PIL import Image  # noqa: F401
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                    if not winreg.QueryValueEx(key, 'EnableTransparency')[0]:
                        return None
            except OSError:
                pass
            compositor = cls(root, canvas, on_failure)
            compositor.set_material(material)
            return compositor
        except (OSError, AttributeError, ImportError) as exc:
            logging.getLogger('tokenpulse.monitor.glass').warning('Desktop composition unavailable: %s', exc)
            return None

    def set_material(self, material):
        self.material = material
        self.set_refraction(material == 'liquid')

    def set_refraction(self, enabled):
        self.display_frame = None
        if self._optics_pending is not None:
            self.root.after_cancel(self._optics_pending)
            self._optics_pending = None
        if self._optics is not None:
            self._optics.close()
            self._optics = None
        self.refraction = False
        if enabled and os.environ.get('TOKEN_MONITOR_REFRACTION', '1').lower() not in {'0', 'false', 'no'}:
            try:
                from monitor_refraction import DesktopRefraction
                self._optics = DesktopRefraction(self.hwnd)
                self.refraction = True
            except (ImportError, OSError, RuntimeError) as exc:
                logging.getLogger('tokenpulse.monitor.glass').warning('Curved material unavailable: %s', exc)
        accent = self.Accent(0 if self.refraction or self.material == 'opaque' else 3, 0, 0, 0)
        data = self.Composition(19, self.c.addressof(accent), self.c.sizeof(accent))
        self.blur_enabled = bool(self.u.SetWindowCompositionAttribute(self.hwnd, self.c.byref(data))) and accent.state == 3
        if self.refraction:
            self._optics_pending = self.root.after(16, self._poll_refraction)
        self.request_frame()

    def _submit_refraction(self):
        if self._optics is None or self._overlay is None:
            return
        previous = self.u.SetThreadDpiAwarenessContext(self.c.c_void_p(-4))
        try:
            rect = self.w.RECT()
            if not self.u.GetWindowRect(self.hwnd, self.c.byref(rect)):
                return
            bounds = (rect.left, rect.top, rect.right, rect.bottom)
        finally:
            if previous:
                self.u.SetThreadDpiAwarenessContext(previous)
        self._optics.submit(self._size, bounds, self._overlay, self.glass_tint, self.glass_dark,
                            tuple(self.lenses.values())[:4], self.pointer,
                            self.blur_radius, self.refraction_strength)

    def _poll_refraction(self):
        self._optics_pending = None
        if not self.active or self._optics is None:
            return
        try:
            if self._optics.error:
                raise OSError(self._optics.error)
            self._submit_refraction()
            frame = self._optics.result()
            geometry = (self.root.winfo_width(), self.root.winfo_height())
            canvas_size = (self.canvas.winfo_width(), self.canvas.winfo_height())
            if frame is not None and frame[0] == self._size == geometry == canvas_size:
                self.display_frame = frame
                self._blit(frame[1])
            self._optics_pending = self.root.after(16, self._poll_refraction)
        except (OSError, ValueError, tk.TclError) as exc:
            self.error = str(exc)
            self.close()
            self.on_failure(self.error)

    def _buffer(self, width, height):
        if self._size == (width, height):
            return
        self._overlay = None
        self.display_frame = None
        self._release_buffer()
        c, w = self.c, self.w
        info = self.BitmapInfo(c.sizeof(self.BitmapInfo), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        self._dc = self.g.CreateCompatibleDC(None)
        bits = c.c_void_p()
        self._bitmap = self.g.CreateDIBSection(self._dc, c.byref(info), 0, c.byref(bits), None, 0)
        if not self._dc or not self._bitmap:
            self._release_buffer()
            raise c.WinError(c.get_last_error())
        self._old_bitmap = self.g.SelectObject(self._dc, self._bitmap)
        self._bits, self._size = bits, (width, height)

    def _capture_canvas(self, color, erase_occluders=False):
        from PIL import Image
        canvas = self._render_canvas
        occluders = {item: (canvas.itemcget(item, 'fill'), canvas.itemcget(item, 'outline'))
                     for item in canvas.find_withtag('refraction_occluder')} if erase_occluders else {}
        try:
            # Erase earlier ink to the current matte, then draw tooltip text.
            # Its opaque panel stays in the base layer for contrast adjustment.
            for item in occluders:
                canvas.itemconfigure(item, fill=color, outline=color)
            canvas.configure(bg=color)
            canvas.update_idletasks()
            self.c.memset(self._bits, 0, self._size[0] * self._size[1] * 4)
            if not self.u.PrintWindow(self._render_root.winfo_id(), self._dc, 2):
                raise self.c.WinError(self.c.get_last_error())
        finally:
            for item, (fill, outline) in occluders.items():
                canvas.itemconfigure(item, fill=fill, outline=outline)
        raw = self.c.string_at(self._bits, self._size[0] * self._size[1] * 4)
        return Image.frombytes('RGB', self._size, raw, 'raw', 'BGRX')

    def _capture_layers(self):
        canvas = self._render_canvas
        states = {item: canvas.itemcget(item, 'state') for item in canvas.find_all()}
        foreground = {item for item in states if canvas.type(item) == 'text' or 'refraction_ink' in canvas.gettags(item)}
        occluders = set(canvas.find_withtag('refraction_occluder'))
        layers = []
        try:
            for show_foreground in (False, True):
                for item, state in states.items():
                    visible = (item in foreground) == show_foreground or (show_foreground and item in occluders)
                    canvas.itemconfigure(item, state=state if visible else 'hidden')
                layers.append(recover_alpha(self._capture_canvas('#000000', show_foreground),
                                            self._capture_canvas('#FFFFFF', show_foreground)))
        finally:
            for item, state in states.items():
                canvas.itemconfigure(item, state=state)
        return layers

    def request_frame(self):
        if self.active and not self._painting and self._pending is None:
            self._pending = self.root.after_idle(self.present)

    def _clip_corners(self):
        # DWM blur must be clipped too, otherwise it can leave a blurred
        # rectangle outside the alpha-rounded corners. Regions use device pixels.
        previous = self.u.SetThreadDpiAwarenessContext(self.c.c_void_p(-4))
        try:
            rect = self.w.RECT()
            if not self.u.GetWindowRect(self.hwnd, self.c.byref(rect)):
                return
            size = (rect.right - rect.left, rect.bottom - rect.top)
            if size == self._region_size:
                return
            diameter = round(60 * size[0] / self._size[0])
            region = self.g.CreateRoundRectRgn(0, 0, size[0] + 1, size[1] + 1, diameter, diameter)
            if region:
                if self.u.SetWindowRgn(self.hwnd, region, False):
                    self._region_size = size  # Windows now owns the region.
                else:
                    self.g.DeleteObject(region)
        finally:
            if previous:
                self.u.SetThreadDpiAwarenessContext(previous)

    def present(self):
        if self._pending is not None:
            self.root.after_cancel(self._pending)
            self._pending = None
        if not self.active or self._painting:
            return
        self._painting = True
        try:
            width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
            if min(width, height) < 2:
                return
            self._buffer(width, height)
            if (self._render_canvas.winfo_width(), self._render_canvas.winfo_height()) != (width, height):
                self._render_root.geometry(f'{width}x{height}+-32000+-32000')
                return
            if self.refraction:
                base, foreground = self._capture_layers()
                self.frame = composite_premultiplied(base, foreground)
                self._overlay = (base.tobytes(), foreground.tobytes())
                self._submit_refraction()
                return
            black, white = self._capture_canvas('#000000'), self._capture_canvas('#FFFFFF')
            self.frame = recover_alpha(black, white)
            raw = self.frame.tobytes('raw', 'BGRA')
            self._blit(raw)
        except (OSError, ValueError, tk.TclError) as exc:
            self.error = str(exc)
            self.close()
            self.on_failure(self.error)
        finally:
            self._painting = False

    def _blit(self, raw):
        from contextlib import nullcontext
        self.c.memmove(self._bits, raw, len(raw))
        size, source = self.w.SIZE(*self._size), self.w.POINT(0, 0)
        blend = self.Blend(0, 0, 255, 1)
        # Preserve the selected capture policy while Tk attributes are reapplied.
        with self._optics.capture_lock if self._optics is not None else nullcontext():
            color, opacity, flags = self.w.DWORD(), self.w.BYTE(), self.w.DWORD()
            if self.u.GetLayeredWindowAttributes(self.hwnd, self.c.byref(color), self.c.byref(opacity), self.c.byref(flags)):
                style = self.u.GetWindowLongW(self.hwnd, -20)
                self.u.SetWindowLongW(self.hwnd, -20, style & ~0x80000)
                self.u.SetWindowLongW(self.hwnd, -20, style | 0x80000)
                if self._optics is not None:
                    self._optics.u.SetWindowDisplayAffinity(self.hwnd, self._optics.affinity)
            if not self.u.UpdateLayeredWindow(self.hwnd, None, None, self.c.byref(size), self._dc,
                                             self.c.byref(source), 0, self.c.byref(blend), 2):
                raise self.c.WinError(self.c.get_last_error())
        self._clip_corners()
        if not self.frames:
            logging.getLogger('tokenpulse.monitor.glass').info('Desktop pixel alpha active; refraction=%s; size=%s', self.refraction, self._size)
        self.frames += 1

    def _release_buffer(self):
        if self._old_bitmap:
            self.g.SelectObject(self._dc, self._old_bitmap)
        if self._bitmap:
            self.g.DeleteObject(self._bitmap)
        if self._dc:
            self.g.DeleteDC(self._dc)
        self._dc = self._bitmap = self._old_bitmap = self._bits = self._size = None

    def snapshot_rgb(self, background):
        """An app-only image, without the user's desktop."""
        if self.frame is None:
            return None
        from PIL import Image, ImageChops
        alpha = self.frame.getchannel('A')
        behind = ImageChops.multiply(Image.new('RGB', self.frame.size, background),
                                     ImageChops.invert(alpha).convert('RGB'))
        return ImageChops.add(self.frame.convert('RGB'), behind)

    def close(self):
        if self._optics_pending is not None:
            self.root.after_cancel(self._optics_pending)
            self._optics_pending = None
        if self._optics is not None:
            self._optics.close()
            self._optics = None
        self.refraction = False
        if self.active:
            accent = self.Accent(0, 0, 0, 0)
            data = self.Composition(19, self.c.addressof(accent), self.c.sizeof(accent))
            self.u.SetWindowCompositionAttribute(self.hwnd, self.c.byref(data))
            self.u.SetWindowRgn(self.hwnd, None, False)
        self.active = False
        self.frame = self.display_frame = self._overlay = None
        self.canvas._paint_callback = None
        self.canvas._mirror = None
        self.canvas._mirror_ids = None
        if self._pending is not None:
            self.root.after_cancel(self._pending)
            self._pending = None
        self._release_buffer()
        if self._render_root.winfo_exists():
            self._render_root.destroy()

    def _destroyed(self, event):
        if event.widget == self.root:
            self.close()
