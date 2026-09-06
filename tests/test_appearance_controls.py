"""Appearance interaction, persistence and material behavior without account I/O."""
import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import monitor
from monitor_ui import WorkspaceUI
from monitor_window import CompositedCanvas


class AppearancePreview:
    """Only the app's Canvas contract; no collectors, account files or network."""
    WIDTH, HEIGHT, WINDOW_ALPHA = 390, 760, .99
    closed = False
    state = None
    error = ''
    _refresh_pending = _loading = False
    _pinned = True
    _main_tab = 'accounts'
    _hover_btn = _tooltip_text = ''

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry('390x760+60+60')
        self.canvas = CompositedCanvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack(fill='both', expand=True)
        self.root.update()
        self._btn_rects = {}
        self.ui = WorkspaceUI(self, monitor)
        self.ui.appearance_open = True
        self._draw()

    def _draw(self):
        self.canvas.delete('all')
        self._btn_rects.clear()
        self.ui.shell()
        self.ui.appearance()
        self.ui.finish()

    def _add_tooltip(self, *_args):
        pass

    def close_app(self):
        self.closed = True
        if self.ui.desktop:
            self.ui.desktop.close()
        for timer in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(timer)
        self.root.destroy()


class AppearanceSettingTests(unittest.TestCase):
    def test_invalid_numbers_cannot_reach_shader(self):
        for value in (None, {}, 'bad', float('inf'), float('-inf'), float('nan')):
            with self.subTest(value=value):
                self.assertEqual(WorkspaceUI._bounded_setting(value, 4., 10.), 4.)
        self.assertEqual(WorkspaceUI._bounded_setting(-1, 4., 10.), 0.)
        self.assertEqual(WorkspaceUI._bounded_setting(99, 4., 10.), 10.)


@unittest.skipUnless(os.name == 'nt', 'Windows Canvas UI')
class AppearanceWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(monitor, 'APP_DIR', Path(self.directory.name)))
        stack.enter_context(patch.dict(os.environ, {'TOKEN_MONITOR_DESKTOP_GLASS': '0'}))
        stack.enter_context(patch.object(WorkspaceUI, '_prefers_reduced_motion', return_value=False))
        self.app = AppearancePreview()
        self.addCleanup(self.app.close_app)

    def test_opaque_is_solid_and_survives_reload_with_optics_settings(self):
        app, ui = self.app, self.app.ui
        ui.blur_radius, ui.refraction_strength = 7.3, 1.65
        ui.handle_button('material_opaque')
        self.assertEqual(app.root.attributes('-alpha'), 1.)
        self.assertFalse(ui._sliders)
        if ui.glass:
            self.assertEqual(ui._backdrop.getchannel('A').getextrema(), (255, 255))
            self.assertEqual(len(ui._backdrop.getcolors()), 1, 'Pure color has no decorative backdrop')
        saved = json.loads(ui._appearance_path.read_text())
        self.assertEqual((saved['material'], saved['blur'], saved['refraction']), ('opaque', 7.3, 1.65))
        reloaded = AppearancePreview()
        try:
            self.assertEqual((reloaded.ui.material_name, reloaded.ui.blur_radius, reloaded.ui.refraction_strength),
                             ('opaque', 7.3, 1.65))
        finally:
            reloaded.close_app()

    def test_controls_fit_minimum_portrait_size_without_overlaps(self):
        for width, height in ((360, 640), (390, 760)):
            app = self.app
            app.WIDTH, app.HEIGHT = width, height
            app.root.geometry(f'{width}x{height}+60+60')
            app._draw()
            texts = [(app.canvas.itemcget(i, 'text'), app.canvas.bbox(i))
                     for i in app.canvas.find_all() if app.canvas.type(i) == 'text']
            for index, (label, a) in enumerate(texts):
                self.assertTrue(a[0] >= 0 and a[1] >= 0 and a[2] <= width and a[3] <= height, (label, a))
                for other, b in texts[index + 1:]:
                    self.assertFalse(min(a[2], b[2]) - max(a[0], b[0]) > 2 and
                                     min(a[3], b[3]) - max(a[1], b[1]) > 2, (label, other))

    @unittest.skipIf(monitor.Image is None, 'Preview requires optional Pillow')
    def test_flat_materials_still_demonstrate_motion_without_refraction(self):
        app, ui = self.app, self.app.ui
        for material in ('alpha', 'opaque'):
            ui.handle_button('material_' + material)
            ui.reduced_motion = False
            ui._demo_pressure = ui._demo_released_at = 0.
            app._draw()
            self.assertFalse(ui._sliders)
            still = app.canvas.bbox('liquid_demo')
            self.assertTrue(ui.pointer_press(*ui._demo_position))
            pressed = app.canvas.bbox('liquid_demo')
            self.assertGreaterEqual(pressed[2] - pressed[0], still[2] - still[0] + 10)
            ui.handle_button('appearance_motion')
            self.assertEqual(app.canvas.bbox('liquid_demo'), still)
            ui.pointer_release()
            ui.pointer_press(*ui._demo_position)
            ui.pointer_release()
            self.assertEqual(app.canvas.bbox('liquid_demo'), still)
            self.assertIsNone(ui._animation_id)

    @unittest.skipIf(monitor.Image is None, 'Native preview requires optional Pillow')
    def test_drag_keyboard_and_motion_gate(self):
        from types import SimpleNamespace
        app, ui = self.app, self.app.ui
        self.addCleanup(setattr, ui, 'desktop', None)
        # Exercise actual UI event handlers against a native-compositor contract.
        ui.desktop = SimpleNamespace(active=True, refraction=True, lenses={}, pointer=(0., 0.))
        app._draw()
        l, t, r, b = ui._sliders['blur']
        self.assertTrue(ui.pointer_press(l, (t + b) / 2))
        ui.pointer_drag(r + 30, (t + b) / 2)
        ui.pointer_release()
        self.assertEqual(ui.blur_radius, 10.)
        self.assertEqual(ui.desktop.blur_radius, 10.)
        self.assertEqual(json.loads(ui._appearance_path.read_text())['blur'], 10.)
        ui.focused = 'slider_refraction'
        ui.adjust_focus(-1, endpoint=True)
        self.assertEqual(ui.refraction_strength, 0.)
        ui.adjust_focus(1)
        self.assertEqual(ui.refraction_strength, .05)
        self.assertEqual(ui.desktop.refraction_strength, .05)
        x, y = ui._demo_position
        self.assertTrue(ui.pointer_press(x, y))
        self.assertGreater(ui.desktop.lenses['demo'][-1], 0.)
        ui._light = .9
        ui.handle_button('appearance_motion')
        self.assertEqual(ui.desktop.lenses['demo'][-1], 0.)
        self.assertEqual(ui._light, 0.)
        self.assertIsNone(ui._animation_id)
        self.assertEqual(ui.desktop.pointer, (0., 0.))
        ui.pointer_release()
        ui.desktop.active = False  # A failed/closed GPU must not advertise a live preview.
        app._draw()
        self.assertFalse(ui._sliders)
        self.assertIsNone(ui._demo_position)
        ui.desktop = None


if __name__ == '__main__':
    unittest.main()
