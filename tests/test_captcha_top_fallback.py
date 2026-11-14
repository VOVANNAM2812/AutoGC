"""The top-cup fallback for CAPTCHA rounds without enough tracking evidence."""

import unittest
from unittest.mock import patch

import bluestacks_captcha
from captcha import BOARD, SLOTS, Result


def no_selection(reason):
    return Result(1.0, 3.0, 5, None, 6, reason)


class TopFallbackTests(unittest.TestCase):
    def test_no_evidence_round_targets_top_cup(self):
        result = no_selection("too little consistent tracking evidence; votes: left=2")
        fallback = bluestacks_captcha.top_fallback(result)
        x, y = SLOTS[0]
        self.assertEqual(fallback.coordinate, (BOARD[0] + 2 * x, BOARD[1] + 2 * y))
        self.assertEqual(fallback.initial_slot, result.initial_slot)

    def test_other_failures_are_left_alone(self):
        self.assertIsNone(bluestacks_captcha.top_fallback(no_selection("cup layout did not settle")))
        detected = Result(1.0, 3.0, 5, 2, 9, "")
        self.assertIsNone(bluestacks_captcha.top_fallback(detected))

    def test_tap_top_fallback_taps_once(self):
        result = no_selection("too little consistent tracking evidence")
        with patch.object(bluestacks_captcha, "tap_cup") as tap:
            outcome = bluestacks_captcha.tap_top_fallback("adb", "dev", result, "board")
        self.assertEqual(outcome, bluestacks_captcha.TOP_FALLBACK)
        self.assertEqual(tap.call_count, 1)
        self.assertEqual(tap.call_args.args[2].final_slot, 0)

        with patch.object(bluestacks_captcha, "tap_cup") as tap:
            outcome = bluestacks_captcha.tap_top_fallback("adb", "dev", no_selection("cup layout did not settle"), "b")
        self.assertFalse(outcome)
        tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
