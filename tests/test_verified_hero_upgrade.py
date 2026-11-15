"""Exercise verified Hero purchases, white-gem reserves and recovery."""

from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

import autoplay
from emulator import EmulatorTarget
from hero_upgrade import HeroUpgrade, WAIT_SECONDS


HOME_POINT = (373, 92)
SINGLE_POINT = (895, 473)
BULK_POINT = (397, 624)
DETAIL_CLOSE = (994, 160)
LIST_CLOSE = (1245, 76)


def screen(name, **fields):
    defaults = dict(gems=None, level=None, cost=None, point=None, hero_entry=None,
                    hero=None, close_point=None, bulk_cost=None, bulk_point=None)
    defaults.update(fields)
    return SimpleNamespace(name=name, **defaults)


def home(gems):
    return screen("home", gems=gems, hero_entry=HOME_POINT)


def detail(gems, level, *, hero="chosen Hero", cost=1, bulk_cost=None):
    return screen("hero_detail", hero=hero, gems=gems, level=level, cost=cost,
                  point=SINGLE_POINT, close_point=DETAIL_CLOSE, bulk_cost=bulk_cost,
                  bulk_point=BULK_POINT if bulk_cost is not None else None)


class VerifiedHeroUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = Path(temporary.name) / "hero.json"
        self.upgrade = HeroUpgrade(lambda *_: None, "hero-2", 20, self.journal)

    def plan_purchase(self, panel):
        self.assertEqual(self.upgrade.feed(0, home(panel.gems)), (True, None))
        _, open_action = self.upgrade.feed(0.2, home(panel.gems))
        self.assertEqual(open_action.point, HOME_POINT)
        self.assertEqual(self.upgrade.feed(0.4, panel), (True, None))
        return self.upgrade.feed(0.6, panel)[1]

    def test_purchase_requires_same_title_balance_level_and_price(self):
        panel = detail(21, 200, bulk_cost=20)
        action = self.plan_purchase(panel)
        self.assertEqual((action.point, action.charge), (BULK_POINT, (21, 200, 20, 10)))
        for latest in (None, detail(21, 200, hero="other Hero", bulk_cost=20),
                       detail(22, 200, bulk_cost=20), detail(21, 201, bulk_cost=20),
                       detail(21, 200, bulk_cost=10)):
            self.assertFalse(self.upgrade.can_tap(action, latest))
        self.assertTrue(self.upgrade.can_tap(action, panel))
        self.upgrade.record_tap(action)
        self.assertEqual(json.loads(self.journal.read_text())["pending"], [21, 200, 20, 10])
        self.assertFalse(self.upgrade.can_tap(action, panel))

        after = detail(1, 210, bulk_cost=20)
        self.assertEqual(self.upgrade.feed(0.8, after), (True, None))
        self.assertEqual(self.upgrade.feed(1.0, after), (True, None))
        self.assertIsNone(json.loads(self.journal.read_text())["pending"])
        self.assertEqual(self.upgrade.feed(1.2, after), (True, None))
        _, close_detail = self.upgrade.feed(1.4, after)
        self.assertEqual(close_detail.point, DETAIL_CLOSE)

    def test_bulk_purchase_that_would_spend_reserve_falls_back_to_single(self):
        action = self.plan_purchase(detail(20, 200, cost=2, bulk_cost=20))
        self.assertEqual((action.point, action.charge), (SINGLE_POINT, (20, 200, 2, 1)))

    def test_unconfirmed_purchase_remains_pending_and_is_not_retried_on_restart(self):
        panel = detail(20, 200)
        self.upgrade.record_tap(self.plan_purchase(panel))
        saved = self.journal.read_bytes()
        restarted = HeroUpgrade(lambda *_: None, "hero-2", 20, self.journal)
        self.assertEqual(restarted.feed(0, panel), (True, None))
        self.assertEqual(restarted.feed(0.2, panel), (True, None))
        _, close_action = restarted.feed(WAIT_SECONDS + 0.3, panel)
        self.assertEqual(close_action.point, DETAIL_CLOSE)
        self.assertEqual(self.journal.read_bytes(), saved)
        with self.assertRaisesRegex(ValueError, "existing upgrade for hero-2"):
            HeroUpgrade(lambda *_: None, "hero-1", 20, self.journal)

    def test_empty_slot_closes_hero_list_and_resumes(self):
        self.upgrade.feed(0, home(20))
        self.upgrade.feed(0.2, home(20))
        hero_list = screen("hero_list", close_point=LIST_CLOSE)
        self.assertEqual(self.upgrade.feed(0.4, hero_list), (True, None))
        _, close_list = self.upgrade.feed(WAIT_SECONDS + 0.3, hero_list)
        self.assertEqual(close_list.point, LIST_CLOSE)
        self.assertEqual(self.upgrade.feed(WAIT_SECONDS + 0.4, home(20)), (False, None))
        self.assertFalse(self.journal.exists())

    def test_live_purchase_dispatch_rechecks_the_hero_and_records_before_tap(self):
        target = EmulatorTarget(emulator="ldplayer", adb=Path("adb"), serial="device",
                                instance="test", display_name="test")
        args = SimpleNamespace(mode="wave", tap=True, video=None, serial="device", seconds=5,
                               skip=None, ads=False, auto_captcha=False, humanize=True,
                               upgrade_tower=None, upgrade_hero="hero-2", hero_min_gems=2,
                               upgrade_gems=None, upgrade_levels=None)
        frame = Image.new("RGB", (1280, 720))
        png = BytesIO()
        frame.save(png, format="PNG")
        home = autoplay.Screen("home", gems=16, hero_entry=(373, 184), layout="hero")
        panel = autoplay.Screen("hero_detail", hero="selected", gems=16, level=20, cost=1,
                                point=SINGLE_POINT, close_point=DETAIL_CLOSE)
        for fresh_hero in ("selected", "changed"):
            with self.subTest(fresh_hero=fresh_hero), TemporaryDirectory() as temp:
                journal = Path(temp) / "hero.json"

                def run_adb(adb, serial, *command):
                    if command == ("exec-out", "screencap", "-p"):
                        return png.getvalue()
                    self.assertEqual(command, ("shell", "input", "tap", "895", "473"))
                    self.assertEqual(json.loads(journal.read_text())["pending"], [16, 20, 1, 1])

                frames = [(time, frame, None) for time in (0, 0.25, 0.5, 0.75)]
                fresh = replace(panel, hero=fresh_hero)
                with patch.object(autoplay, "ensure_adb_connected", return_value="device"), \
                        patch.object(autoplay, "adb_frames", return_value=iter(frames)), \
                        patch.object(autoplay, "foreground_window", return_value=("com.raongames.growcastle", "game")), \
                        patch.object(autoplay, "foreground_activity", return_value="game"), \
                        patch.object(autoplay, "Vision") as vision, \
                        patch.object(autoplay, "human_tap", return_value=(373, 184)) as navigate, \
                        patch.object(autoplay, "adb_run", side_effect=run_adb) as adb, \
                        patch("builtins.print"):
                    vision.return_value.classify.side_effect = [home, home, panel, panel, fresh]
                    autoplay.play(args, target, lambda *_: None, journal)
                    self.assertEqual(vision.call_args.kwargs["hero_point"], (373, 184))
                    navigate.assert_called_once()
                    self.assertEqual(adb.call_count, 2 if fresh_hero == "selected" else 1)
                if fresh_hero == "changed":
                    self.assertIsNone(json.loads(journal.read_text())["pending"])
