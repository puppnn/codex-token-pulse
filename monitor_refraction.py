"""Live desktop input and GPU material, isolated from Tk and usage data.

The worker owns its GPU/GDI resources. Only UI pixels enter submit(); only the
latest composed frame leaves result(). Desktop frames stay in memory.
"""
from __future__ import annotations

import ctypes
from contextlib import nullcontext
import os
import logging
from pathlib import Path
import struct
import threading
import time


VERTEX = '#version 330\nvoid main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));gl_Position=vec4(p*2.-1.,0,1);}'
BACKGROUND_BLUR_SIGMA = 4.0  # Logical UI pixels, independent of display scaling.


class DesktopRefraction:
    def __init__(self, hwnd):
        # MSS tries to change process DPI awareness. Latch the GUI's existing
        # mode first; the capture worker uses its own per-monitor thread mode.
        awareness = ctypes.c_int()
        shcore = ctypes.WinDLL('shcore')
        if shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) == 0:
            shcore.SetProcessDpiAwareness(awareness.value)
        # Validate optional dependencies before altering the window's capture policy.
        import moderngl
        import mss
        self.gl, self.mss, self.hwnd = moderngl, mss, hwnd
        self.capture_mode = os.environ.get('TOKEN_MONITOR_CAPTURE_MODE', 'windows')
        self.affinity = 0x11 if self.capture_mode == 'desktop' else 0
        self._request = self._result = None
        self._lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error = ''
        self.live = False
        self.renderer = ''
        self.frames = 0
        self.frame_ms = 0.
        self.u = ctypes.WinDLL('user32', use_last_error=True)
        self.u.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.u.SetWindowDisplayAffinity.restype = ctypes.c_bool
        self.u.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        self.u.GetWindowDisplayAffinity.restype = ctypes.c_bool
        self.u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.u.GetWindowLongW.restype = ctypes.c_long
        self.u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        self.u.SetWindowLongW.restype = ctypes.c_long
        self.u.IsWindow.argtypes = [ctypes.c_void_p]
        self.u.IsWindow.restype = ctypes.c_bool
        self.u.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.u.IsWindowVisible.restype = ctypes.c_bool
        previous = ctypes.c_uint()
        self.u.GetWindowDisplayAffinity(hwnd, ctypes.byref(previous))
        self.previous_affinity = previous.value
        if not self._set_affinity(self.affinity):
            raise OSError('Cannot configure the glass window capture policy')
        self._thread = threading.Thread(target=self._run, name='TokenPulseGlass', daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self.error:
            self.close()
            raise OSError(self.error or 'GPU material initialization timed out')

    def submit(self, size, rect, overlay, tint, dark, lenses, pointer,
               blur_radius=BACKGROUND_BLUR_SIGMA, refraction_strength=1.0):
        with self._lock:
            self._request = (size, rect, overlay, tint, dark, lenses, pointer, blur_radius, refraction_strength)

    def result(self):
        with self._lock:
            result, self._result = self._result, None
        return result

    def _set_affinity(self, value):
        if self.u.SetWindowDisplayAffinity(self.hwnd, value):
            return True
        # Windows 22000 can return ERROR_NOT_ENOUGH_MEMORY once ULW has
        # installed a surface. Release that surface before changing affinity.
        style = self.u.GetWindowLongW(self.hwnd, -20)
        self.u.SetWindowLongW(self.hwnd, -20, style & ~0x80000)
        try:
            return bool(self.u.SetWindowDisplayAffinity(self.hwnd, value))
        finally:
            self.u.SetWindowLongW(self.hwnd, -20, style)

    def _run(self):
        from PIL import Image
        context = capture = program = vao = framebuffer = background = ink = canvas_base = None
        blur_program = blur_vao = blur_horizontal = blur_vertical = None
        allocated = None
        previous_overlay = None
        last_live = None
        try:
            # Keep device pixels confined to this worker; do not change Tk's DPI mode.
            self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            context = self.gl.create_context(standalone=True, require=330)
            self.renderer = context.info['GL_RENDERER']
            source = (Path(__file__).parent / 'assets' / 'liquid-glass.frag').read_text(encoding='utf-8')
            program = context.program(vertex_shader=VERTEX, fragment_shader=source)
            vao = context.vertex_array(program, [])
            blur_source = (Path(__file__).parent / 'assets' / 'glass-blur.frag').read_text(encoding='utf-8')
            blur_program = context.program(vertex_shader=VERTEX, fragment_shader=blur_source)
            blur_vao = context.vertex_array(blur_program, [])
            blur_program['sourceImage'] = 0
            if self.capture_mode == 'desktop':
                capture = self.mss.MSS()
            else:
                from monitor_capture import WindowBackdropCapture
                capture = WindowBackdropCapture(self.hwnd)
            program['backdrop'], program['ink'] = 0, 1
            program['canvasBase'] = 2
            program['softBackdrop'] = 3
            self._ready.set()
            while not self._stop.is_set():
                start = time.perf_counter()
                with self._lock:
                    request = self._request
                if request is None:
                    self._stop.wait(.025)
                    continue
                if not self.u.IsWindowVisible(self.hwnd):
                    self._stop.wait(.25)
                    continue
                size, rect, overlay, tint, dark, lenses, pointer, blur_radius, refraction_strength = request
                w, h = size
                left, top, right, bottom = rect
                pw, ph, pad = right-left, bottom-top, 32
                if min(pw, ph, w, h) < 2:
                    self._stop.wait(.1)
                    continue
                allocation = (size, pw, ph)
                if allocation != allocated:
                    for buffer in (blur_horizontal, blur_vertical):
                        if buffer is not None:
                            for texture in buffer.color_attachments:
                                texture.release()
                    for resource in (framebuffer, background, ink, canvas_base, blur_horizontal, blur_vertical):
                        if resource is not None:
                            resource.release()
                    framebuffer = context.simple_framebuffer(size, components=4)
                    background = context.texture((pw+pad*2, ph+pad*2), 4)
                    # A half-size surface keeps the two blur passes inexpensive.
                    blur_size = (max(1, (pw+pad*2)//2), max(1, (ph+pad*2)//2))
                    blur_horizontal = context.framebuffer(color_attachments=[context.texture(blur_size, 4)])
                    blur_vertical = context.framebuffer(color_attachments=[context.texture(blur_size, 4)])
                    ink = context.texture(size, 4)
                    canvas_base = context.texture(size, 4)
                    for texture in (background, ink, canvas_base, *blur_horizontal.color_attachments, *blur_vertical.color_attachments):
                        texture.filter = (self.gl.LINEAR, self.gl.LINEAR)
                        texture.repeat_x = texture.repeat_y = False
                    background.swizzle = 'BGRA'
                    allocated, previous_overlay = allocation, None
                if overlay is not previous_overlay:
                    canvas_base.write(overlay[0])
                    ink.write(overlay[1])
                    previous_overlay = overlay
                try:
                    with self.capture_lock if self.affinity else nullcontext():
                        affinity = ctypes.c_uint()
                        if self.affinity and (not self.u.GetWindowDisplayAffinity(self.hwnd, ctypes.byref(affinity)) or affinity.value != self.affinity):
                            if not self._set_affinity(self.affinity):
                                raise OSError('Window capture policy unavailable')
                        frame = capture.grab({'left':left-pad, 'top':top-pad, 'width':pw+2*pad, 'height':ph+2*pad})
                    background.write(frame.bgra)
                    self.live = True
                except Exception:
                    # Clear stale desktop pixels immediately; the fallback is a solid material.
                    pixel = bytes((round(tint[2]*255), round(tint[1]*255), round(tint[0]*255), 255))
                    background.write(pixel * ((pw+2*pad)*(ph+2*pad)))
                    self.live = False
                if self.live != last_live:
                    logging.getLogger('tokenpulse.monitor.glass').info(
                        'Curved refraction: %s; capture=%s; GPU=%s',
                        'live desktop' if self.live else 'desktop unavailable; solid fallback',
                        self.capture_mode, self.renderer)
                    last_live = self.live
                blur_size = blur_horizontal.size
                blur_program['targetSize'] = blur_size
                context.viewport = (0, 0, *blur_size)
                # Sample in physical coordinates, retaining the same softness at each DPI.
                blur_program['sigma'] = blur_radius * pw/w * blur_size[0]/background.width
                blur_program['stepUV'] = (1.0/blur_size[0], 0.0)
                background.use(0)
                blur_horizontal.use()
                blur_vao.render(vertices=3)
                blur_program['sigma'] = blur_radius * ph/h * blur_size[1]/background.height
                blur_program['stepUV'] = (0.0, 1.0/blur_size[1])
                blur_horizontal.color_attachments[0].use(0)
                blur_vertical.use()
                blur_vao.render(vertices=3)
                program['resolution'] = size
                program['textureSize'] = ((pw+2*pad)*w/pw, (ph+2*pad)*h/ph)
                program['textureOffset'] = (pad*w/pw, pad*h/ph)
                program['pointer'] = pointer
                program['tint'] = tint
                program['darkTheme'] = float(dark)
                program['refractionStrength'] = refraction_strength
                program['lensCount'] = len(lenses)
                rectangles = [value for lens in lenses for value in lens[:4]] + [0.] * (16-len(lenses)*4)
                shapes = [value for lens in lenses for value in lens[4:]] + [0.] * (8-len(lenses)*2)
                program['lenses'].write(struct.pack('16f', *rectangles))
                program['lensShape'].write(struct.pack('8f', *shapes))
                background.use(0)
                ink.use(1)
                canvas_base.use(2)
                # Zero softness samples the full-resolution source, avoiding the
                # half-size blur buffer's resampling even when sigma is zero.
                (blur_vertical.color_attachments[0] if blur_radius > 0 else background).use(3)
                framebuffer.use()
                context.viewport = (0, 0, w, h)
                vao.render(vertices=3)
                image = Image.frombytes('RGBA', size, framebuffer.read(components=4)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                pixels = image.tobytes('raw', 'BGRA')
                self.frame_ms = (time.perf_counter()-start)*1000
                with self._lock:
                    self._result = (size, pixels)
                self.frames += 1
                self._stop.wait(max(0., (1/60 if self.live else .5)-(time.perf_counter()-start)))
        except Exception as exc:
            self.error = str(exc)
            self.live = False
        finally:
            self._ready.set()
            if capture is not None:
                capture.close()
            for resource in (blur_horizontal, blur_vertical):
                if resource is not None:
                    for texture in resource.color_attachments:
                        texture.release()
                    resource.release()
            for resource in (blur_vao, blur_program, vao, program, framebuffer, background, ink, canvas_base, context):
                if resource is not None:
                    resource.release()

    def close(self):
        self._stop.set()
        if hasattr(self, '_thread'):
            self._thread.join(timeout=2)
        with self.capture_lock:
            if self.u.IsWindow(self.hwnd) and not self._set_affinity(self.previous_affinity):
                logging.getLogger('tokenpulse.monitor.glass').warning('Could not restore window capture policy')
        with self._lock:
            self._request = self._result = None
