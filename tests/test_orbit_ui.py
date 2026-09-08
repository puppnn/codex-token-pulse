"""Exercise the new view against current monitor state, without account I/O."""
import os
import json
import tempfile
import time
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import monitor
from monitor_ui import WorkspaceUI


class QuietWatcher:
    def __init__(self, *_args, **_kwargs):
        pass

    def poll(self):
        return False

    def close(self):
        pass


class PreviewApp(monitor.FloatingMonitorApp):
    def noop(self, *_args, **_kwargs):
        pass

    _capture_auth_switch = noop
    _restore_live_usage_checkpoint = noop
    _persist_live_usage_checkpoint = noop
    _schedule_live_active_refresh = noop
    _schedule_auth_switch_refresh = noop
    _schedule_live_usage_refresh = noop
    _start_initial_live_catchup = noop
    _schedule_midnight_refresh = noop
    _schedule_auto_refresh = noop
    refresh_async = noop

    def close_app(self):
        # A test may create another Tk interpreter after this window closes.
        for timer in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(timer)
        super().close_app()


def preview_state():
    now = monitor.datetime.now(monitor.CN_TZ)
    rows = []
    for index, name in enumerate(('Atlas Research', 'API Studio', 'Northstar Lab')):
        window = dict(tokens=12000 + index * 3200, requests=12, cost=.21,
                      utilization=26 + index * 21, quota_available=True,
                      resets_at=(now + monitor.timedelta(hours=2)).isoformat())
        rows.append(dict(name=name, plan_type='pro', tokens=184000 + index * 52000,
                         requests=28, cost=1.27, last_used_at=now.isoformat(),
                         models={f'model-{index + 1}': 184000 + index * 52000},
                         window_5h=dict(window), window_7d=dict(window)))
    rows[0]['window_cycle'] = dict(rows[0]['window_7d'], tokens=921000)
    weights = [(h * 7) % 13 + 1 for h in range(24)]
    hourly = [dict(hour=h, tokens=int(708000 * weight / sum(weights)),
                   requests=4 if h < 12 else 3, cost=3.81 / 24) for h, weight in enumerate(weights)]
    hourly[-1]['tokens'] += 708000 - sum(row['tokens'] for row in hourly)
    return monitor.MonitorState(
        loading=False, mode='local', usage_source='local', source_label='Demo',
        updated_at=time.time(), top_accounts=rows,
        active_accounts=[dict(rows[0], current=2, max=5)],
        today_tokens=708000, today_requests=84, today_account_cost=3.81,
        client_usage=dict(tokens=708000, requests=84, cost=3.81, dashboard={'hourly_today': hourly},
                          models={'model-a': 524000, 'model-b': 184000},
                          input_tokens=108000, cached_input_tokens=510000,
                          output_tokens=90000, providers=rows),
        usage_sync={'state': 'fresh'},
    )


def preview_context(directory):
    """Isolate readers AND already-bound persistence paths from real data."""
    stack = ExitStack()
    stack.enter_context(patch.object(monitor, 'APP_DIR', Path(directory)))
    for name in (
        'USAGE_HISTORY_JSON', 'CLIENT_USAGE_JSON', 'LIVE_USAGE_CHECKPOINT_JSON',
        'ACCOUNT_TYPE_HISTORY_JSON', 'AUTH_SWITCH_EVENTS_PATH',
        'ATTRIBUTION_DIAGNOSTICS_PATH', 'MODEL_PRICE_CACHE_JSON',
    ):
        original = getattr(monitor, name)
        stack.enter_context(patch.object(monitor, name, Path(directory) / original.name))
    stack.enter_context(patch.object(monitor, '_USAGE_HISTORY_CACHE', None))
    stack.enter_context(patch.object(monitor, 'Sub2APIClient', return_value=SimpleNamespace(
        mode='preview', include_history_details=True, include_account_30d=True)))
    stack.enter_context(patch.object(monitor, 'CodexUsageFileWatcher', QuietWatcher))
    stack.enter_context(patch.object(monitor, 'GrokUsageFileWatcher', QuietWatcher))
    stack.enter_context(patch.object(monitor, 'local_account_type_map', return_value={}))
    return stack


class OrbitStateTests(unittest.TestCase):
    def test_preview_history_writes_are_confined_to_temporary_directory(self):
        production_path = monitor.USAGE_HISTORY_JSON
        with tempfile.TemporaryDirectory() as directory, preview_context(directory):
            state = preview_state()
            state.client_usage['date'] = monitor.today_key()
            monitor.update_usage_history(state)
            destination = Path(directory) / production_path.name
            self.assertEqual(monitor.USAGE_HISTORY_JSON, destination)
            self.assertTrue(destination.exists())
            saved = json.loads(destination.read_text(encoding='utf-8'))
            self.assertEqual(saved['days'][monitor.today_key()]['tokens'], state.today_tokens)
        self.assertEqual(monitor.USAGE_HISTORY_JSON, production_path)

    def test_unknown_prices_are_distinct_from_zero_cost(self):
        ui = WorkspaceUI.__new__(WorkspaceUI)
        ui.m = monitor
        self.assertEqual(ui.cost_text({'cost': 0}), monitor.money(0))
        self.assertEqual(ui.cost_text({'cost': 0, 'unpriced_tokens': 100}), '未定价')
        self.assertEqual(ui.cost_text({'cost': 2, 'unpriced_tokens': 100}), monitor.money(2) + ' +')

    def test_cycle_selector_uses_cycle_usage_and_recovers_when_removed(self):
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = preview_state()
        app._account_range = 'cycle'
        app._scroll_offsets = {'accounts': 92}
        app._filter_account_display_rows = lambda rows: rows
        rows, key, _label = app._account_rows_for_range()
        self.assertEqual(key, 'window_cycle')
        self.assertEqual([row['tokens'] for row in rows], [921000])
        app.state.top_accounts[0].pop('window_cycle')
        app._account_rows_for_range()
        self.assertEqual(app._account_range, 'today')
        self.assertEqual(app._scroll_offsets['accounts'], 0)


@unittest.skipUnless(os.name == 'nt', 'Windows UI smoke test')
class OrbitWindowTests(unittest.TestCase):
    @unittest.skipIf(monitor.Image is None, 'Pillow is optional')
    def test_flat_dock_pixels_do_not_flash_during_hover_animation(self):
        with tempfile.TemporaryDirectory() as directory, preview_context(directory), \
             patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '1',
                                     'TOKEN_MONITOR_REFRACTION': '0', 'TOKEN_MONITOR_REDUCE_MOTION': '0'}), \
             patch.object(WorkspaceUI, '_prefers_reduced_motion', return_value=False), \
             patch.object(monitor, 'load_usage_history', return_value={'days': {}}):
            app = PreviewApp()
            try:
                app.state = preview_state()
                app._filter_account_display_rows = lambda rows: rows
                ui = app._workspace_ui
                ui.material_name = 'alpha'
                app._draw()
                app.root.update()
                native = app._desktop_compositor
                if native is None or not native.active:
                    self.skipTest('Native desktop composition unavailable')
                native.present()
                box = (24, app.HEIGHT - 74, app.WIDTH - 24, app.HEIGHT - 24)
                before = native.snapshot_rgb(ui.BG).crop(box).tobytes()
                with patch.object(ui, '_request_animation'):
                    for direction in (1., -1., 1.):
                        ui._light_target = direction
                        ui._last_glass_frame = 0.
                        ui._animate()
                        native.present()
                        after = native.snapshot_rgb(ui.BG).crop(box).tobytes()
                        self.assertEqual(before, after, 'Hover background redraw obscures navigation')
            finally:
                app.close_app()

    def test_nav_background_stays_below_buttons_during_light_animation(self):
        for material, pillow in (('alpha', True), ('opaque', True), ('liquid', True), ('liquid', False)):
            with self.subTest(material=material, pillow=pillow), \
                 tempfile.TemporaryDirectory() as directory, preview_context(directory), \
                 patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '0',
                                         'TOKEN_MONITOR_REDUCE_MOTION': '0'}), \
                 patch.object(monitor, 'Image', monitor.Image if pillow else None), \
                 patch.object(WorkspaceUI, '_prefers_reduced_motion', return_value=False), \
                 patch.object(monitor, 'load_usage_history', return_value={'days': {}}):
                app = PreviewApp()
                try:
                    app.state = preview_state()
                    app._filter_account_display_rows = lambda rows: rows
                    ui = app._workspace_ui
                    ui.material_name = material
                    app._draw()
                    foreground = app.canvas.find_withtag('nav_foreground')
                    initial_count = len(app.canvas.find_all())
                    for tick in range(4):
                        ui._light_target = 1. if tick % 2 == 0 else -1.
                        ui._last_glass_frame = 0.
                        if ui._animation_id is not None:
                            app.root.after_cancel(ui._animation_id)
                        ui._animate()
                        stack = list(app.canvas.find_all())
                        background = app.canvas.find_withtag('liquid_dock')
                        self.assertTrue(background)
                        self.assertLess(max(stack.index(item) for item in background),
                                        min(stack.index(item) for item in foreground))
                        selection = app.canvas.find_withtag('nav_selection')
                        self.assertLess(max(stack.index(item) for item in background),
                                        min(stack.index(item) for item in selection))
                        self.assertEqual(len(stack), initial_count)
                    for page in ('main_stats', 'main_library', 'main_accounts'):
                        ui.handle_button(page)
                        if ui._animation_id is not None:
                            app.root.after_cancel(ui._animation_id)
                        ui._last_glass_frame = 0.
                        ui._animate()
                        stack = list(app.canvas.find_all())
                        background = app.canvas.find_withtag('liquid_dock')
                        foreground = app.canvas.find_withtag('nav_foreground')
                        selection = app.canvas.find_withtag('nav_selection')
                        self.assertLess(max(stack.index(item) for item in background),
                                        min(stack.index(item) for item in selection))
                        self.assertLess(max(stack.index(item) for item in selection),
                                        min(stack.index(item) for item in foreground))
                finally:
                    app.close_app()

    def test_real_event_handler_updates_orbit_flow_and_both_delta_badges(self):
        with tempfile.TemporaryDirectory() as directory, preview_context(directory), \
             patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '0',
                                     'TOKEN_MONITOR_REDUCE_MOTION': '1'}), \
             patch.object(monitor, '_current_codex_account_label', return_value='Codex local - demo@example.test'), \
             patch.object(monitor, 'load_usage_history', return_value={'days': {}}), \
             patch.object(monitor, 'append_attribution_diagnostic', return_value=True):
            app = PreviewApp()
            try:
                app.state = preview_state()
                app._filter_account_display_rows = lambda rows: rows
                event = dict(event_id='orbit-live-event', session_id='orbit-live-session',
                             when=monitor.datetime.now(monitor.timezone.utc),
                             total_tokens=12345, input_tokens=12000, cached_tokens=10000,
                             output_tokens=345, model='gpt-5.6-sol', cost=.25)
                before = app.state.today_tokens
                self.assertTrue(app._record_live_usage_events([event]))
                self.assertEqual(app.state.today_tokens, before + 12345)
                self.assertEqual(app.state.latest_request['provider'], 'Codex local - demo@example.test')
                self.assertFalse(app._record_live_usage_events([event]))
                self.assertEqual(app.state.today_tokens, before + 12345)
                app._draw()
                self.assertEqual(app.canvas.itemcget('orbit_recent_tokens', 'text'), monitor.compact_number(12345))
                for tab in ('main_accounts', 'main_stats'):
                    app._workspace_ui.handle_button(tab)
                    self.assertTrue(app._redraw_token_delta_badge())
                    self.assertTrue(app._redraw_cost_delta_badge())
                    self.assertEqual(app.canvas.itemcget('token_delta_badge', 'text'), '+12,345')
                    self.assertEqual(app.canvas.itemcget('cost_delta_badge', 'text'), '+$0.25')
                future = time.monotonic() + monitor.TOKEN_DELTA_BADGE_DURATION_SECONDS + 1
                with patch.object(time, 'monotonic', return_value=future):
                    app._redraw_token_delta_badge()
                    app._redraw_cost_delta_badge()
                    self.assertEqual(app.canvas.itemcget('token_delta_badge', 'state'), 'hidden')
                    self.assertEqual(app.canvas.itemcget('cost_delta_badge', 'state'), 'hidden')
                    self.assertEqual(app._token_flow_snapshot()[1], 0)
            finally:
                app.close_app()

    @unittest.skipIf(monitor.Image is None, 'Pillow is optional')
    def test_motion_toggle_changes_preview_and_stops_all_lens_feedback(self):
        with tempfile.TemporaryDirectory() as directory, preview_context(directory), \
             patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '0',
                                     'TOKEN_MONITOR_REDUCE_MOTION': '0'}), \
             patch.object(WorkspaceUI, '_prefers_reduced_motion', return_value=False):
            app = PreviewApp()
            try:
                app.root.update()
                ui = app._workspace_ui
                # Inspect the native lens geometry without depending on a CI GPU.
                # Pixel changes are covered by the real shader tests.
                ui.desktop = SimpleNamespace(active=True, refraction=True, lenses={}, pointer=(0., 0.))
                ui.handle_button('appearance')
                ui.handle_button('appearance_motion')
                self.assertTrue(ui.reduced_motion)

                def picture():
                    rect = ui.desktop.lenses['demo']
                    return SimpleNamespace(width=rect[2], height=rect[3], size=rect[2:4],
                                           tobytes=lambda: rect)

                def advance(now):
                    if ui._animation_id is not None:
                        app.root.after_cancel(ui._animation_id)
                    with patch('monitor_ui.time.monotonic', return_value=now):
                        ui._animate()

                px, py = ui._demo_position
                still = picture()
                self.assertTrue(ui.pointer_press(px, py))
                self.assertEqual(picture().tobytes(), still.tobytes())
                self.assertTrue(ui.pointer_drag(px + 18, py))
                self.assertEqual(ui._demo_position, (px + 18, py))
                dragged = picture()
                ui.pointer_release()
                self.assertEqual(picture().tobytes(), dragged.tobytes())
                self.assertIsNone(ui._animation_id)

                # Enabling previews a visible release without requiring a hover-capable device.
                with patch('monitor_ui.time.monotonic', return_value=100.):
                    ui.handle_button('appearance_motion')
                self.assertFalse(ui.reduced_motion)
                self.assertGreaterEqual(picture().width - still.width, 10)
                self.assertLess(picture().height, still.height)
                self.assertIsNotNone(ui._animation_id)
                advance(100.12)
                self.assertLess(picture().width, still.width)
                advance(100.6)
                self.assertEqual(picture().size, still.size)
                self.assertIsNone(ui._animation_id)

                # Lighting actually changes the displayed pixels while the lens stays in place.
                for i in range(16):
                    ui.pointer_motion(ui._demo_rect[0], py)
                    advance(101. + i * .04)
                left_light = ui.desktop.pointer
                for i in range(16):
                    ui.pointer_motion(ui._demo_rect[2], py)
                    advance(102. + i * .04)
                self.assertNotEqual(ui.desktop.pointer, left_light)

                with patch('monitor_ui.time.monotonic', return_value=104.):
                    ui.pointer_press(*ui._demo_position)
                    ui.pointer_release()
                self.assertGreater(picture().width, still.width)
                timer = ui._animation_id
                ui.handle_button('appearance_motion')
                self.assertEqual(picture().size, still.size)
                self.assertIsNone(ui._animation_id)
                self.assertNotIn(timer, app.root.tk.splitlist(app.root.tk.call('after', 'info')))
                disabled = picture().tobytes()
                ui.pointer_motion(ui._demo_rect[0], py)
                ui.pointer_press(*ui._demo_position)
                ui.pointer_release()
                advance(105.)
                self.assertEqual(picture().tobytes(), disabled)
                self.assertIsNone(ui._animation_id)
                settings = json.loads(Path(directory, 'monitor_appearance.json').read_text(encoding='utf-8'))
                self.assertEqual(settings, {'theme': ui.theme_name, 'motion': False, 'material': ui.material_name,
                                            'blur': 4., 'refraction': 1.})

                # Windows accessibility settings also disable the enable-preview pulse.
                with patch.object(WorkspaceUI, '_prefers_reduced_motion', return_value=True):
                    ui.handle_button('appearance_motion')
                self.assertTrue(ui.reduced_motion)
                self.assertEqual(picture().tobytes(), disabled)
                self.assertIsNone(ui._animation_id)
            finally:
                app._workspace_ui.desktop = None
                app.close_app()

    @unittest.skipIf(monitor.Image is None, 'Pillow is optional')
    def test_tooltip_panel_erases_earlier_ink_in_native_layers(self):
        with tempfile.TemporaryDirectory() as directory, preview_context(directory), \
             patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '1',
                                     'TOKEN_MONITOR_REFRACTION': '0', 'TOKEN_MONITOR_REDUCE_MOTION': '1'}):
            app = PreviewApp()
            try:
                native = app._desktop_compositor
                if native is None:
                    self.skipTest('Native desktop composition unavailable')
                app.root.geometry('390x760+60+60')
                app.root.update()
                canvas = app.canvas
                canvas.delete('all')
                canvas.create_text(160, 200, text='MMMM', fill='white', font=('Segoe UI', -60))
                native.present()
                # Match present()'s guard: idle paints must not interrupt the two mattes.
                with patch.object(native, '_painting', True):
                    _base, before = native._capture_layers()
                area = (110, 190, 210, 220)
                self.assertGreater(before.getchannel('A').crop(area).getextrema()[1], 0)
                canvas.create_rectangle(80, 150, 240, 250, fill='#26304A', outline='',
                                        tags=('refraction_occluder',))
                canvas.create_text(90, 158, anchor='nw', text='TIP', fill='white', font=('Segoe UI', -12))
                with patch.object(native, '_painting', True):
                    _base, after = native._capture_layers()
                self.assertEqual(after.getchannel('A').crop(area).getextrema(), (0, 0))
                self.assertGreater(after.getchannel('A').crop((85, 155, 120, 175)).getextrema()[1], 0)
            finally:
                app.close_app()

    def test_views_navigation_and_dependency_free_fallback(self):
        try:
            probe = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk desktop unavailable: {exc}')
        else:
            probe.update()
            probe.destroy()
        # Force the actual Canvas fallback even on machines with Pillow/GPU.
        with tempfile.TemporaryDirectory() as directory, preview_context(directory), \
             patch.dict(os.environ, {'TOKEN_MONITOR_UI': 'orbit', 'TOKEN_MONITOR_DESKTOP_GLASS': '0',
                                     'TOKEN_MONITOR_REDUCE_MOTION': '1'}), \
             patch.object(monitor, 'Image', None):
            app = PreviewApp()
            errors = []
            app.root.report_callback_exception = lambda *exc: errors.append(exc)
            try:
                self.assertIsNone(app._workspace_ui.glass)
                self.assertIsNone(app._desktop_compositor if hasattr(app, '_desktop_compositor') else None)
                app.state = preview_state()
                app._filter_account_display_rows = lambda rows: rows
                for width, height in ((390, 760), (360, 640)):
                    app._apply_window_size(width, height)
                    app.root.update()
                    for name in ('main_accounts', 'main_stats', 'main_library', 'appearance'):
                        with self.subTest(size=(width, height), view=name):
                            app._workspace_ui.handle_button(name)
                            app.root.update()
                            self.assertIn('btn_close', app._btn_rects)
                            self.assertIn('main_stats', app._btn_rects)
                            for item in app.canvas.find_all():
                                if app.canvas.type(item) != 'text' or app.canvas.itemcget(item, 'state') == 'hidden':
                                    continue
                                bounds = app.canvas.bbox(item)
                                self.assertGreaterEqual(bounds[0], 0)
                                self.assertGreaterEqual(bounds[1], 0)
                                self.assertLessEqual(bounds[2], width)
                                self.assertLessEqual(bounds[3], height)
                    app._workspace_ui.handle_button('appearance_done')
                app._workspace_ui.handle_button('main_library')
                self.assertIn('rank_cycle', app._btn_rects)
                app._on_press(SimpleNamespace(
                    x=sum(app._btn_rects['rank_cycle'][::2]) / 2,
                    y=sum(app._btn_rects['rank_cycle'][1::2]) / 2))
                self.assertEqual(app._account_range, 'cycle')
                self.assertFalse(errors, errors)
            finally:
                app.close_app()


if __name__ == '__main__':
    unittest.main()
