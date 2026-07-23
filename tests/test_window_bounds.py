import unittest

import monitor


class WindowBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.area = monitor.WorkArea(0, 0, 1536, 816)

    def test_position_inside_work_area_is_unchanged(self) -> None:
        self.assertEqual(
            monitor.constrain_window_position(1120, 70, 390, self.area),
            (1120, 70),
        )

    def test_only_recovery_width_must_remain_visible(self) -> None:
        self.assertEqual(
            monitor.constrain_window_position(1500, 70, 390, self.area),
            (1456, 70),
        )
        self.assertEqual(
            monitor.constrain_window_position(-500, 70, 390, self.area),
            (-310, 70),
        )

    def test_top_recovery_strip_stays_available(self) -> None:
        self.assertEqual(
            monitor.constrain_window_position(100, -50, 390, self.area),
            (100, 0),
        )
        self.assertEqual(
            monitor.constrain_window_position(100, 900, 390, self.area),
            (100, 784),
        )

    def test_negative_coordinate_monitor_is_supported(self) -> None:
        left_monitor = monitor.WorkArea(-1920, 0, 0, 1040)
        self.assertEqual(
            monitor.constrain_window_position(-2100, 100, 390, left_monitor),
            (-2100, 100),
        )
        self.assertEqual(
            monitor.format_tk_geometry(390, 760, -2100, 100),
            "390x760+-2100+100",
        )

    def test_small_work_area_uses_the_available_size(self) -> None:
        tiny = monitor.WorkArea(10, 20, 60, 40)
        self.assertEqual(
            monitor.constrain_window_position(999, 999, 390, tiny),
            (10, 20),
        )


if __name__ == "__main__":
    unittest.main()
