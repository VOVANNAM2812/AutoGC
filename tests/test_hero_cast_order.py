"""Hero detection, battle gating, latches, priority, and tap safety."""

from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
import unittest

import numpy as np
from PIL import Image

import autoplay
from autoplay import HERO_SKILLS, Player, Screen, Vision, detect_hero_skills, viewport
from emulator import EmulatorTarget


class HeroDetectionTests(unittest.TestCase):
    def setUp(self):
        self.rgb = np.zeros((720, 1280, 3), dtype=np.uint8)

    def bar(self, hero, color=(10, 200, 220), count=245):
        _, x, y, _ = HERO_SKILLS[hero]
        region = self.rgb[y - 2:y + 3, x - 24:x + 25]
        region[:] = 0
        for i in range(count):
            region[i // 49, i % 49] = color

    def test_coordinates_and_detection_in_current_frame(self):
        expected = [(x, y, tap_y) for y, tap_y in ((146, 184), (241, 277), (331, 370))
                    for x in (293, 373, 453)]
        self.assertEqual([(x, y, tap_y) for _, x, y, tap_y in HERO_SKILLS], expected)
        for hero in range(9):
            self.bar(hero)
        self.assertEqual(detect_hero_skills(self.rgb), list(range(9)))
        self.bar(4, count=0)
        self.assertEqual(detect_hero_skills(self.rgb), [0, 1, 2, 3, 5, 6, 7, 8])

    def test_92_percent_threshold_and_signed_arithmetic(self):
        self.bar(0, count=225)  # 225 / 245 < 0.92
        self.bar(1, count=226)  # 226 / 245 >= 0.92
        self.bar(2, color=(250, 220, 200))  # unsigned subtraction would wrap
        self.bar(3, color=(0, 106, 165))  # blue-red >65 and green-red >50
        self.bar(4, color=(0, 105, 220))  # strict green threshold
        self.bar(5, color=(0, 200, 164))  # strict blue threshold
        self.bar(6, color=(150, 210, 215))  # blue-red == 65
        self.bar(7, color=(100, 150, 200))  # green-red == 50
        self.assertEqual(detect_hero_skills(self.rgb), [1, 3])

    def test_viewport_tolerance_and_size(self):
        self.assertEqual(viewport(Image.new("RGB", (640, 360))).size, (1280, 720))
        self.assertEqual(viewport(Image.new("RGB", (1820, 1000))).size, (1280, 720))
        self.assertIsNone(viewport(Image.new("RGB", (1850, 1000))))
        self.assertIsNone(viewport(Image.new("RGB", (900, 600))))

    def test_classification_requires_hud_speed_and_wave_or_boss(self):
        vision = Vision(False, track_boss=True)
        self.bar(0)
        for hud, speed, wave, boss, expected in (
            (True, True, True, False, "battle"),
            (True, True, False, True, "battle"),
            (True, True, True, True, "battle"),
            (False, True, True, False, "unknown"),
            (True, False, True, False, "unknown"),
            (True, True, False, False, "unknown"),
        ):
            with self.subTest(hud=hud, speed=speed, wave=wave, boss=boss), \
                    patch.object(vision, "matches", side_effect=lambda rgb, name, *args:
                                 {"hud": hud, "speed": speed, "wave": wave}.get(name, False)), \
                    patch.object(vision, "matches_boss", return_value=boss), \
                    patch.object(vision, "hp_empty", return_value=False), \
                    patch.object(vision, "find_mimic", return_value=None), \
                    patch.object(vision, "smith_skill_ready", return_value=False), \
                    patch.object(vision, "castle_hp_low", return_value=False):
                screen = vision._classify_game_screen(self.rgb.astype(np.int16))
                self.assertEqual(screen.name, expected)
                if expected == "battle":
                    self.assertEqual(screen.ready, (2,))
                    self.assertEqual(screen.boss_visible, boss)


class HeroDecisionTests(unittest.TestCase):
    def make_player(self, humanize=False, *, boss_skill=None):
        return Player("wave", lambda *args: None, watch_ads=False, humanize=humanize, boss_skill=boss_skill)

    def test_each_decision_scans_from_hero_one_and_preserves_priority(self):
        player = self.make_player()
        self.assertEqual(player.cast(0, (5,)).name, "cast skill 4")
        self.assertEqual(player.cast(1, (0, 2, 6)).name, "cast archer skill 1")
        # Hero 1 becomes ready after the Archer, so it wins over Hero 5.
        self.assertEqual(player.cast(2, (2, 6)).name, "cast skill 1")
        self.assertEqual(player.cast(3, (1, 6), smith_ready=True, hp_low=True).name,
                         "cast Smith (HP < 50%)")
        self.assertEqual(player.cast(4, (1, 6), smith_ready=True, hp_low=True).name,
                         "cast archer skill 2")
        self.assertEqual(player.cast(5, (6,)).name, "cast skill 5")

    def test_shared_015_second_interval_for_smith_archer_and_heroes(self):
        for humanize in (False, True):
            with self.subTest(humanize=humanize):
                player = self.make_player(humanize)
                ready = (0, 1, 2, 3)
                self.assertEqual(player.cast(0, ready, smith_ready=True, hp_low=True).name,
                                 "cast Smith (HP < 50%)")
                self.assertIsNone(player.cast(0.149, ready, smith_ready=True, hp_low=True))
                self.assertEqual(player.cast(0.15, ready, smith_ready=True, hp_low=True).name,
                                 "cast archer skill 1")
                self.assertIsNone(player.cast(0.29, ready, smith_ready=True, hp_low=True))
                self.assertEqual(player.cast(0.31, ready, smith_ready=True, hp_low=True).name,
                                 "cast archer skill 2")
                self.assertIsNone(player.cast(0.44, ready, smith_ready=True, hp_low=True))
                self.assertEqual(player.cast(0.47, ready, smith_ready=True, hp_low=True).name,
                                 "cast skill 1")
                self.assertIsNone(player.cast(0.61, ready, smith_ready=True, hp_low=True))
                self.assertEqual(player.cast(0.63, ready, smith_ready=True, hp_low=True).name,
                                 "cast skill 2")

    def test_reserved_hero_waits_for_boss_and_rearms_after_cooldown(self):
        player = self.make_player(boss_skill=6)
        self.assertIsNone(player.cast(0, (6,)))
        self.assertEqual(player.cast(1, (0, 2, 6), boss_visible=True).name, "cast archer skill 1")
        self.assertEqual(player.cast(2, (2, 6), boss_visible=True).name, "cast skill 5")
        self.assertEqual(player.cast(3, (2, 6), boss_visible=True).name, "cast skill 1")
        self.assertIsNone(player.cast(4, (), boss_visible=True))
        self.assertIsNone(player.cast(5, (), boss_visible=True))
        self.assertIsNone(player.cast(6, (6,)))
        self.assertEqual(player.cast(7, (6,), boss_visible=True).name, "cast skill 5")

    def test_mimic_collection_uses_separate_cooldown(self):
        player = self.make_player()
        frame = Screen("battle", ready=(2,), mimic=(300, 400))
        player.feed(0, frame)
        self.assertEqual(player.feed(0.5, frame).name, "collect Mimic chest")
        self.assertIsNone(player.feed(0.79, frame))
        self.assertEqual(player.feed(0.81, frame).name, "cast skill 1")

    def test_latch_requires_two_consecutive_non_full_battle_frames(self):
        player = self.make_player()
        self.assertEqual(player.cast(0, (2,)).name, "cast skill 1")
        self.assertIsNone(player.cast(0.1, ()))
        self.assertIsNone(player.cast(0.2, (2,)))  # full resets missing streak
        self.assertIsNone(player.cast(0.3, ()))
        self.assertIsNone(player.cast(0.4, (2,)))
        self.assertIsNone(player.cast(0.5, ()))
        self.assertIsNone(player.cast(0.6, ()))
        self.assertEqual(player.cast(1, (2,)).name, "cast skill 1")

    def test_frame_started_before_tap_ack_does_not_rearm_that_skill(self):
        player = self.make_player()
        player.feed(0, Screen("battle", ready=(2, 3)))
        self.assertEqual(player.feed(0.5, Screen("battle", ready=(2, 3))).slot, 2)
        self.assertEqual(player.feed(0.7, Screen("battle", ready=(3,)), ignore_skill=2).slot, 3)
        self.assertEqual(player.skills_used[2], 0)
        player.feed(0.8, Screen("battle"))
        self.assertEqual(player.skills_used[2], 1)
        self.assertIsNone(player.feed(1, Screen("battle", ready=(2,))))
        player.feed(1.1, Screen("battle"))
        player.feed(1.2, Screen("battle"))
        self.assertEqual(player.feed(1.4, Screen("battle", ready=(2,))).slot, 2)

    def test_interrupted_or_invalid_frames_do_not_complete_latch_streak(self):
        for interruption in ("skip", "captcha", "invalid"):
            with self.subTest(interruption=interruption):
                player = self.make_player()
                player.feed(0, Screen("battle", ready=(2,)))
                self.assertEqual(player.feed(0.5, Screen("battle", ready=(2,))).name, "cast skill 1")
                player.feed(0.6, Screen("battle", ready=()))
                if interruption == "invalid":
                    player.invalidate_frame(0.7)
                else:
                    player.feed(0.7, Screen(interruption))
                player.feed(1, Screen("battle", ready=()))
                self.assertIn(2, player.skills_used)  # one observed missing frame
                player.feed(1.1, Screen("battle", ready=(2,)))
                self.assertIn(2, player.skills_used)  # full breaks the streak
                self.assertIsNone(player.feed(1.5, Screen("battle", ready=(2,))))
                player.feed(1.6, Screen("battle", ready=()))
                player.feed(1.7, Screen("battle", ready=()))
                self.assertEqual(player.feed(1.8, Screen("battle", ready=(2,))).name, "cast skill 1")

    def test_entry_delay_cooldown_and_latch_reset(self):
        for humanize in (False, True):
            with self.subTest(humanize=humanize):
                player = self.make_player(humanize)
                battle = Screen("battle", ready=(2, 3))
                self.assertIsNone(player.feed(0, battle))
                self.assertIsNone(player.feed(0.44, battle))
                self.assertEqual(player.feed(0.45, battle).name, "cast skill 1")
                self.assertIsNone(player.feed(0.59, battle))
                self.assertEqual(player.feed(0.61, battle).name, "cast skill 2")
                player.feed(1, Screen("victory"))
                self.assertEqual(player.skills_used, {})
                player.feed(2, Screen("home"))
                player.feed(4, Screen("home"))  # starts a new battle
                self.assertEqual(player.skills_used, {})
                player.feed(5, battle)
                self.assertEqual(player.feed(5.5, battle).name, "cast skill 1")


class HeroPlayTests(unittest.TestCase):
    def args(self, *, tap, video=None):
        return SimpleNamespace(mode="wave", skip=None, tap=tap, humanize=True,
                               upgrade_hero=None, upgrade_tower=None, upgrade_gems=None,
                               upgrade_levels=None, auto_captcha=False, ads=False,
                               video=video, serial="device", seconds=5)

    def target(self):
        return EmulatorTarget(emulator="ldplayer", adb=Path("adb"), serial="device",
                              instance="test", display_name="test")

    def test_real_hero_tap_scaled_foreground_checked_and_no_extra_capture(self):
        frame = Image.new("RGB", (1920, 1080))
        battle = Screen("battle", ready=(2,))
        with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                patch.object(autoplay, "adb_frames", return_value=iter([(0, frame, None), (.5, frame, None)])), \
                patch.object(autoplay, "foreground_window", return_value=("com.raongames.growcastle", "game")) as fg, \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "human_tap") as human, \
                patch.object(autoplay, "SkillTapShell") as shell, \
                patch.object(autoplay, "adb_run") as adb, \
                patch("builtins.print"):
            vision.return_value.classify.return_value = battle
            autoplay.play(self.args(tap=True), self.target(), lambda *args: None, None)
            self.assertEqual(fg.call_count, 3)  # once per frame and just before tap
            human.assert_not_called()
            adb.assert_not_called()
            shell.return_value.tap.assert_called_once_with(Path("adb"), "device", 440, 276)
            shell.return_value.close.assert_called_once()
            self.assertEqual(vision.return_value.classify.call_count, 2)

    def test_live_casts_enforce_wall_clock_spacing_after_archer(self):
        frame = Image.new("RGB", (1280, 720))
        battle = Screen("battle", ready=(0, 2))
        clock = SimpleNamespace(now=10.0)

        def acknowledge_tap(_adb, _serial, x, _y):
            if x == 135:
                clock.now = 10.1
            return True

        def sleep_for(seconds):
            clock.now += seconds

        with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                patch.object(autoplay, "adb_frames", return_value=iter([
                    (0, frame, None), (.5, frame, None), (.9, frame, None),
                ])), \
                patch.object(autoplay, "foreground_window", return_value=("com.raongames.growcastle", "game")), \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "human_tap") as human, \
                patch.object(autoplay, "SkillTapShell") as shell, \
                patch.object(autoplay, "monotonic", side_effect=lambda: clock.now), \
                patch.object(autoplay, "sleep", side_effect=sleep_for) as sleep, \
                patch("builtins.print"):
            vision.return_value.classify.return_value = battle
            shell.return_value.tap.side_effect = acknowledge_tap
            autoplay.play(self.args(tap=True), self.target(), lambda *args: None, None)
            human.assert_not_called()
            self.assertEqual(shell.return_value.tap.call_args_list,
                             [call(Path("adb"), "device", 135, 405), call(Path("adb"), "device", 293, 184)])
            self.assertAlmostEqual(sleep.call_args.args[0], 0.05)

    def test_tap_overlaps_next_capture_and_logs_actual_send_time(self):
        frame = Image.new("RGB", (1280, 720))
        battle = Screen("battle", ready=(2,))
        clock = SimpleNamespace(now=10.0)
        tap_started = Event()
        next_capture_started = Event()

        def capture(*_args, **_kwargs):
            yield 0, frame, None
            clock.now = 10.61
            yield 0.5, frame, None
            self.assertTrue(tap_started.wait(timeout=2), "No tap dispatched during capture")
            clock.now = 10.97
            next_capture_started.set()
            yield 0.9, frame, None

        def acknowledge_tap(*_args):
            tap_started.set()
            self.assertTrue(next_capture_started.wait(timeout=2), "Capture blocked on tap acknowledgment")
            return True

        report = Mock()
        with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                patch.object(autoplay, "adb_frames", side_effect=capture), \
                patch.object(autoplay, "foreground_window", return_value=("com.raongames.growcastle", "game")), \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "SkillTapShell") as shell, \
                patch.object(autoplay, "monotonic", side_effect=lambda: clock.now), \
                patch("builtins.print"):
            vision.return_value.classify.return_value = battle
            shell.return_value.tap.side_effect = acknowledge_tap
            autoplay.play(self.args(tap=True), self.target(), report, None)
            shell.return_value.tap.assert_called_once()
        [(send_time, message)] = [entry.args for entry in report.call_args_list if "TAP cast" in entry.args[1]]
        self.assertAlmostEqual(send_time, 0.61)
        self.assertEqual(message, "TAP cast skill 1 (293, 184)")

    def test_foreground_change_cancels_tap_and_allows_safe_retry(self):
        frame = Image.new("RGB", (1280, 720))
        battle = Screen("battle", ready=(2,))
        game = ("com.raongames.growcastle", "game")
        battle_focus = Mock()
        battle_focus.result.return_value = game
        fg_values = [game, ("other.app", "activity"), game, game, game]
        with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                patch.object(autoplay, "adb_frames", return_value=iter([
                    (0, frame, None), (.5, frame, battle_focus),
                    (1, frame, None), (1.5, frame, None), (2, frame, None),
                ])), \
                patch.object(autoplay, "foreground_window", side_effect=fg_values), \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "SkillTapShell") as shell, \
                patch("builtins.print"):
            vision.return_value.classify.return_value = battle
            autoplay.play(self.args(tap=True), self.target(), lambda *args: None, None)
            shell.return_value.tap.assert_called_once_with(Path("adb"), "device", 293, 184)
            self.assertEqual(vision.return_value.classify.call_count, 4)  # Discard the prefetched frame.

    def test_invalid_aspect_frame_never_classified_or_tapped(self):
        bad = Image.new("RGB", (900, 600))
        with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                patch.object(autoplay, "adb_frames", return_value=iter([(0, bad, None), (1, bad, None)])), \
                patch.object(autoplay, "foreground_window") as fg, \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "adb_run") as adb, \
                patch("builtins.print"):
            autoplay.play(self.args(tap=True), self.target(), lambda *args: None, None)
            fg.assert_not_called()
            vision.return_value.classify.assert_not_called()
            adb.assert_not_called()

    def test_streamed_skip_waits_for_native_screenshot_before_tapping(self):
        frame = Image.new("RGB", (1280, 720))
        stream = Mock()
        skip = Screen("skip", (888, 216), gems=30, skip_options=((20, (639, 365)),))

        def capture(*_args, battle_capture, fast_battle, **_kwargs):
            yield 0, frame, None
            self.assertTrue(fast_battle())
            battle_capture.stream = stream
            yield 0.15, frame, None
            stream.close.assert_called_once()
            self.assertFalse(fast_battle())
            yield 0.4, frame, None

        args = self.args(tap=True)
        args.skip = 20
        with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                patch.object(autoplay, "adb_frames", side_effect=capture), \
                patch.object(autoplay, "foreground_window", return_value=("com.raongames.growcastle", "game")), \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "human_tap", return_value=(639, 365)) as tap, \
                patch.object(autoplay, "SkillTapShell") as shell, \
                patch("builtins.print"):
            vision.return_value.classify.side_effect = [Screen("battle"), skip, skip]
            autoplay.play(args, self.target(), lambda *args: None, None)
            tap.assert_called_once()
            self.assertEqual(tap.call_args.args[2], (639, 365))
            shell.return_value.tap.assert_not_called()

    def test_preview_logs_exact_normalized_hero_action_without_tap(self):
        frame = Image.new("RGB", (640, 360))
        with patch.object(autoplay, "replay_frames", return_value=iter([(0, frame), (.5, frame)])), \
                patch.object(autoplay, "Vision") as vision, \
                patch.object(autoplay, "adb_run") as adb, \
                patch("builtins.print") as output:
            vision.return_value.classify.return_value = Screen("battle", ready=(2,))
            autoplay.play(self.args(tap=False, video=Path("recording.mp4")), None, lambda *args: None, None)
            output.assert_any_call("PREVIEW cast skill 1 (293, 184)", flush=True)
            adb.assert_not_called()


class SkillTapShellTests(unittest.TestCase):
    def make_process(self, *replies):
        process = Mock(stdin=BytesIO(), stdout=BytesIO(b"\n".join(replies) + b"\n"))
        process.poll.return_value = None
        return process

    def test_ordered_taps_share_one_shell(self):
        process = self.make_process(*([autoplay.SkillTapShell.ACK] * 3))
        shell = autoplay.SkillTapShell()
        self.addCleanup(shell.close)
        with patch.object(autoplay.subprocess, "Popen", return_value=process) as popen:
            shell.start("adb", "device")
            self.assertTrue(shell.tap("adb", "device", 135, 405))
            shell.start("adb", "device")
            self.assertTrue(shell.tap("adb", "device", 293, 184))
        commands = process.stdin.getvalue().splitlines()
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(commands[0].startswith(b"echo "))
        self.assertTrue(commands[1].startswith(b"input tap 135 405 "))
        self.assertTrue(commands[2].startswith(b"input tap 293 184 "))

    def test_unconfirmed_tap_never_replayed_on_fallback(self):
        process = self.make_process(autoplay.SkillTapShell.ACK, autoplay.SkillTapShell.FAILED)
        shell = autoplay.SkillTapShell()
        self.addCleanup(shell.close)
        with patch.object(autoplay.subprocess, "Popen", return_value=process), \
                patch.object(autoplay, "adb_run") as run:
            shell.start("adb", "device")
            self.assertFalse(shell.tap("adb", "device", 135, 405))
            run.assert_not_called()
            self.assertTrue(shell.tap("adb", "device", 293, 184))
            run.assert_called_once_with("adb", "device", "shell", "input", "tap", "293", "184")


if __name__ == "__main__":
    unittest.main()
