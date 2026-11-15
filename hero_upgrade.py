"""Upgrade one equipped Hero while preserving a white gem balance."""

from base64 import b64decode, b64encode
from dataclasses import asdict, dataclass
import json
from pathlib import Path

import numpy as np
from PIL import Image

from tower_upgrade import Action, VALID_BULK_COSTS, VALID_COSTS


GEM_RESERVE = 1
MINIMUM_GEMS = GEM_RESERVE + 1
BULK_LEVELS = 10
STABLE_SECONDS = 0.18
WAIT_SECONDS = 5
HOME_READING_SECONDS = 2.5
HOME_SCREENS = {"home", "gold"}


def _valid_charge(gems, level, cost, levels):
    if not all(type(value) is int for value in (gems, level, cost, levels)):
        return False
    valid_cost = (levels == 1 and cost in VALID_COSTS
                  or levels == BULK_LEVELS and cost in VALID_BULK_COSTS)
    return valid_cost and level >= 1 and gems - cost >= GEM_RESERVE


def _purchase_on(screen):
    bulk_charge = (screen.gems, screen.level, screen.bulk_cost, BULK_LEVELS)
    if screen.bulk_point is not None and _valid_charge(*bulk_charge):
        return screen.bulk_point, bulk_charge
    single_charge = (screen.gems, screen.level, screen.cost, 1)
    if screen.point is not None and _valid_charge(*single_charge):
        return screen.point, single_charge
    return None


@dataclass
class _Batch:
    slot: str
    hero: str
    pending: tuple[int, int, int, int] | None = None
    next_gems: int | None = None
    started: bool = False

    def validate(self):
        if (not isinstance(self.slot, str) or not self.slot
                or not isinstance(self.hero, str) or not self.hero or type(self.started) is not bool):
            raise ValueError("Missing Hero upgrade identity")
        if self.pending is not None:
            if not isinstance(self.pending, (list, tuple)) or len(self.pending) not in (3, 4):
                raise ValueError("Invalid pending Hero upgrade")
            pending = tuple(self.pending)
            if len(pending) == 3:
                pending = (*pending, 1)  # Older journals only recorded single-level purchases.
            if not _valid_charge(*pending) or self.next_gems is not None:
                raise ValueError("Invalid pending Hero upgrade")
            self.pending = pending
            self.started = True
        if self.next_gems is not None and (type(self.next_gems) is not int
                                           or self.next_gems < MINIMUM_GEMS):
            raise ValueError("Invalid Hero upgrade restart threshold")


class HeroUpgrade:
    def __init__(self, report, hero, minimum_gems=MINIMUM_GEMS, journal=None):
        self.report = report
        self.slot = hero
        self.minimum_gems = max(MINIMUM_GEMS, minimum_gems)
        self.next_gems = self.minimum_gems
        self.journal = journal
        self.batch = None
        self.navigation = None
        self.observation = None
        self.since = 0.0
        self.waiting_since = None
        self.last_balance = None
        self.planned = None
        self.reopened_pending = False
        self.recovering_purchase = False
        self.aborting = False
        self.disabled = False
        if journal is not None and journal.exists():
            try:
                data = json.loads(journal.read_text(encoding="utf-8"))
                if data is not None:
                    self.batch = _Batch(**data)
                    self.batch.validate()
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid Hero upgrade journal") from exc
            if self.batch is not None and self.batch.slot != hero:
                if self.batch.pending is not None:
                    raise ValueError(f"Finish the existing upgrade for {self.batch.slot} before selecting {hero}")
                self.batch = None
                self._save()
            self.recovering_purchase = self.batch is not None and self.batch.pending is not None

    def _save(self):
        if self.journal is not None:
            temporary = self.journal.with_suffix(".tmp")
            data = asdict(self.batch) if self.batch is not None else None
            temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
            temporary.replace(self.journal)

    @staticmethod
    def _matches_hero(observed, expected):
        if not observed or not expected:
            return False
        return observed == expected or same_hero(observed, expected)

    def suspend(self):
        self.observation = None
        self.planned = None

    def needs_home_reading(self, time, screen):
        return not self.disabled and (
            time - self.since < STABLE_SECONDS or screen.gems is None
            or (screen.gems >= self.next_gems and screen.hero_entry is None)
        )

    def _navigate(self, time, label, point, *expected):
        self.navigation = (time, expected)
        self.waiting_since = None
        return True, Action(label, point)

    def _timed_out(self, time):
        if self.waiting_since is None:
            self.waiting_since = time
        return time - self.waiting_since >= WAIT_SECONDS

    def _wait(self, time, message):
        if self._timed_out(time):
            raise RuntimeError(message)
        return True, None

    def _wait_for_purchase(self, time, screen):
        if self._timed_out(time):
            gems, level, cost, levels = self.batch.pending
            self.report(
                time, f"Hero upgrade +{levels} unconfirmed. Expected level {level + levels} and "
                f"{gems - cost} white gems. Last read level {screen.level} and {screen.gems} white gems. "
                "Closing the Hero menu and leaving the purchase pending.",
            )
            return self._stop(time, screen)
        return True, None

    def _stop(self, time, screen, message=None):
        if message:
            self.report(time, message)
        self.aborting = True
        self.navigation = None
        self.waiting_since = None
        self.suspend()
        if screen.name in HOME_SCREENS:
            self.disabled = True
            self.report(time, "Hero upgrades disabled for this run. Resuming autoplay.")
            return False, None
        return self._close_menu(time, screen)

    def feed(self, time, screen):
        self.planned = None
        if self.disabled:
            return False, None
        name, gems = ("home" if screen.name == "gold" else screen.name), screen.gems
        if self.aborting and screen.name in HOME_SCREENS:
            return self._stop(time, screen)
        if (not self.aborting and self.recovering_purchase
                and self.navigation is None and not self.reopened_pending
                and name not in {"home", "hero_detail", "hero_list"}):
            self.suspend()
            return False, None
        fingerprint = screen.hero
        if self.batch is not None and self._matches_hero(fingerprint, self.batch.hero):
            fingerprint = self.batch.hero
        elif (not self.aborting and self.batch is None
                and name == "hero_detail" and self.observation is not None):
            previous_name, _, _, previous_hero, *_ = self.observation
            if previous_name == name and self._matches_hero(fingerprint, previous_hero):
                fingerprint = previous_hero
        purchase = None
        if self.aborting:
            observation = (name, fingerprint, screen.close_point)
        else:
            purchase = _purchase_on(screen) if name == "hero_detail" else None
            observation = (name, gems, screen.level, fingerprint)
            if self.batch is None or self.batch.pending is None or name != "hero_detail":
                observation += (screen.hero_entry, screen.close_point, purchase)
        if observation != self.observation:
            self.observation, self.since = observation, time
        stable = time - self.since >= STABLE_SECONDS

        if not self.aborting and self.batch is None and self.navigation is None:
            if (name != "home" or gems is None or gems < self.next_gems
                    or screen.hero_entry is None):
                return False, None
            if not stable:
                return True, None
            self.last_balance = gems
            self.report(time, f"Hero upgrade started for {self.slot} at {gems} white gems")
            return self._navigate(time, f"open {self.slot} upgrade", screen.hero_entry, "hero_detail")

        if self.navigation is not None:
            started, expected = self.navigation
            if (name not in expected or not stable
                    or (name == "hero_detail" and self.batch is None and not fingerprint)):
                if time - started >= WAIT_SECONDS:
                    if self.aborting:
                        raise RuntimeError("Hero menu did not close. Close it manually to resume autoplay")
                    return self._stop(time, screen, f"Hero upgrade could not verify {' or '.join(expected)}")
                return True, None
            self.navigation = None

        if self.aborting:
            if not stable:
                return self._wait(time, "Hero menu could not be verified. Close it manually to resume autoplay")
            return self._close_menu(time, screen)
        if not stable:
            if self.batch is not None and self.batch.pending is not None:
                return self._wait_for_purchase(time, screen)
            if self._timed_out(time):
                return self._stop(time, screen, "Hero upgrade stopped because the screen did not stabilize")
            return True, None
        if self.batch is None:
            if name != "hero_detail" or not fingerprint:
                return self._wait(time, "Hero upgrade could not verify the selected Hero panel")
            self.batch = _Batch(self.slot, fingerprint)
            self._save()

        batch = self.batch
        if batch.next_gems is not None:
            return self._finish(time, screen)
        if (name == "home" and batch.pending is not None and not self.reopened_pending
                and screen.hero_entry is not None):
            self.reopened_pending = True
            return self._navigate(time, f"verify pending purchase for {self.slot}", screen.hero_entry, "hero_detail")
        if (name == "hero_list" and batch.pending is not None and not self.reopened_pending
                and self.recovering_purchase and screen.close_point is not None):
            return self._navigate(time, "close Hero list", screen.close_point, "home")
        if name in {"home", "hero_list"} and batch.pending is None:
            return self._start_finish(time, screen, (self.last_balance or 0) + 1)
        if name != "hero_detail" or fingerprint != batch.hero:
            if batch.pending is not None:
                return self._wait_for_purchase(time, screen)
            if self._timed_out(time):
                return self._stop(time, screen, "Hero upgrade stopped because the selected Hero panel was not verified")
            return True, None
        if gems is not None:
            self.last_balance = gems

        if batch.pending is not None:
            before_gems, before_level, cost, levels = batch.pending
            expected_gems, expected_level = before_gems - cost, before_level + levels
            if (gems, screen.level) == (expected_gems, expected_level):
                self.report(time, f"{self.slot} level {screen.level} confirmed ({gems} white gems)")
            elif (self.recovering_purchase and gems is not None and gems > before_gems
                  and screen.level is not None and screen.level >= expected_level):
                # A restarted run can see gems earned since the old purchase. Use the
                # verified current balance and apply the new run's starting threshold.
                batch.started = False
                self.report(time, f"{self.slot} current level {screen.level} is at or above the previous "
                                  f"upgrade target {expected_level}. Rechecking the starting threshold with {gems} white gems.")
            else:
                return self._wait_for_purchase(time, screen)
            batch.pending = None
            self.reopened_pending = False
            self.recovering_purchase = False
            self._save()
            self.waiting_since = None
            self.suspend()
            return True, None

        if not batch.started and gems is not None and gems < self.minimum_gems:
            return self._start_finish(time, screen, self.minimum_gems)
        if gems is not None and gems < MINIMUM_GEMS:
            return self._start_finish(time, screen, MINIMUM_GEMS)
        if purchase is None:
            if (gems is not None and type(screen.cost) is int and screen.cost in VALID_COSTS
                    and gems - screen.cost < GEM_RESERVE):
                return self._start_finish(time, screen, screen.cost + GEM_RESERVE)
            if not self._timed_out(time):
                return True, None
            self.report(time, f"{self.slot} single-level upgrade could not be verified")
            return self._start_finish(time, screen, (self.last_balance or 0) + 1)

        self.waiting_since = None
        point, charge = purchase
        _, level, cost, levels = charge
        self.planned = Action(
            f"upgrade {self.slot} +{levels} to level {level + levels} ({cost} white gems)", point, charge
        )
        return True, self.planned

    def can_tap(self, action, latest):
        return (action is not None and action == self.planned and action.charge is not None
                and self.batch is not None and self.batch.pending is None and latest is not None
                and latest.name == "hero_detail"
                and self._matches_hero(latest.hero, self.batch.hero)
                and _purchase_on(latest) == (action.point, action.charge))

    def record_tap(self, action):
        batch = self.batch
        if (batch is None or batch.pending is not None or action is None or action != self.planned
                or action.charge is None or not _valid_charge(*action.charge)):
            raise RuntimeError("Hero upgrade stopped because the planned purchase was not verified")
        # Persist the expected debit before tapping; never retry a pending charge.
        batch.pending = action.charge
        batch.started = True
        self._save()
        self.suspend()

    def _start_finish(self, time, screen, next_gems):
        self.batch.next_gems = max(self.minimum_gems, next_gems)
        self.waiting_since = None
        self._save()
        return self._finish(time, screen)

    def _finish(self, time, screen):
        if screen.name in {"home", "gold"}:
            self.next_gems = max(self.minimum_gems, self.batch.next_gems)
            if not self.batch.started and screen.gems is not None:
                self.next_gems = max(self.next_gems, screen.gems + 1)
            self.batch = None
            self.waiting_since = None
            self._save()
            self.report(time, f"Hero upgrade finished for {self.slot}; resuming play")
            return True, None
        return self._close_menu(time, screen)

    def _close_menu(self, time, screen):
        if screen.name == "hero_detail" and screen.close_point is not None:
            return self._navigate(time, "close Hero detail", screen.close_point, "hero_list", "home")
        if screen.name == "hero_list" and screen.close_point is not None:
            return self._navigate(time, "close Hero list", screen.close_point, "home")
        return self._wait(time, "Hero menu could not be verified. Close it manually to resume autoplay")


HOME_TEXT = {
    "town": (20, 66, 148, 105),
    "upgrade": (923, 89, 1218, 129),
    "battle": (1126, 668, 1257, 706),
    "replay": (950, 668, 1084, 706),
}
UI = {
    "wallet": ((252, 4, 285, 34), 17),
    "wallet_dim": ((252, 4, 285, 34), 12),
    "detail_close": ((972, 137, 1014, 181), 13),
    "detail_border": ((255, 492, 286, 544), 16),
    "equipped": ((635, 449, 765, 489), 19),
    "single_level_up": ((846, 438, 941, 474), 17),
    "price_diamond": ((865, 482, 900, 512), 15),
    "bulk_level_up": ((305, 590, 491, 627), 15),
    "bulk_price_diamond": ((358, 633, 395, 665), 14),
    "list_title_dim": ((847, 27, 1015, 74), 13),
    "list_title": ((847, 27, 1015, 74), 15),
    "list_close": ((1224, 56, 1267, 97), 15),
}
GEMS_BOX = (289, 2, 400, 36)
LEVEL_BOX = (305, 418, 459, 459)
TITLE_BOX = (324, 104, 886, 154)
DETAIL_CLOSE = (994, 160)
LIST_CLOSE = (1245, 76)
LEVEL_UP = (895, 473)
BULK_LEVEL_UP = (397, 624)
FINGERPRINT_SIZE = (12, 16)
GLYPH_RECORD_SIZE = 2 + FINGERPRINT_SIZE[0] * FINGERPRINT_SIZE[1] // 8


def _crop(rgb, box):
    left, top, right, bottom = box
    return rgb[top:bottom, left:right]


def _white_mask(rgb, minimum, spread):
    return (rgb.min(axis=2) > minimum) & (np.ptp(rgb, axis=2) < spread)


def _glyphs(mask):
    columns = mask.any(axis=0)
    starts = np.flatnonzero(columns & ~np.r_[False, columns[:-1]])
    ends = np.flatnonzero(columns & ~np.r_[columns[1:], False]) + 1
    for start, end in zip(starts, ends):
        tile = mask[:, start:end]
        rows = np.flatnonzero(tile.any(axis=1))
        yield start, end, tile[rows[0]:rows[-1] + 1]


def _glyph_mask(rgb, palette):
    red, green, blue = rgb.transpose(2, 0, 1)
    if palette == "level":
        return (green > 106) & (green - red > 49) & (green - blue > 49)
    if palette == "cost":
        return (blue > 115) & (blue - red > 46) & (green - red > 40)
    minimum = max(54, np.quantile(rgb.max(axis=2), 0.995) * 0.59)
    return _white_mask(rgb, minimum, 32)


def _title_fingerprint(rgb):
    mask = _white_mask(rgb, 155, 32)
    letters = list(_glyphs(mask))
    if not 1 <= len(letters) <= 28:
        return None
    fingerprint = bytearray()
    previous_end = None
    for start, end, glyph in letters:
        height, width = glyph.shape
        if not (3 <= height <= 40 and 3 <= width <= 42):
            return None
        gap = 0 if previous_end is None else start - previous_end
        if gap > 32:
            return None
        previous_end = end
        aspect = round(16 * width / height)
        fingerprint.extend((gap, aspect))
        image = Image.fromarray(glyph.astype(np.uint8) * 255)
        normalized = np.asarray(image.resize(FINGERPRINT_SIZE, Image.Resampling.BILINEAR)) >= 128
        fingerprint.extend(np.packbits(normalized).tobytes())
    return b64encode(fingerprint).decode("ascii")


def _expand_glyph(glyph):
    padded = np.pad(glyph, 1)
    return (glyph | padded[:-2, 1:-1] | padded[2:, 1:-1]
            | padded[1:-1, :-2] | padded[1:-1, 2:])


def same_hero(left, right):
    """Compare every title glyph so similar names cannot match by one global score."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        first = b64decode(left, validate=True)
        second = b64decode(right, validate=True)
    except ValueError:
        return False
    if (not first or len(first) != len(second) or len(first) % GLYPH_RECORD_SIZE):
        return False
    for start in range(0, len(first), GLYPH_RECORD_SIZE):
        end = start + GLYPH_RECORD_SIZE
        if abs(first[start] - second[start]) > 5 or abs(first[start + 1] - second[start + 1]) > 4:
            return False
        a, b = (np.unpackbits(np.frombuffer(key[start + 2:end], dtype=np.uint8))
                .reshape(FINGERPRINT_SIZE[::-1]).astype(bool) for key in (first, second))
        union = np.count_nonzero(a | b)
        if np.count_nonzero(a ^ b) > max(10, union * 0.42):
            return False
        # The dialog pulses during upgrades, which shifts thin letter strokes by a pixel.
        unmatched = np.count_nonzero(a & ~_expand_glyph(b)) + np.count_nonzero(b & ~_expand_glyph(a))
        if unmatched > max(4, union * 0.10):
            return False
    return True


class HeroVision:
    def __init__(self, home_point):
        self.home_point = home_point
        with np.load(Path(__file__).with_name("hero_templates.npz"), allow_pickle=False) as atlas:
            self.ui = {key: atlas[key].astype(np.int16) for key in UI}
            self.text = {key: _white_mask(atlas[key], 145, 48) for key in HOME_TEXT}
            self.numbers = {key: atlas[key].astype(bool) for key in ("gems", "level", "cost")}

    def _locate(self, rgb, name, horizontal=3, vertical=3):
        (left, top, right, bottom), limit = UI[name]
        reference = self.ui[name]
        unshifted = float(np.mean(np.abs(rgb[top:bottom, left:right] - reference)))
        if unshifted < limit * 0.35:
            return 0, 0
        best = min(unshifted, float(limit))
        location = (0, 0) if unshifted < limit else None
        for dy in range(-vertical, vertical + 1):
            for dx in range(-horizontal, horizontal + 1):
                sample = rgb[top + dy:bottom + dy, left + dx:right + dx]
                difference = float(np.mean(np.abs(sample - reference)))
                if difference < best:
                    best = difference
                    location = dx, dy
        return location

    def _number(self, rgb, palette, limit):
        if not rgb.size:
            return None
        digits = list(_glyphs(_glyph_mask(rgb, palette)))
        if not 1 <= len(digits) <= limit:
            return None
        value = 0
        for _, _, glyph in digits:
            height, width = glyph.shape
            if not (18 <= height <= 28 and 11 <= width <= 21):
                return None
            normalized = np.asarray(Image.fromarray(glyph).resize((18, 24)), dtype=bool)
            scores = np.count_nonzero(self.numbers[palette] != normalized, axis=(2, 3)).min(axis=1)
            best, second = np.argsort(scores)[:2]
            if scores[best] > 72 or scores[second] - scores[best] < 17:
                return None
            value = 10 * value + int(best)
        return value

    def _gems(self, rgb, *, dimmed=False):
        if self._locate(rgb, "wallet_dim" if dimmed else "wallet", 2, 2) is None:
            return None
        wallet = _crop(rgb, GEMS_BOX)
        red, green, blue = wallet.transpose(2, 0, 1)
        orange = (red > 72) & (red - green > 19) & (green - blue > 24) & (red - blue > 55)
        suffix = np.flatnonzero(orange.sum(axis=0) >= 2)
        if suffix.size:
            wallet = wallet[:, :suffix[0]]
        return self._number(wallet, "gems", 5)

    def is_home(self, rgb):
        for name, (left, top, right, bottom) in HOME_TEXT.items():
            reference = self.text[name]
            for dx, dy in ((0, 0), (0, 1), (0, -1), (0, 2), (-1, 1), (1, 1), (-2, 0), (-2, 2)):
                mask = _white_mask(rgb[top + dy:bottom + dy, left + dx:right + dx], 145, 48)
                if np.count_nonzero(mask & reference) > 0.63 * np.count_nonzero(mask | reference):
                    break
            else:
                return False
        return True

    def home(self, rgb, *, verified_home=False):
        """Return the white diamond count and a visible Hero slot on a verified home screen."""
        rgb = np.asarray(rgb, dtype=np.int16)
        if not verified_home and not self.is_home(rgb):
            return None, None
        gems = self._gems(rgb)
        if self.home_point is None:
            return gems, None
        x, y = self.home_point
        outline = rgb[y - 22:y + 23, x - 34:x + 51].max(axis=2) < 94
        has_slot = (outline[:, :10].mean(axis=0).max() >= 0.65
                    and outline[:, -12:].mean(axis=0).max() >= 0.65)
        return gems, self.home_point if has_slot else None

    def panel(self, rgb):
        """Return verified Hero dialog fields suitable for Screen construction."""
        rgb = np.asarray(rgb, dtype=np.int16)
        if self._locate(rgb, "list_title_dim", 2, 2) is not None:
            close = self._locate(rgb, "detail_close", 7, 7)
            if close is not None and self._locate(rgb, "detail_border", 7, 7) is not None:
                return self._detail(rgb)
        if self._locate(rgb, "list_title") is not None and self._locate(rgb, "list_close") is not None:
            return {"name": "hero_list", "close_point": LIST_CLOSE}
        return None

    def _detail(self, rgb):
        hero = _title_fingerprint(_crop(rgb, TITLE_BOX))
        gems = self._gems(rgb, dimmed=True)
        level = self._number(_crop(rgb, LEVEL_BOX), "level", 7)
        cost = None
        point = None
        bulk_cost = None
        if self._locate(rgb, "equipped", 7, 7) is not None:
            button = self._locate(rgb, "single_level_up", 7, 7)
            if button is not None:
                diamond = self._locate(rgb, "price_diamond", 51, 6)
                if diamond is not None:
                    dx, dy = diamond
                    value = self._number(rgb[480 + dy:515 + dy, 901 + dx:981], "cost", 4)
                    if value in VALID_COSTS:
                        cost = value
                        point = LEVEL_UP
            bulk_cost = self._bulk_price(rgb)
        return {"name": "hero_detail", "hero": hero, "gems": gems, "level": level,
                "cost": cost, "point": point, "close_point": DETAIL_CLOSE,
                "bulk_cost": bulk_cost, "bulk_point": BULK_LEVEL_UP if bulk_cost is not None else None}

    def _bulk_price(self, rgb):
        # The left control is LV UP +10. Require its complete label and diamond price.
        button = self._locate(rgb, "bulk_level_up", 7, 7)
        if button is None:
            return None
        diamond = self._locate(rgb, "bulk_price_diamond", 7, 7)
        if diamond is None:
            return None
        dx, dy = diamond
        if abs(dx - button[0]) > 3 or abs(dy - button[1]) > 3:
            return None
        value = self._number(rgb[631 + dy:670 + dy, 396 + dx:495], "cost", 4)
        return value if value in VALID_BULK_COSTS else None
