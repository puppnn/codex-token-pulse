"""Native frame coherence under repeated UI redraws with a fixed backdrop."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import monitor
from tests.test_orbit_ui import PreviewApp, preview_context, preview_state
from monitor_window import CompositedCanvas, DesktopCompositor


class FrameTransactionTests(unittest.TestCase):
    def test_canvas_only_publishes_after_outer_frame_finishes(self):
        canvas = CompositedCanvas.__new__(CompositedCanvas)
        callback = Mock()
        canvas._paint_callback = callback
        with canvas.frame_update():
            canvas._changed()
            with canvas.frame_update():
                canvas._changed()
            callback.assert_not_called()
        callback.assert_called_once()
        self.assertEqual(canvas._paint_revision, 2)

    def test_gpu_poll_defers_during_matting_or_scene_update(self):
        for painting, depth in ((True, 0), (False, 1)):
            compositor = DesktopCompositor.__new__(DesktopCompositor)
            compositor.active = True
            compositor._painting = painting
            compositor.canvas = SimpleNamespace(_frame_depth=depth)
            compositor.root = Mock()
            compositor._optics = Mock()
            compositor._submit_refraction = Mock()
            compositor._blit = Mock()
            compositor._poll_refraction()
            compositor._optics.result.assert_not_called()
            compositor._blit.assert_not_called()
            compositor._submit_refraction.assert_not_called()
            compositor.root.after.assert_called_once_with(16, compositor._poll_refraction)

    def test_mutated_layers_do_not_replace_previous_complete_overlay(self):
        compositor = DesktopCompositor.__new__(DesktopCompositor)
        compositor.active, compositor._painting, compositor.refraction = True, False, True
        compositor._pending = None
        compositor.canvas = SimpleNamespace(_frame_depth=0, _paint_revision=1,
                                           winfo_width=lambda: 20, winfo_height=lambda: 20)
        compositor._render_canvas = SimpleNamespace(winfo_width=lambda: 20, winfo_height=lambda: 20)
        compositor._buffer = Mock()
        compositor._overlay = previous = (b'complete-base', b'complete-ink')
        compositor._submit_refraction = Mock()
        compositor.request_frame = Mock()

        def mixed_layers():
            compositor.canvas._paint_revision += 1
            return None, None

        compositor._capture_layers = mixed_layers
        compositor.present()
        self.assertIs(compositor._overlay, previous)
        compositor._submit_refraction.assert_not_called()
        compositor.request_frame.assert_called_once()


@unittest.skipUnless(os.name == 'nt', 'Windows compositor required')
class GlassFrameCoherenceTests(unittest.TestCase):
    def test_repeated_redraws_keep_static_gpu_pixels_stable(self):
        try:
            import moderngl
            from PIL import Image
        except ImportError:
            self.skipTest('Optional GPU dependencies unavailable')
        from monitor_capture import WindowBackdropCapture

        def fixed_backdrop(_capture, rect):
            return SimpleNamespace(bgra=bytes((110, 100, 90, 255)) * (rect['width'] * rect['height']))

        with tempfile.TemporaryDirectory() as directory, preview_context(directory), \
             patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '1',
                                     'TOKEN_MONITOR_REFRACTION': '1', 'TOKEN_MONITOR_REDUCE_MOTION': '1'}), \
             patch.object(WindowBackdropCapture, 'grab', fixed_backdrop), \
             patch.object(monitor, 'load_usage_history', return_value={'days': {}}):
            app = PreviewApp()
            try:
                app.state = preview_state()
                app._filter_account_display_rows = lambda rows: rows
                app._apply_window_size(390, 760)
                app._draw()
                native = app._desktop_compositor
                if native is None or not native.refraction:
                    self.skipTest('GPU compositor unavailable')
                samples = []
                # Let the initial loading frame and native geometry handshake
                # leave the worker before measuring a fully constructed scene.
                sample_after = time.monotonic() + .75
                deadline = sample_after + 8.0
                next_draw = 0.
                while time.monotonic() < deadline and len(samples) < 16:
                    if time.monotonic() >= next_draw:
                        app._draw()
                        next_draw = time.monotonic() + .07
                    app.root.update()
                    frame = native.display_frame
                    if frame and native._optics.frames > 4 and time.monotonic() >= sample_after:
                        picture = Image.frombytes('RGBA', frame[0], frame[1], 'raw', 'BGRA')
                        samples.append(picture.crop((50, 170, 340, 310)).tobytes())
                    time.sleep(.012)
                self.assertGreater(len(samples), 8)
                variants = list(dict.fromkeys(samples))
                if len(variants) > 1:
                    from PIL import ImageChops, ImageStat
                    first, last = [Image.frombytes('RGBA', (290, 140), raw).convert('RGB')
                                   for raw in (variants[0], variants[-1])]
                    difference = ImageChops.difference(first, last)
                    message = f'variants={len(variants)} bbox={difference.getbbox()} mean={ImageStat.Stat(difference).mean}'
                else:
                    message = ''
                self.assertEqual(len(variants), 1, message)
                self.assertNotEqual(native._bits.value, native._capture_bits.value)
            finally:
                app.close_app()
