"""Real shader compilation and blur behavior when OpenGL 3.3 is available."""
from pathlib import Path
import unittest

try:
    import moderngl
except ImportError:
    moderngl = None

from monitor_refraction import BACKGROUND_BLUR_SIGMA, VERTEX


@unittest.skipIf(moderngl is None, 'ModernGL is optional')
class GlassGpuTests(unittest.TestCase):
    def test_blur_diffuses_background_detail_and_material_compiles(self):
        try:
            context = moderngl.create_context(standalone=True, require=330)
        except Exception as exc:
            self.skipTest(f'OpenGL 3.3 unavailable: {exc}')
        resources = []
        try:
            assets = Path(__file__).resolve().parents[1] / 'assets'
            material = context.program(vertex_shader=VERTEX,
                                       fragment_shader=(assets / 'liquid-glass.frag').read_text())
            resources.append(material)
            blur = context.program(vertex_shader=VERTEX,
                                   fragment_shader=(assets / 'glass-blur.frag').read_text())
            resources.append(blur)
            width, height = 64, 4
            source = context.texture((width, height), 4, b''.join(
                bytes((255, 255, 255, 255)) if x == 32 else bytes((0, 0, 0, 255))
                for _y in range(height) for x in range(width)))
            resources.append(source)
            source.repeat_x = source.repeat_y = False
            source.filter = (moderngl.LINEAR, moderngl.LINEAR)
            target = context.simple_framebuffer((width, height), components=4)
            resources.append(target)
            vao = context.vertex_array(blur, [])
            resources.append(vao)
            source.use(0)
            blur['sourceImage'] = 0
            blur['targetSize'] = (width, height)
            blur['stepUV'] = (1 / width, 0)
            # The runtime blurs a half-resolution background texture.
            blur['sigma'] = BACKGROUND_BLUR_SIGMA / 2
            target.use()
            context.viewport = (0, 0, width, height)
            vao.render(vertices=3)
            result = target.read(components=4)
            self.assertLess(result[32 * 4], 60, 'Blur should suppress the sharp stripe')
            self.assertGreater(result[29 * 4], 10, 'Blur should spread into neighboring pixels')
            self.assertEqual(result[0], 0, 'Distant background should remain unchanged')
            blur['sigma'] = 0
            vao.render(vertices=3)
            self.assertEqual(target.read(components=4), source.read(), 'Zero softness must be an exact passthrough')
            blur['sigma'] = 8
            vao.render(vertices=3)
            strong = target.read(components=4)
            self.assertLess(strong[32 * 4], result[32 * 4], 'Maximum softness spreads the central peak further')
            self.assertGreater(strong[24 * 4], result[24 * 4], 'Large sigma must reach beyond the old kernel')
        finally:
            for resource in reversed(resources):
                resource.release()
            context.release()

    def test_material_keeps_foreground_crisp_over_soft_background(self):
        try:
            context = moderngl.create_context(standalone=True, require=330)
        except Exception as exc:
            self.skipTest(f'OpenGL 3.3 unavailable: {exc}')
        resources = []
        try:
            source = Path(__file__).resolve().parents[1] / 'assets/liquid-glass.frag'
            material = context.program(vertex_shader=VERTEX, fragment_shader=source.read_text())
            resources.append(material)
            size = 128
            striped = b''.join(bytes((64 if x % 2 else 192,) * 3 + (255,))
                               for _y in range(size) for x in range(size))
            foreground = b''.join(bytes((0, 0, 0, 255 if x == 61 and 48 <= y < 80 else 0))
                                  for y in range(size) for x in range(size))
            pixels = (striped, foreground, bytes(size * size * 4),
                      bytes((128, 128, 128, 255)) * size * size)
            for slot, data in enumerate(pixels):
                texture = context.texture((size, size), 4, data)
                resources.append(texture)
                texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
                texture.repeat_x = texture.repeat_y = False
                texture.use(slot)
            for name, value in (('backdrop', 0), ('ink', 1), ('canvasBase', 2), ('softBackdrop', 3),
                                ('resolution', (size, size)), ('textureSize', (size, size)),
                                ('textureOffset', (0, 0)), ('pointer', (20, 20)),
                                ('tint', (.929, .949, .969)), ('darkTheme', 0), ('lensCount', 0), ('refractionStrength', 1.)):
                material[name] = value
            target = context.simple_framebuffer((size, size), components=4)
            resources.append(target)
            vao = context.vertex_array(material, [])
            resources.append(vao)
            target.use()
            context.viewport = (0, 0, size, size)
            vao.render(vertices=3)
            result = target.read(components=4)

            def pixel(x, y):
                start = (y * size + x) * 4
                return tuple(result[start:start + 4])

            self.assertEqual(pixel(61, 64), (0, 0, 0, 255), 'Foreground strokes stay opaque and crisp')
            self.assertGreater(min(pixel(60, 64)[:3]), 100, 'Ink must not bleed into the background')
            self.assertEqual(pixel(60, 64), pixel(62, 64), 'The center uses the diffused background')
            self.assertEqual(pixel(0, 0), (0, 0, 0, 0), 'Rounded exterior remains transparent')
            # Sample a striped background at the curved edge; the user control
            # must change displacement without fading the foreground or silhouette.
            resources[1].use(3)
            frames = []
            for strength in (0., 1., 2.):
                material['refractionStrength'] = strength
                vao.render(vertices=3)
                frame = target.read(components=4)
                frames.append(frame)
                self.assertEqual(frame[(64 * size + 61) * 4 + 3], 255, 'Foreground remains opaque as contrast adapts')
            self.assertNotEqual(frames[0], frames[1])
            self.assertNotEqual(frames[1], frames[2])
            self.assertEqual(frames[0][3::4], frames[2][3::4])
        finally:
            for resource in reversed(resources):
                resource.release()
            context.release()


if __name__ == '__main__':
    unittest.main()
