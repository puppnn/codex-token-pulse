"""Native capture regression: the widget stays visible without sampling itself."""
import ctypes
import os
import unittest

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = ImageGrab = None

@unittest.skipIf(Image is None, 'Pillow is optional')
@unittest.skipUnless(os.name == 'nt', 'Requires a Windows desktop')
class RemoteGlassCaptureTests(unittest.TestCase):
    def test_visible_widget_reads_changing_windows_behind_it(self):
        for dpi_context in (-4, -1):
            with self.subTest(dpi_context=dpi_context):
                self.capture_scene(dpi_context)

    def capture_scene(self, dpi_context):
        from ctypes import wintypes as w
        import tkinter as tk
        from monitor_capture import WindowBackdropCapture

        u = ctypes.WinDLL('user32', use_last_error=True)
        u.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        u.OpenInputDesktop.restype = w.HANDLE
        u.CloseDesktop.argtypes = [w.HANDLE]
        desktop = u.OpenInputDesktop(0, False, 1)
        if not desktop:
            self.skipTest('The interactive desktop is locked or unavailable')
        u.CloseDesktop(desktop)
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u.GetAncestor.argtypes, u.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
        u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        u.GetWindowDisplayAffinity.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        previous = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(dpi_context))
        root = reader = None
        try:
            root = tk.Tk()
            root.title('Token Pulse capture regression')
            root.overrideredirect(True)
            root.attributes('-topmost', True)
            root.configure(bg='#113355')
            root.geometry('280x200+80+120')
            stripe = tk.Toplevel(root)
            stripe.overrideredirect(True)
            stripe.attributes('-topmost', True)
            stripe.configure(bg='#3366cc')
            stripe.geometry('100x200+260+120')
            widget = tk.Toplevel(root)
            widget.overrideredirect(True)
            widget.attributes('-topmost', True)
            widget.configure(bg='#ea823d')
            widget.geometry('180x120+160+160')
            root.update()
            stripe.lift()
            widget.lift()
            root.update()
            ctypes.WinDLL('dwmapi').DwmFlush()
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            hwnd = u.GetAncestor(widget.winfo_id(), 2)
            bounds, affinity = w.RECT(), w.DWORD()
            u.GetWindowRect(hwnd, ctypes.byref(bounds))
            self.assertTrue(u.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)))
            self.assertEqual(affinity.value, 0, 'The widget must remain in remote capture')
            rect = dict(left=bounds.left, top=bounds.top,
                        width=bounds.right-bounds.left, height=bounds.bottom-bounds.top)
            reader = WindowBackdropCapture(hwnd)

            def backdrop():
                frame = reader.grab(rect)
                return Image.frombytes('RGB', (rect['width'], rect['height']), frame.bgra, 'raw', 'BGRX')

            first = backdrop()
            self.assertEqual(first.getpixel((20, 20)), (17, 51, 85))
            self.assertEqual(first.getpixel((150, 20)), (51, 102, 204))
            self.assertEqual(first.getpixel((rect['width']-15, rect['height']-15)), (51, 102, 204),
                             'DPI scaling must not leave black right/bottom margins')
            root.configure(bg='#00bb88')
            root.update()
            self.assertEqual(backdrop().getpixel((20, 20)), (0, 187, 136))
            visible = ImageGrab.grab(bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom))
            self.assertEqual(visible.getpixel((20, 20)), (234, 130, 61),
                             'A normal screen capture must still contain the widget')
        finally:
            if reader:
                reader.close()
            if root:
                root.destroy()
            if previous:
                u.SetThreadDpiAwarenessContext(previous)


if __name__ == '__main__':
    unittest.main()
