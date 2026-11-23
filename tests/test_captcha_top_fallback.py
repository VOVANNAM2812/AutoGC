"""The top-cup fallback for CAPTCHA rounds without a detected cup."""

import unittest
from unittest.mock import patch

import bluestacks_captcha
from captcha import BOARD, SLOTS, Result


def no_selection(reason):
    return Result(1.0, 3.0, 5, None, 6, reason)


class TopFallbackTests(unittest.TestCase):
    def test_every_no_selection_reason_targets_top_cup(self):
        for reason in (
            "too little consistent tracking evidence; votes: left=2",
            "no fast spin matched the diamond entry",
            "no visible diamond entry",
            "cup layout did not settle",
        ):
            with self.subTest(reason=reason):
                result = no_selection(reason)
                fallback = bluestacks_captcha.top_fallback(result)
                x, y = SLOTS[0]
                self.assertEqual(fallback.coordinate, (BOARD[0] + 2 * x, BOARD[1] + 2 * y))
                self.assertEqual(fallback.initial_slot, result.initial_slot)

    def test_detected_selection_is_not_overridden(self):
        detected = Result(1.0, 3.0, 5, 2, 9, "")
        self.assertIsNone(bluestacks_captcha.top_fallback(detected))

    def test_tap_top_fallback_taps_once(self):
        result = no_selection("no fast spin matched the diamond entry")
        with patch.object(bluestacks_captcha, "tap_cup") as tap:
            outcome = bluestacks_captcha.tap_top_fallback("adb", "dev", result, "board")
        self.assertEqual(outcome, bluestacks_captcha.TOP_FALLBACK)
        self.assertEqual(tap.call_count, 1)
        self.assertEqual(tap.call_args.args[2].final_slot, 0)

        with patch.object(bluestacks_captcha, "tap_cup") as tap:
            outcome = bluestacks_captcha.tap_top_fallback(
                "adb", "dev", no_selection("cup layout did not settle"), "b"
            )
        self.assertEqual(outcome, bluestacks_captcha.TOP_FALLBACK)
        tap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
