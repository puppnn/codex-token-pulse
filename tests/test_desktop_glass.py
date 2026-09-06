import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from monitor_window import DesktopCompositor, recover_alpha, composite_premultiplied

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, 'Pillow is optional')
class DesktopGlassTests(unittest.TestCase):
    def test_glass_and_opaque_ink_keep_independent_opacity(self):
        source = Image.new('RGBA', (3, 1))
        source.putdata([(237, 242, 247, 166), (27, 48, 70, 255), (0, 0, 0, 0)])
        black = Image.alpha_composite(Image.new('RGBA', source.size, 'black'), source)
        white = Image.alpha_composite(Image.new('RGBA', source.size, 'white'), source)
        frame = recover_alpha(black, white)
        self.assertEqual(frame.getpixel((0, 0))[3], 166)
        self.assertEqual(frame.getpixel((1, 0)), (27, 48, 70, 255))
        self.assertEqual(frame.getpixel((2, 0)), (0, 0, 0, 0))
        for color in frame.getpixel((0, 0))[:3]:
            self.assertLessEqual(color, 166)

    def test_desktop_changes_affect_glass_but_not_text(self):
        compositor = DesktopCompositor.__new__(DesktopCompositor)
        compositor.frame = Image.new('RGBA', (2, 1))
        compositor.frame.putdata([(154, 157, 160, 166), (27, 48, 70, 255)])
        red = compositor.snapshot_rgb('#CC3322')
        blue = compositor.snapshot_rgb('#2266CC')
        self.assertNotEqual(red.getpixel((0, 0)), blue.getpixel((0, 0)))
        self.assertEqual(red.getpixel((1, 0)), blue.getpixel((1, 0)))

    def test_lens_does_not_turn_a_translucent_source_opaque(self):
        from monitor_glass import GlassRenderer, PALETTES
        renderer = GlassRenderer()
        background = renderer.backdrop(180, 100, PALETTES['silver'], desktop=True)
        lens = renderer.render(background, 20, 20, 120, 50, 25, PALETTES['silver'])
        self.assertTrue(166 <= lens.getpixel((72, 37))[3] < 220)
        self.assertEqual(lens.getpixel((0, 0))[3], 0)

    def test_app_snapshot_ignores_the_composed_desktop_frame(self):
        compositor = DesktopCompositor.__new__(DesktopCompositor)
        compositor.frame = Image.new('RGBA', (4, 4), (0, 0, 0, 0))
        compositor.display_frame = ((4, 4), Image.new('RGBA', (4, 4), '#FF00FF').tobytes())
        self.assertEqual(compositor.snapshot_rgb('#EDF2F7').getpixel((2, 2)), (237, 242, 247))

    def test_separate_ink_does_not_lower_opaque_text_alpha(self):
        base = Image.new('RGBA', (2, 1), (80, 90, 100, 128))
        foreground = Image.new('RGBA', (2, 1))
        foreground.putdata([(27, 48, 70, 255), (0, 0, 0, 0)])
        combined = composite_premultiplied(base, foreground)
        self.assertEqual(combined.getpixel((0, 0)), (27, 48, 70, 255))
        self.assertEqual(combined.getpixel((1, 0)), (80, 90, 100, 128))

    def test_labels_remain_legible_on_extreme_desktop_colors(self):
        from monitor_ui import WorkspaceUI
        from monitor_glass import PALETTES

        def luminance(color):
            values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * weight
                       for v, weight in zip(values, (.2126, .7152, .0722)))

        ui = WorkspaceUI.__new__(WorkspaceUI)
        ui.material_name = 'liquid'
        ui.desktop = SimpleNamespace(active=True)
        for palette in PALETTES.values():
            ui.palette, ui.BG, ui.TEXT = palette, palette.background, palette.text
            ui._text_colors = {}
            worst = ui.blend('#FFFFFF' if palette.dark else '#000000', ui.BG,
                             (184 if palette.dark else 166) / 255)
            for color in (palette.text, palette.secondary, palette.muted, palette.accent):
                ink, background = luminance(ui.legible(color)), luminance(worst)
                self.assertGreaterEqual((max(ink, background) + .05) / (min(ink, background) + .05), 4.5,
                                        (palette.name, color))

    def test_explicit_opaque_mode_does_not_create_native_windows(self):
        with patch.dict(os.environ, {'TOKEN_MONITOR_DESKTOP_GLASS': '0'}):
            self.assertIsNone(DesktopCompositor.create(None, None, None))


if __name__ == '__main__':
    unittest.main()
