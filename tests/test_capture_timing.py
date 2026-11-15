"""Timing tests for live ADB frame capture."""

from io import BytesIO
import struct
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image

import autoplay


class CaptureTimingTests(unittest.TestCase):
    @staticmethod
    def png_frame():
        buffer = BytesIO()
        Image.new("RGB", (16, 9)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_target_interval_accounts_for_time_already_spent(self):
        png = self.png_frame()
        for humanize in (False, True):
            for spent, expected in ((0.05, 0.13), (0.25, 0)):
                with self.subTest(humanize=humanize, spent=spent), \
                        patch.object(autoplay, "monotonic", side_effect=[10.0, 10.0, 10.0 + spent, 10.3]), \
                        patch.object(autoplay, "sleep") as sleep, \
                        patch.object(autoplay, "adb_run", return_value=png) as adb:
                    frames = autoplay.adb_frames("adb", "device", 0, humanize=humanize)
                    first = next(frames)
                    second = next(frames)
                    frames.close()
                    self.assertEqual(first[0], 0)
                    self.assertAlmostEqual(second[0], 0.3)
                    self.assertEqual(adb.call_count, 2)
                    self.assertAlmostEqual(sleep.call_args.args[0], expected)

    def test_raw_capture_reads_android_headers_and_discards_alpha(self):
        pixel = bytes((1, 23, 45, 67))
        for extended_header in (b"", b"\0" * 4):
            with self.subTest(header_length=12 + len(extended_header)), \
                    patch.object(autoplay, "adb_run", return_value=struct.pack("<III", 16, 9, 1)
                                 + extended_header + pixel * 16 * 9):
                screen = autoplay.capture_raw_screen("adb", "device")
                self.assertEqual(screen.size, (16, 9))
                self.assertEqual(screen.getpixel((5, 4)), (1, 23, 45))

    def test_battle_capture_falls_back_to_png_with_fast_timing(self):
        png = self.png_frame()
        focus_worker = Mock()
        focus = focus_worker.submit.return_value
        with patch.object(autoplay, "monotonic", side_effect=[10.0, 10.0, 10.05, 10.15]), \
                patch.object(autoplay, "sleep") as sleep, \
                patch.object(autoplay, "adb_run", side_effect=[b"unsupported", png, png]) as adb:
            frames = autoplay.adb_frames("adb", "device", 0, fast_battle=lambda: True,
                                          focus_worker=focus_worker)
            _, first_image, first_focus = next(frames)
            _, second_image, second_focus = next(frames)
            self.assertEqual(first_image.size, (16, 9))
            self.assertEqual(second_image.size, (16, 9))
            self.assertIsNone(first_focus)
            self.assertIs(second_focus, focus)
            focus.cancel.assert_called_once()
            frames.close()
            self.assertEqual(adb.call_args_list, [
                call("adb", "device", "exec-out", "screencap", timeout=2),
                call("adb", "device", "exec-out", "screencap", "-p"),
                call("adb", "device", "exec-out", "screencap", "-p"),
            ])
            self.assertAlmostEqual(sleep.call_args.args[0], 0.1)

    def test_battle_stream_uses_native_size_and_closes_when_battle_ends(self):
        png = self.png_frame()
        battle_capture = autoplay.BattleCapture()
        stream_frame = Image.new("RGB", (16, 9), "green")
        with patch.object(autoplay, "sleep"), \
                patch.object(autoplay, "adb_run", return_value=png) as adb, \
                patch.object(autoplay, "BattleFrameStream") as stream, \
                patch.object(autoplay, "capture_raw_screen") as raw:
            stream.return_value.next_frame.return_value = (20.25, stream_frame)
            frames = autoplay.adb_frames(
                "adb", "device", 0, start=20.0, battle_capture=battle_capture,
                fast_battle=Mock(side_effect=[False, True, False]),
            )
            self.addCleanup(frames.close)
            self.addCleanup(battle_capture.close)
            next(frames)
            self.assertEqual(next(frames), (0.25, stream_frame, None))
            next(frames)
            stream.assert_called_once_with("adb", "device", (16, 9))
            stream.return_value.close.assert_called_once()
            raw.assert_not_called()
            self.assertEqual(adb.call_count, 2)

    def test_unsupported_battle_stream_falls_back_once_for_the_session(self):
        png = self.png_frame()
        battle_capture = autoplay.BattleCapture()
        raw_frame = Image.new("RGB", (16, 9), "green")
        with patch.object(autoplay, "sleep"), \
                patch.object(autoplay, "adb_run", return_value=png), \
                patch.object(autoplay, "BattleFrameStream") as stream, \
                patch.object(autoplay, "capture_raw_screen", return_value=raw_frame):
            stream.return_value.next_frame.return_value = None
            frames = autoplay.adb_frames(
                "adb", "device", 0, battle_capture=battle_capture,
                fast_battle=Mock(side_effect=[False, True, True]),
            )
            self.addCleanup(frames.close)
            self.addCleanup(battle_capture.close)
            next(frames)
            self.assertIs(next(frames)[1], raw_frame)
            self.assertIs(next(frames)[1], raw_frame)
            stream.assert_called_once()
            stream.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
