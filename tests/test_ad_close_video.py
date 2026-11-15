"""Check reward-dialog detection and actions from an offline LDPlayer frame."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import autoplay
from autoplay import Player, Screen, Vision
from emulator import EmulatorTarget


FIXTURE = Path(__file__).parent / "fixtures" / "ad_close_video_dialog.png"


class CloseVideoVisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Image.open(FIXTURE) as image:
            cls.frame = image.convert("RGB")
        cls.vision = Vision(upgrade_tower=False)

    def assert_video_action(self, screen, name, point):
        self.assertEqual(screen.name, name)
        self.assertIsNotNone(screen.point)
        x, y = screen.point
        expected_x, expected_y = point
        self.assertLessEqual(abs(x - expected_x), 8)
        self.assertLessEqual(abs(y - expected_y), 8)
        self.assertFalse(screen.raw_point)
        self.assertIsNone(screen.keyevent)

    def test_visible_reward_selects_close_video_without_ui_hierarchy(self):
        for state in ("watching_ad", "ad_foreground"):
            with self.subTest(state=state), patch.object(self.vision.ad_closer, "inspect", return_value=None) as inspect:
                screen = self.vision.classify(self.frame, **{state: True})
                self.assert_video_action(screen, "ad_close_video", (570, 445))
                inspect.assert_not_called()

    def test_unverified_reward_selects_resume_video(self):
        without_badge = self.frame.copy()
        without_badge.paste((36, 75, 82), (1035, 0, 1280, 95))
        with patch.object(self.vision.ad_closer, "inspect", return_value=None) as inspect:
            screen = self.vision.classify(without_badge, watching_ad=True)
            self.assert_video_action(screen, "ad_resume", (762, 446))
            inspect.assert_not_called()

    def test_dialog_buttons_do_not_close_outside_ad_context(self):
        screen = self.vision.classify(self.frame)
        self.assertNotIn(screen.name, {"ad_close_video", "ad_resume", "reward_granted"})

    def test_single_button_does_not_verify_the_dialog(self):
        incomplete = self.frame.copy()
        incomplete.paste((255, 255, 255), (490, 416, 650, 477))
        screen = self.vision.classify(incomplete, watching_ad=True)
        self.assertNotIn(screen.name, {"ad_close_video", "ad_resume"})

    def test_close_point_moves_with_the_dialog(self):
        left, top, right, bottom = 385, 205, 890, 520
        dialog = self.frame.crop((left, top, right, bottom))
        shifted = self.frame.copy()
        shifted.paste(self.frame.getpixel((0, 719)), (left, top, right, bottom))
        shifted.paste(dialog, (left + 12, top - 10))
        screen = self.vision.classify(shifted, ad_foreground=True)
        self.assert_video_action(screen, "ad_close_video", (582, 435))

    def test_live_ad_tap_scales_to_the_original_frame(self):
        raw_frame = self.frame.resize((1600, 900), Image.Resampling.BILINEAR)
        frames = [(0, raw_frame, None), (0.25, raw_frame, None)]
        foreground = ("com.raongames.growcastle", "com.google.android.gms.ads.AdActivity")
        target = EmulatorTarget(emulator="ldplayer", adb=Path("adb"), serial="device",
                                instance="test", display_name="test")
        args = SimpleNamespace(mode="wave", tap=True, video=None, serial="device", seconds=5,
                               skip=None, ads=True, auto_captcha=False, humanize=False,
                               upgrade_tower=None, upgrade_hero=None,
                               upgrade_gems=None, upgrade_levels=None)
        with patch.object(autoplay, "ensure_adb_connected", return_value=target.serial), \
                patch.object(autoplay, "adb_frames", return_value=iter(frames)), \
                patch.object(autoplay, "foreground_window", return_value=foreground), \
                patch.object(autoplay, "adb_run") as adb, patch("builtins.print"):
            autoplay.play(args, target, Mock(), None)

        adb.assert_called_once()
        adb_path, serial, shell, command, tap, x, y = adb.call_args.args
        self.assertEqual((adb_path, serial, shell, command, tap),
                         (target.adb, target.serial, "shell", "input", "tap"))
        self.assertLessEqual(abs(int(x) - 712), 10)
        self.assertLessEqual(abs(int(y) - 556), 10)


class CloseVideoPlayerTests(unittest.TestCase):
    def test_close_confirms_reward_on_return_to_the_game(self):
        report = Mock()
        player = Player("wave", report)
        dialog = Screen("ad_close_video", (570, 445))
        self.assertIsNone(player.feed(0, dialog))
        action = player.feed(0.21, dialog)
        self.assertIsNotNone(action)
        self.assertEqual((action.name, action.point), ("close video", (570, 445)))
        self.assertIsNone(player.feed(0.5, dialog))

        home = Screen("home")
        player.feed(1.0, home)
        player.feed(1.6, home)
        self.assertTrue(any("Reward granted, returned to game" in call.args[1]
                            for call in report.call_args_list))
        self.assertIsNone(player.ad)

    def test_resume_waits_five_seconds_before_retry(self):
        player = Player("wave", Mock())
        dialog = Screen("ad_resume", (762, 446))
        self.assertIsNone(player.feed(0, dialog))
        action = player.feed(0.21, dialog)
        self.assertIsNotNone(action)
        self.assertEqual((action.name, action.point), ("resume ad for reward", (762, 446)))
        self.assertIsNone(player.feed(0.5, dialog))
        self.assertIsNone(player.feed(5.1, dialog))
        retry = player.feed(5.3, dialog)
        self.assertIsNotNone(retry)
        self.assertEqual(retry.name, "resume ad for reward")


if __name__ == "__main__":
    unittest.main()
