"""Read the windows behind the widget without hiding it from remote viewers.

PrintWindow reads a window's own rendering, so the floating widget cannot feed
back into its backdrop. Pixels stay in the material worker's memory.
"""
from __future__ import annotations

import ctypes as c
import time
from ctypes import wintypes as w
from types import SimpleNamespace


class WindowBackdropCapture:
    def __init__(self, hwnd):
        from PIL import Image, ImageChops, ImageDraw
        self.Image, self.Chops, self.Draw = Image, ImageChops, ImageDraw
        self.hwnd = hwnd
        self.u = c.WinDLL('user32', use_last_error=True)
        self.g = c.WinDLL('gdi32', use_last_error=True)
        self.dwm = c.WinDLL('dwmapi')
        self._surfaces = {}
        self._desktop_images = {}
        self.sources = []
        self._callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        class BitmapInfo(c.Structure):
            _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                        ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                        ('image_size', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                        ('colors', w.DWORD), ('important', w.DWORD)]

        self.BitmapInfo = BitmapInfo
        signatures = [
            (self.u.EnumWindows, [self._callback_type, w.LPARAM], w.BOOL),
            (self.u.IsWindowVisible, [w.HWND], w.BOOL),
            (self.u.IsIconic, [w.HWND], w.BOOL),
            (self.u.IsHungAppWindow, [w.HWND], w.BOOL),
            (self.u.GetWindowRect, [w.HWND, c.POINTER(w.RECT)], w.BOOL),
            (self.u.GetWindowDpiAwarenessContext, [w.HWND], c.c_void_p),
            (self.u.SetThreadDpiAwarenessContext, [c.c_void_p], c.c_void_p),
            (self.u.GetWindowLongW, [w.HWND, c.c_int], w.LONG),
            (self.u.GetWindowDisplayAffinity, [w.HWND, c.POINTER(w.DWORD)], w.BOOL),
            (self.u.GetClassNameW, [w.HWND, w.LPWSTR, c.c_int], c.c_int),
            (self.u.FindWindowExW, [w.HWND, w.HWND, w.LPCWSTR, w.LPCWSTR], w.HWND),
            (self.u.PrintWindow, [w.HWND, w.HDC, w.UINT], w.BOOL),
            (self.u.OpenInputDesktop, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.u.CloseDesktop, [w.HANDLE], w.BOOL),
            (self.dwm.DwmGetWindowAttribute, [w.HWND, w.DWORD, c.c_void_p, w.DWORD], w.LONG),
            (self.g.CreateCompatibleDC, [w.HDC], w.HDC),
            (self.g.CreateDIBSection, [w.HDC, c.POINTER(BitmapInfo), w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD], w.HBITMAP),
            (self.g.SelectObject, [w.HDC, w.HANDLE], w.HANDLE),
            (self.g.DeleteObject, [w.HANDLE], w.BOOL),
            (self.g.DeleteDC, [w.HDC], w.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def _release(self, hwnd):
        self._desktop_images.pop(hwnd, None)
        surface = self._surfaces.pop(hwnd, None)
        if surface:
            _size, dc, bitmap, old, _bits = surface
            self.g.SelectObject(dc, old)
            self.g.DeleteObject(bitmap)
            self.g.DeleteDC(dc)

    def _read(self, hwnd, size, desktop_layer=False):
        # PrintWindow renders in the source window's coordinate space. An
        # unaware Tk/GDI window at 125% otherwise leaves black padding in a
        # device-sized bitmap. Capture at its native DPI, then scale once.
        context = self.u.GetWindowDpiAwarenessContext(hwnd)
        previous = self.u.SetThreadDpiAwarenessContext(context) if context else None
        try:
            bounds = w.RECT()
            if not self.u.GetWindowRect(hwnd, c.byref(bounds)):
                raise OSError('The background window no longer exists')
            native_size = (bounds.right-bounds.left, bounds.bottom-bounds.top)
            if min(native_size) < 1:
                raise OSError('The background window is empty')
            image = self._read_surface(hwnd, native_size, desktop_layer)
        finally:
            if previous:
                self.u.SetThreadDpiAwarenessContext(previous)
        return image if image.size == size else image.resize(size, self.Image.Resampling.BILINEAR)

    def _read_surface(self, hwnd, size, desktop_layer=False):
        cached = self._desktop_images.get(hwnd)
        if desktop_layer and cached and cached[1].size == size and time.monotonic()-cached[0] < .25:
            return cached[1]
        if hwnd in self._surfaces and self._surfaces[hwnd][0] != size:
            self._release(hwnd)
        if hwnd not in self._surfaces:
            info = self.BitmapInfo(c.sizeof(self.BitmapInfo), size[0], -size[1], 1, 32, 0, 0, 0, 0, 0, 0)
            dc, bits = self.g.CreateCompatibleDC(None), c.c_void_p()
            bitmap = self.g.CreateDIBSection(dc, c.byref(info), 0, c.byref(bits), None, 0)
            if not dc or not bitmap:
                if bitmap:
                    self.g.DeleteObject(bitmap)
                if dc:
                    self.g.DeleteDC(dc)
                raise OSError('Cannot allocate background capture surface')
            old = self.g.SelectObject(dc, bitmap)
            self._surfaces[hwnd] = (size, dc, bitmap, old, bits)
        _size, dc, _bitmap, _old, bits = self._surfaces[hwnd]
        c.memset(bits, 0, size[0]*size[1]*4)
        if self.u.IsHungAppWindow(hwnd) or not self.u.PrintWindow(hwnd, dc, 2):
            raise OSError('The background window cannot be read')
        image = self.Image.frombytes('RGB', size, c.string_at(bits, size[0]*size[1]*4), 'raw', 'BGRX')
        if desktop_layer:
            self._desktop_images[hwnd] = (time.monotonic(), image)
        return image

    def grab(self, rect):
        desktop = self.u.OpenInputDesktop(0, False, 1)
        if not desktop:
            self._desktop_images.clear()
            raise OSError('The input desktop is locked or unavailable')
        self.u.CloseDesktop(desktop)
        left, top, width, height = (rect[k] for k in ('left', 'top', 'width', 'height'))
        output = self.Image.new('RGB', (width, height))
        remaining = self.Image.new('L', output.size, 255)
        # Pixels outside the virtual screen are black, as in desktop capture.
        vx, vy = self.u.GetSystemMetrics(76), self.u.GetSystemMetrics(77)
        vr, vb = vx+self.u.GetSystemMetrics(78), vy+self.u.GetSystemMetrics(79)
        valid = self.Image.new('L', output.size)
        box = (max(0, vx-left), max(0, vy-top), min(width, vr-left), min(height, vb-top))
        if box[2] > box[0] and box[3] > box[1]:
            valid.paste(255, box)
        remaining = self.Chops.multiply(remaining, valid)
        windows = []

        @self._callback_type
        def collect(hwnd, _):
            if hwnd == self.hwnd or not self.u.IsWindowVisible(hwnd) or self.u.IsIconic(hwnd):
                return True
            affinity = w.DWORD()
            if self.u.GetWindowDisplayAffinity(hwnd, c.byref(affinity)) and affinity.value == 0x11:
                return True
            bounds, cloaked = w.RECT(), w.DWORD()
            self.dwm.DwmGetWindowAttribute(hwnd, 14, c.byref(cloaked), 4)
            if cloaked.value or not self.u.GetWindowRect(hwnd, c.byref(bounds)):
                return True
            area = (bounds.left, bounds.top, bounds.right, bounds.bottom)
            if min(area[2], left+width) <= max(area[0], left) or min(area[3], top+height) <= max(area[1], top):
                return True
            name = c.create_unicode_buffer(128)
            self.u.GetClassNameW(hwnd, name, len(name))
            windows.append((hwnd, area, name.value))
            return True

        self.u.EnumWindows(collect, 0)
        used = []
        try:
            # EnumWindows gives front-to-back order. Paint only still-uncovered pixels.
            for hwnd, area, kind in windows:
                if not remaining.getbbox():
                    break
                x1, y1, x2, y2 = area
                box = (max(0, x1-left), max(0, y1-top), min(width, x2-left), min(height, y2-top))
                mask = self.Image.new('L', output.size)
                mask.paste(255, box)
                mask = self.Chops.multiply(mask, remaining)
                if not mask.getbbox():
                    continue
                used.append(hwnd)
                image = self._read(hwnd, (x2-x1, y2-y1), kind in {'WorkerW', 'Progman'})
                crop = image.crop((left-x1, top-y1, left+width-x1, top+height-y1))
                # Explorer paints its icon layer over black through PrintWindow.
                # Leave those clear pixels for the wallpaper window underneath.
                if kind in {'WorkerW', 'Progman'} and self.u.FindWindowExW(hwnd, None, 'SHELLDLL_DefView', None):
                    red, green, blue = crop.split()
                    ink = self.Chops.lighter(self.Chops.lighter(red, green), blue).point(lambda value: 255 if value else 0)
                    mask = self.Chops.multiply(mask, ink)
                output.paste(crop, (0, 0), mask)
                remaining = self.Chops.subtract(remaining, mask)
            if remaining.getbbox():
                raise OSError('No readable window covers part of the background')
            self.sources = used
            return SimpleNamespace(bgra=output.convert('RGBA').tobytes('raw', 'BGRA'))
        finally:
            for hwnd in tuple(self._surfaces):
                if hwnd not in used:
                    self._release(hwnd)

    def close(self):
        for hwnd in tuple(self._surfaces):
            self._release(hwnd)
