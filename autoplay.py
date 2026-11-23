"""Play Grow Castle waves, cast ready skills, and collect gold ads on BlueStacks 5 or LDPlayer 9."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from io import BytesIO
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
import random
import struct
import subprocess
from threading import Thread
from time import monotonic, sleep
import zlib

import numpy as np
from PIL import Image, ImageOps

from ad_closer import AdCloser, CloseVideoDetector, KEYCODE_BACK
from battle_capture import BattleFrameStream
from bluestacks_captcha import TOP_FALLBACK, watch_target
from captcha import VIEWPORT
from emulator import (
    AD_OVERLAY_PACKAGES,
    adb_run,
    ensure_adb_connected,
    foreground_activity,
    foreground_window,
    format_instances,
    human_tap,
    is_ad_activity,
    resolve_target,
)
from hero_upgrade import HOME_READING_SECONDS, MINIMUM_GEMS, HeroUpgrade, HeroVision
from tower_upgrade import (
    Action,
    DEFAULT_HERO_SLOT,
    DETAIL_CLOSE,
    EXIT_LAYER_VIEW,
    GEM_THRESHOLD,
    LIST_CLOSE,
    TowerUpgrade,
    lock_journal,
)
from tower_vision import load_gem_templates, read_white_gems
from windows_video import read_video


TEMPLATES = {
    "hud": ((0, 6, 29, 36), 17),
    "hud_dim": ((0, 6, 29, 36), 10),
    "upgrade": ((924, 94, 1217, 129), 25),
    "battle": ((1126, 673, 1256, 702), 20),
    "replay": ((951, 673, 1083, 702), 20),
    "wave": ((615, 40, 707, 68), 13),
    "wave_late": ((615, 40, 707, 68), 0),
    "speed": ((25, 634, 96, 680), 17),
    "gold_coin": ((253, 617, 322, 681), 14),
    "gold_get": ((565, 620, 688, 655), 14),
    "gold_get_centered": ((562, 625, 690, 678), 17),
    "replay_100": ((804, 633, 919, 669), 19),
    "replay_80": ((957, 633, 1072, 669), 19),
    "skip_title": ((475, 242, 797, 294), 16),
    "skip_close": ((866, 194, 911, 238), 17),
    "victory": ((457, 232, 827, 310), 18),
    "processing": ((466, 260, 831, 311), 20),
    "captcha": ((407, 91, 867, 129), 16),
    "captcha_start": ((847, 536, 1003, 623), 14),
    "hp_zero": ((918, 7, 938, 29), 0),
    "reward": ((1068, 26, 1266, 56), 0),
    "reward_light": ((1098, 30, 1268, 54), 0),
    "reward_mixed": ((1060, 22, 1262, 53), 0),
    "ad_end_close": ((1237, 22, 1277, 62), 15),
    "ad_end_cta": ((247, 651, 393, 680), 15),
    "ad_sheet_close": ((1237, 19, 1277, 59), 14),
    "ad_sheet_chevron": ((1219, 136, 1245, 159), 15),
    "ad_sheet_cta": ((958, 535, 1162, 589), 16),
}
GOLD_HUD = (36, 4, 194, 39)
BOSS_BAR_REGION = (360, 30, 780, 78)
BOSS_FFT_SHAPE = (64, 512)
# Invariant white strokes ('#') and dark outline ('@') of the BOSS health bar label;
# '.' marks the HP fill area so matching succeeds whether Boss HP is full red or empty black.
BOSS_GLYPH = (
    ".@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
    "@@.........@@@@@.......@@@@@.........@@@@........",
    "@.########.@@@@@.#####.@@@@@.#######.@@@.########",
    "@.########...@@..#####...@...#######.@@..########",
    "@.##......##.@.##.....##.@.##........@.##........",
    "@.##.@..@.##.@.##@...@##.@.##@.........##.@......",
    "@.##.@....##.@.##@...@##.@.##@.........##.@......",
    "@.##......##.@.##@...@##.@.#.......@...##......@.",
    "@.########..@@.##@...@##.@...#####.@.....######.@",
    "@.########...@.##@...@##...@.#####...@...######..",
    "@.##.@@@@.##.@.##@...@##....@@@@@@.#.@..@@@@@@.##",
    "@.##.@....##.@.##@...@##.........@.#.@.........##",
    "@.##.@@@@.##.@.##@...@##.@@@@@@@@@.#.@@@@@@@@@.##",
    "@.##......##.@.##.....##.@.........#.@.........##",
    "@.########..@@...#####...@.#######...@.########..",
    "@.########.@@@@@.#####.@@@.#######.@@@.########.@",
    "@@.........@@@@@......@@@@.........@@@.........@@",
    "..@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
)
SMITH_SKILL = ("Smith (HP < 50%)", 373, 51, 92)
ARCHER_SKILLS = (
    ("archer skill 1", 135, 363, 405),
    ("archer skill 2", 135, 480, 522),
)
ALTAR_SKILL_POINT = (118, 180)
HERO_SKILLS = tuple(
    (f"skill {idx}", x, bar_y, hero_y)
    for idx, (x, bar_y, hero_y) in enumerate(
        ((x, bar_y, hero_y) for bar_y, hero_y in ((146, 184), (241, 277), (331, 370))
         for x in (293, 373, 453)),
        start=1,
    )
)
SKILLS = ARCHER_SKILLS + HERO_SKILLS
HERO_SKILL_SLOTS = tuple(f"hero-{i}" for i in range(1, len(HERO_SKILLS) + 1))
SKILL_SLOTS = tuple(f"archer-{i}" for i in range(1, len(ARCHER_SKILLS) + 1)) + HERO_SKILL_SLOTS
GAME_SCREENS = {"home", "gold", "replay_menu", "battle", "skip"}
HERO_MENUS = {"hero_detail", "hero_list"}
RETURN_SCREENS = GAME_SCREENS | HERO_MENUS | {"victory", "defeat", "tower", "castles", "captcha"}
AD_CLOSE_SCREENS = {
    "reward_granted",
    "ad_close_video",
    "ad_end_card",
    "ad_end_sheet",
    "ad_ui_close",
    "ad_visual_close",
    "ad_back",
    "ad_external_overlay",
}
SKIP_CLOSE_POINT = (888, 216)
SKIP_TIERS = (10, 20, 30)
SKIP_10_POINT = (469, 365)
SKIP_20_POINT = (639, 365)
SKIP_30_POINT = (809, 365)
SKIP_POINTS = {10: SKIP_10_POINT, 20: SKIP_20_POINT, 30: SKIP_30_POINT}
SKIP_GEM_THRESHOLD = 30
SKILL_CAST_INTERVAL = 0.15
ADB_FRAME_INTERVAL = 0.18
LDPLAYER_CAPTURE_INTERVAL = 0.30
CAPTCHA_MAX_ATTEMPTS = 3
CAPTCHA_RETRY_DELAY = 3.0
# After a no-evidence round taps the top cup, wait this long before retrying.
CAPTCHA_FALLBACK_WAIT = 10.0
# Allow post-wave guild/ranking notices to clear before opening a Hero slot.
WAVE_HERO_HOME_WAIT = 4.0
SKIP_SLOTS = (
    (418, 522, 434, SKIP_10_POINT),
    (588, 692, 604, SKIP_20_POINT),
    (758, 862, 774, SKIP_30_POINT),
)
SKIP_30_MASK_SHAPE = (35, 72)
SKIP_MASKS_B85 = {
    10: (
        "c-n=}Jqmy@3<cl^88Y@lf)^87aOmijJXVL$_?HM`t8e<qBf|sk3_vUgCP1YL60m9lSP;PLC-"
        "$#^_zzM8XUA;Fj>W4&4(=kofX8K23FnHa_1e{f*!FHV*q=;i"
    ),
    20: (
        "c-n=~Jqmy@3<cmP9XjR+Uc*83S`I=B4jsKxkJX_VO@9Wl#W#JBA$b96G<%p(z_E#yqh_|ihJ"
        "~#51?mGCL12C`1UgutJULf!yr4au2{S0j3hZ4xi}M*B$YCQ-lv)_Ra<3L*{zgAH{e4&4"
    ),
    30: (
        "c-no>!3n@13<c1Dha9s|>0CV&4D{eyZqOAR!-Leupdx7dPmhEUK0u4j32an6AYLP3&MY#47="
        "3{!HVrz#h7E{q!8xI?$kUbUxpN-~bSq$|h!f0+L%<za0XR;23zW~=YYm&=FE4>xSm6"
    ),
}
SKIP_30_MASK_B85 = SKIP_MASKS_B85[30]
# Background-masked 28x28 RGB + 1-bit mask of the open Mimic chest (mouth, fangs, tongue, lid).
MIMIC_TEMPLATE_B85 = (
    "c-pO%Eo|FB6vtmwsU4V<0|z`Q+6o2+3IYraPMRlKnn8epfx&?T2Mi1h3<9j~fPsOf83Ysr1Ox;G1"
    "QY}m1QY}m1Qc{q!6p?H6a*IR>s{KdYn9ebvZs@B@%es!U-v(NCFVQSV<cdNGa@V^ZITEI8<=oOCP"
    "@*JoRi!qS&O6&!RK8;V|N{L5s^uGZ{tyH^a6U|({X5wLZj`hj6Ll=+%k!cqsSNvIuH~m<H&%`3_a"
    "{~dKenx#X#n9VxZ@^#7(j`7MVCkl8zTn4khh!3L9Hr&{3$FIO%ZGkz{jb$w^mdfFiTNPVoK%6HR%"
    "v6VSpVZHE+|jvd{`gxsvg_R3#L7?+k6+KseDN}rYsrY_u{9%!>_X58}s{hPItuqi^;R0s@o9<WmA"
    "BsN&rr<p^L*|yhA$fQF@+3ntrPji<lo1|>PWNR{eDV_H2_9|B&+tjXL9`k(V^0jo}$V}ZkY`~iOR"
    "LbI1mP4<-es8p1-U*TA90lq;EKfwyaU!i7K{oW#12<lq-)8<yXeTl7k(W+*e(0;*4wtutRedl;(9"
    "CUdCQHOld5&0QcHKmp-qQC&^I5u7I1-sBnyCi{zMN+53UdXaLnkYlYuM(@fm)Jk%qKD@L3ZH8e9w"
    "ujM#3s8l6VbW6lR2shEE=)*bJm;c7sO$zWid*qes4yIB(-emUz?RdsbvMJN_;z4~_>f;APhhj2<@"
    "z;<@z_0pZrIlpRWaYYeJxirH*7pU<b$<LU<UP|A!OH7i<AVF!c$#l^+d)m62_s0<E<N&8RwsoX{y"
    "1~|FAynI$3JR%uUu2$VgnKp^tcQ^CD*xGl1_aoSTqu|3ePG^fJ^$7s}?0f<EQP%3e6ui2I+Gk}0;"
    "{YGe0X`{JzkUJxTd?&N;5C5#6L{^Ln(WtMd%mUW%7Xc}+EMa1j!_zi"
)


def viewport(image):
    width, height = image.size
    if not width or not height or abs(width / height / (16 / 9) - 1) > 0.03:
        return None
    return image.convert("RGB").resize(VIEWPORT, Image.Resampling.BILINEAR)


def _skill_bar_full(rgb, x, bar_y):
    """Test one 49x5 skill bar using signed arithmetic on the supplied frame."""
    bar = np.asarray(rgb[bar_y - 2:bar_y + 3, x - 24:x + 25], dtype=np.int16)
    if bar.shape != (5, 49, 3):
        return False
    red, green, blue = bar.transpose(2, 0, 1)
    cyan = (blue > 164) & (green > 105) & (blue - red > 65) & (green - red > 50)
    return cyan.mean() >= 0.92


def detect_hero_skills(rgb):
    """Return ready Hero indices 0..8 from the current normalized frame only."""
    return [index for index, (_, x, bar_y, _) in enumerate(HERO_SKILLS)
            if _skill_bar_full(rgb, x, bar_y)]


def region(array, box):
    left, top, right, bottom = box
    return array[top:bottom, left:right]


def pale_pixels(rgb, minimum):
    return (rgb.max(axis=2) > minimum) & (np.ptp(rgb, axis=2) < 38)


def _box_sum_2d(integral, height, width):
    return (
        integral[height:, width:]
        - integral[:-height, width:]
        - integral[height:, :-width]
        + integral[:-height, :-width]
    )


@dataclass(frozen=True)
class Screen:
    name: str
    point: tuple[int, int] | None = None
    ready: tuple[int, ...] = ()
    gold: np.ndarray | None = None
    gems: int | None = None
    layout: str | None = None
    level: int | None = None
    cost: int | None = None
    bulk_cost: int | None = None
    mimic: tuple[int, int] | None = None
    skip_30: bool = False
    skip_options: tuple[tuple[int, tuple[int, int]], ...] = ()
    smith_ready: bool = False
    hp_low: bool = False
    raw_point: bool = False
    keyevent: int | None = None
    boss_visible: bool = False
    hero: str | None = None
    hero_entry: tuple[int, int] | None = None
    close_point: tuple[int, int] | None = None
    bulk_point: tuple[int, int] | None = None


@dataclass(frozen=True, kw_only=True)
class SkillAction(Action):
    slot: int | str


class Vision:
    def __init__(self, upgrade_tower=True, *, skip_tier=0, track_boss=False, hero_point=None):
        self.skip_tier = skip_tier
        self.track_boss = track_boss
        atlas_path = Path(__file__).with_name("play_templates.npz")
        with np.load(atlas_path, allow_pickle=False) as atlas:
            self.templates = {name: atlas[name].astype(np.int16) for name in TEMPLATES}
        self.zero = pale_pixels(self.templates["hp_zero"], 145)
        # The progress bar behind the WAVE label changes from white to black.
        self.wave_text = np.mean(np.abs(self.templates["wave"] - self.templates["wave_late"]),
                                 axis=2) < 14
        self.reward = Image.fromarray(self.templates["reward"].astype(np.uint8)).convert("L")
        light_reward = Image.fromarray(self.templates["reward_light"].astype(np.uint8)).convert("L")
        self.reward_light = ImageOps.invert(light_reward)
        self.reward_mixed = Image.fromarray(self.templates["reward_mixed"].astype(np.uint8)).convert("L")
        self.boss_kernels = self._build_boss_kernels()
        self.mimic_templates = self._build_mimic_templates()
        self.gem_templates = load_gem_templates()
        self.skip_masks = self._build_skip_masks()
        self.skip_30_mask = self.skip_masks[30]
        self.ad_closer = AdCloser(atlas_path)
        self.close_video = CloseVideoDetector()
        self.hero_vision = HeroVision(hero_point) if hero_point is not None else None
        self.tower = None
        if upgrade_tower:
            from tower_vision import TowerVision

            self.tower = TowerVision()

    @staticmethod
    def _build_skip_masks():
        total = SKIP_30_MASK_SHAPE[0] * SKIP_30_MASK_SHAPE[1]
        masks = {}
        for tier, b85 in SKIP_MASKS_B85.items():
            raw = zlib.decompress(base64.b85decode(b85))
            masks[tier] = (
                np.unpackbits(np.frombuffer(raw, dtype=np.uint8))[:total]
                .reshape(SKIP_30_MASK_SHAPE)
                .astype(bool)
            )
        return masks

    @classmethod
    def _build_skip_30_mask(cls):
        return cls._build_skip_masks()[30]

    def white_gems(self, rgb):
        return read_white_gems(rgb, self.gem_templates)

    def detect_skip_options(self, rgb, *, target_tier=0) -> tuple[tuple[int, tuple[int, int]], ...]:
        height, width = SKIP_30_MASK_SHAPE
        # Try the chosen tier first at its usual location. A high-confidence
        # label match is still required; never spend based on position alone.
        if target_tier in SKIP_TIERS:
            preferred = SKIP_SLOTS[SKIP_TIERS.index(target_tier)]
            for bx1, bx2, gx, center in (preferred, *(slot for slot in SKIP_SLOTS if slot != preferred)):
                button = rgb[316:415, bx1:bx2]
                red, green, blue = button[:, :, 0], button[:, :, 1], button[:, :, 2]
                gold = (red > 180) & (green > 115) & (blue < 75) & (red.astype(np.int16) - blue.astype(np.int16) > 120)
                cost_area = rgb[426:460, bx1:bx2]
                cr, cg, cb = cost_area[:, :, 0], cost_area[:, :, 1], cost_area[:, :, 2]
                cyan = (cb > 170) & (cg > 130) & (cb.astype(np.int16) - cr.astype(np.int16) > 60)
                if gold.mean() < 0.42 or cyan.mean() < 0.04:
                    continue
                mask = self.skip_masks[target_tier]
                for dy in (0, -1, 1, -2, 2, -3, 3):
                    for dx in (0, -1, 1, -2, 2, -3, 3):
                        sub = rgb[335 + dy:335 + dy + height, gx + dx:gx + dx + width]
                        observed = (sub.min(axis=2) > 175) & (np.ptp(sub, axis=2) < 42)
                        union = np.count_nonzero(observed | mask)
                        lead_union = np.count_nonzero(observed[:, :36] | mask[:, :36])
                        if not union or not lead_union:
                            continue
                        score = np.count_nonzero(observed & mask) / union
                        lead = np.count_nonzero(observed[:, :36] & mask[:, :36]) / lead_union
                        if score < 0.88 or lead < 0.80:
                            continue
                        rivals = []
                        for tier, other in self.skip_masks.items():
                            if tier != target_tier:
                                other_union = np.count_nonzero(observed | other)
                                rivals.append(np.count_nonzero(observed & other) / other_union if other_union else 0)
                        if all(score >= other + 0.08 for other in rivals):
                            return ((target_tier, center),)
        # Unclear labels take the original, more exhaustive matching path.
        found: dict[int, tuple[int, int]] = {}
        for bx1, bx2, gx, center in SKIP_SLOTS:
            button = rgb[316:415, bx1:bx2]
            red, green, blue = button[:, :, 0], button[:, :, 1], button[:, :, 2]
            gold = (red > 180) & (green > 115) & (blue < 75) & (red.astype(np.int16) - blue.astype(np.int16) > 120)
            if gold.mean() < 0.42:
                continue
            cost_area = rgb[426:460, bx1:bx2]
            cr, cg, cb = cost_area[:, :, 0], cost_area[:, :, 1], cost_area[:, :, 2]
            cyan = (cb > 170) & (cg > 130) & (cb.astype(np.int16) - cr.astype(np.int16) > 60)
            if cyan.mean() < 0.04:
                continue
            best_tier = None
            best_score = 0.0
            for tier, mask in self.skip_masks.items():
                lead_mask = mask[:, :36]
                for dy in range(-3, 4):
                    for dx in range(-3, 4):
                        sub = rgb[335 + dy:335 + dy + height, gx + dx:gx + dx + width]
                        observed = (sub.min(axis=2) > 175) & (np.ptp(sub, axis=2) < 42)
                        denom_all = np.count_nonzero(observed | mask)
                        denom_lead = np.count_nonzero(observed[:, :36] | lead_mask)
                        if denom_all > 0 and denom_lead > 0:
                            iou_all = np.count_nonzero(observed & mask) / denom_all
                            iou_lead = np.count_nonzero(observed[:, :36] & lead_mask) / denom_lead
                            if iou_all >= 0.72 and iou_lead >= 0.65 and iou_all > best_score:
                                best_score = iou_all
                                best_tier = tier
            if best_tier is not None and best_tier not in found:
                found[best_tier] = center
        return tuple(sorted(found.items()))

    def matches_skip_30(self, rgb):
        return any(tier == 30 for tier, _ in self.detect_skip_options(rgb))

    @staticmethod
    def _build_boss_kernels():
        white_base = Image.fromarray(
            np.array([[255 if ch == "#" else 0 for ch in row] for row in BOSS_GLYPH], dtype=np.uint8),
            "L",
        )
        dark_base = Image.fromarray(
            np.array([[255 if ch == "@" else 0 for ch in row] for row in BOSS_GLYPH], dtype=np.uint8),
            "L",
        )
        kernels = []
        for scale in (0.95, 1.10, 1.25, 1.38, 1.48):
            size = (round(white_base.width * scale), round(white_base.height * scale))
            white = np.asarray(white_base.resize(size, Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
            dark = np.asarray(dark_base.resize(size, Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
            mask = ((white > 0.45) | (dark > 0.45)).astype(np.float32)
            target = np.where(white > dark, 1.0, 0.0).astype(np.float32)
            m_sum = float(mask.sum())
            t_mean = float((target * mask).sum() / m_sum)
            k_zero = (target - t_mean) * mask
            k_norm = float(np.sqrt(np.sum(k_zero * k_zero)))
            k_fft = np.fft.rfft2(k_zero[::-1, ::-1], s=BOSS_FFT_SHAPE)
            m_fft = np.fft.rfft2(mask[::-1, ::-1], s=BOSS_FFT_SHAPE)
            kernels.append((size[1], size[0], k_fft, m_fft, m_sum, k_norm))
        return tuple(kernels)

    def matches_boss(self, rgb):
        # min(R, G, B) maps white BOSS strokes high (~250) and both black outline (~15)
        # and red/empty HP fill (~20..77) low, while ignoring unmasked fill pixels.
        patch = region(rgb, BOSS_BAR_REGION).min(axis=2).astype(np.float32)
        if patch.max() - patch.min() < 70:
            return False
        p_fft = np.fft.rfft2(patch, s=BOSS_FFT_SHAPE)
        p2_fft = np.fft.rfft2(patch * patch, s=BOSS_FFT_SHAPE)
        ph, pw = patch.shape
        for kh, kw, k_fft, m_fft, m_sum, k_norm in self.boss_kernels:
            numer = np.fft.irfft2(p_fft * k_fft, s=BOSS_FFT_SHAPE)[kh - 1:ph, kw - 1:pw]
            w_sum = np.fft.irfft2(p_fft * m_fft, s=BOSS_FFT_SHAPE)[kh - 1:ph, kw - 1:pw]
            w_sum2 = np.fft.irfft2(p2_fft * m_fft, s=BOSS_FFT_SHAPE)[kh - 1:ph, kw - 1:pw]
            w_std = np.sqrt(np.maximum(0.0, w_sum2 - (w_sum * w_sum) / m_sum))
            valid = w_std >= 28.0 * np.sqrt(m_sum)
            if not np.any(valid):
                continue
            corr = np.where(valid, numer / (w_std * k_norm + 1e-6), -1.0)
            if corr.max() >= 0.78:
                return True
        return False

    @staticmethod
    def _build_mimic_templates():
        raw = zlib.decompress(base64.b85decode(MIMIC_TEMPLATE_B85))
        rgb28 = np.frombuffer(raw[:28 * 28 * 3], dtype=np.uint8).reshape((28, 28, 3))
        mask28 = np.unpackbits(np.frombuffer(raw[28 * 28 * 3:], dtype=np.uint8))[:28 * 28].reshape((28, 28))
        pil_rgb = Image.fromarray(rgb28, "RGB")
        pil_msk = Image.fromarray((mask28 * 255).astype(np.uint8), "L")
        templates = []
        for scale in (0.78, 0.92, 1.08, 1.25, 1.45):
            sz = max(14, round((56.0 / 3.0) * scale))
            trgb = np.asarray(pil_rgb.resize((sz, sz), Image.Resampling.BILINEAR), dtype=np.float32)
            tmsk = (np.asarray(pil_msk.resize((sz, sz), Image.Resampling.BILINEAR)) > 175).astype(np.float32)
            tm = trgb * tmsk[:, :, None]
            tm_norm2 = float(np.sum(tm * tm))
            m_count = float(tmsk.sum() * 3.0)
            templates.append((sz, tmsk, tm, tm_norm2, m_count))
        return tuple(templates)

    def find_mimic(self, rgb):
        y0, x0 = 88, 24
        sub = rgb[y0:685:3, x0:1257:3]
        r, g, b = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
        tongue = (r > 155) & (g > 65) & (g < 160) & (r - g > 48) & (r - b > 15) & (b - g > 8)
        gums = (r > 90) & (b > 100) & (g < 85) & (b - g > 38) & (r - g > 30)
        win = 22
        s_gums = _box_sum_2d(np.pad(gums.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1), win, win)
        if s_gums.max() < 2:
            return None
        s_tongue = _box_sum_2d(np.pad(tongue.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1), win, win)
        if s_tongue.max() < 3:
            return None
        wood = (r > 118) & (g > 52) & (b < 75) & (r - b > 75) & (g - b > 18)
        s_wood = _box_sum_2d(np.pad(wood.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1), win, win)
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        white = (mn > 185) & (mx - mn < 40)
        s_white = _box_sum_2d(np.pad(white.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1), win, win)
        hand_shifted = np.pad(s_white, ((0, 8), (0, 8)))[4:4 + s_tongue.shape[0], 4:4 + s_tongue.shape[1]]

        cand = (s_gums >= 2) & (s_tongue >= 3) & (s_wood >= 14) & (hand_shifted >= 4)
        if not np.any(cand):
            return None

        ys, xs = np.where(cand)
        scores = (s_tongue[ys, xs] + s_gums[ys, xs]) * 3 + s_wood[ys, xs]
        order = np.argsort(scores)[::-1]
        checked = []
        best_rmse = 36.0
        best_pt = None
        sh, sw, _ = sub.shape
        sub_f = sub.astype(np.float32)
        sub_f2 = np.sum(sub_f * sub_f, axis=2)
        for idx in order:
            cy, cx = int(ys[idx]), int(xs[idx])
            if any(abs(cy - py) < 12 and abs(cx - px) < 12 for py, px in checked):
                continue
            checked.append((cy, cx))
            for sz, tmsk, tm, tm_norm2, m_count in self.mimic_templates:
                ry0 = max(0, cy - 7)
                rx0 = max(0, cx - 7)
                ry1 = min(sh, cy + 8 + sz)
                rx1 = min(sw, cx + 8 + sz)
                if ry1 - ry0 < sz or rx1 - rx0 < sz:
                    continue
                roi = sub_f[ry0:ry1, rx0:rx1]
                roi2 = sub_f2[ry0:ry1, rx0:rx1]
                w_rgb = np.lib.stride_tricks.sliding_window_view(roi, (sz, sz, 3))[:, :, 0]
                w_p2 = np.lib.stride_tricks.sliding_window_view(roi2, (sz, sz))
                p_energy = np.tensordot(w_p2, tmsk, axes=((-2, -1), (0, 1)))
                cross = np.tensordot(w_rgb, tm, axes=((-3, -2, -1), (0, 1, 2)))
                mse = np.maximum(0.0, (p_energy - 2.0 * cross + tm_norm2) / m_count)
                min_idx = np.unravel_index(np.argmin(mse), mse.shape)
                rmse = float(np.sqrt(mse[min_idx]))
                if rmse < best_rmse:
                    best_rmse = rmse
                    py = ry0 + int(min_idx[0])
                    px = rx0 + int(min_idx[1])
                    best_pt = (x0 + (px + sz // 2) * 3, y0 + (py + sz // 2) * 3)
            if len(checked) >= 3:
                break
        return best_pt

    def matches(self, rgb, name, offset_y=0):
        box, tolerance = TEMPLATES[name]
        if offset_y:
            left, top, right, bottom = box
            box = (left, top + offset_y, right, bottom + offset_y)
        template = self.templates[name]
        difference = np.abs(region(rgb, box) - template)
        if name != "wave":
            return np.mean(difference) < tolerance
        if np.mean(difference[self.wave_text]) < tolerance:
            return True
        # Native ADB text can be one pixel lower and sharper than the MP4
        # template. Allow a small margin for its anti-aliasing while still
        # requiring the WAVE glyph, HUD and speed button to match.
        left, top, right, bottom = box
        shifted = region(rgb, (left, top + 1, right, bottom + 1))
        return np.mean(np.abs(shifted - template)[self.wave_text]) < 23

    def hp_empty(self, rgb, dim):
        threshold = 53 if dim else 145
        left, top, right, bottom = TEMPLATES["hp_zero"][0]
        if not any(np.count_nonzero(pale_pixels(rgb[top + dy:bottom + dy, left:right],
                                                threshold) ^ self.zero) <= 42
                   for dy in (0, 1)):
            return False
        bar = rgb[8:28, 657:917]
        red, green, blue = bar.transpose(2, 0, 1)
        health = (red > (65 if dim else 130)) & (red - blue > (35 if dim else 80))
        return health.sum() < 6 and pale_pixels(bar, threshold).sum() < 8

    @staticmethod
    def castle_hp_low(rgb):
        """Return True when un-dimmed Castle HP bar (x=615..937, 50% at x=776) is below 50%."""
        if (rgb[8:28, 616:645].min(axis=2) > 175).mean() < 0.16:
            return False
        mid_bar = rgb[10:25, 776:788]
        red, _, blue = mid_bar.transpose(2, 0, 1)
        return float(((red > 140) & (red - blue > 70)).mean()) < 0.15

    @staticmethod
    def smith_skill_ready(rgb):
        """Check cyan skill bar for top-row middle Hero (Smith at x=373, y_bar≈51)."""
        _, x, _, _ = SMITH_SKILL
        sr, sg, sb = rgb[44:59, x - 24:x + 25].transpose(2, 0, 1)
        cyan = (sb > 164) & (sg > 105) & (sb - sr > 65) & (sg - sr > 50)
        return any(
            float(cyan[dy:dy + 4].mean()) >= 0.90
            for dy in range(cyan.shape[0] - 3)
        )

    def reward_close(self, image):
        # Search only the player chrome for the full label and the adjoining X.
        left, top = 902, 8
        chrome = image.crop((left, top, 1280, 106)).convert("L")
        point = self.match_reward(chrome, self.reward, 168, (182, 14))
        if point is None:
            point = self.match_reward(ImageOps.invert(chrome), self.reward_light, 143, (156, 12))
        if point is None:
            point = self.match_reward(chrome, self.reward_mixed, 163, (183, 16), dark_x=True)
        if point is not None:
            return left + point[0], top + point[1]
        return None

    @staticmethod
    def match_reward(chrome, template, close_start, close_center, dark_x=False):
        field = np.asarray(chrome) > 165
        fft_size = (128, 640)
        field_fft = np.fft.rfft2(field.astype(np.float32), s=fft_size, axes=(0, 1))
        integral = np.pad(field.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
        for scale in (1.0, 0.85, 1.15):
            size = (round(template.width * scale), round(template.height * scale))
            mask = np.asarray(template.resize(size, Image.Resampling.BILINEAR)) > 165
            height, width = mask.shape
            kernel = np.fft.rfft2(mask.astype(np.float32), s=fft_size, axes=(0, 1))
            overlap = np.fft.irfft2(field_fft * kernel.conj(), s=fft_size, axes=(0, 1))
            count = (integral[height:, width:] - integral[:-height, width:]
                     - integral[height:, :-width] + integral[:-height, :-width])
            score = 2 * overlap[:count.shape[0], :count.shape[1]] / (count + mask.sum())
            y, x = np.unravel_index(score.argmax(), score.shape)
            if score[y, x] < 0.83:
                continue
            x_start = round(close_start * scale)
            close = field[y:y + height, x + x_start:x + width]
            expected = mask[:, x_start:]
            if 2 * (close & expected).sum() < 0.76 * (close.sum() + expected.sum()):
                continue
            cx, cy = (round(coordinate * scale) for coordinate in close_center)
            if dark_x:
                # The bright circle alone is a match. Check the dark X inside it.
                radius = round(8 * scale)
                glyph = ~field[y + cy-radius:y + cy+radius+1, x + cx-radius:x + cx+radius+1]
                expected_x = ~mask[cy-radius:cy+radius+1, cx-radius:cx+radius+1]
                if 2 * (glyph & expected_x).sum() < 0.76 * (glyph.sum() + expected_x.sum()):
                    continue
            return int(x + cx), int(y + cy)
        return None

    def ad_end_close(self, rgb):
        # The two-card end screen replaces the playback progress and ad count
        # with an empty header and a standalone close button.
        if not self.matches(rgb, "ad_end_close"):
            return None
        header = rgb[:69, :1228]
        if np.max(np.mean(np.abs(header - header[0, 0]), axis=(1, 2))) > 8:
            return None
        template = self.templates["ad_end_cta"]
        height, width, _ = template.shape
        tolerance = TEMPLATES["ad_end_cta"][1]
        for left in (247, 888):
            if not any(np.mean(np.abs(rgb[top:top + height, left:left + width] - template))
                       < tolerance for top in range(578, 680)):
                return None
        return 1257, 42

    def home_screen(self, rgb):
        gold = region(rgb, GOLD_HUD)
        gems, layout = self.tower.home(rgb) if self.tower else (None, None)
        hero_entry = None
        if self.hero_vision is not None:
            gems, hero_entry = self.hero_vision.home(rgb, verified_home=True)
            if layout == "castle":
                hero_entry = None
        mimic = self.find_mimic(rgb)
        # The offer slides a few pixels as it appears after the results screen.
        for offset in (0, *range(-6, 0), *range(1, 7)):
            if self.matches(rgb, "gold_coin", offset) and (
                    self.matches(rgb, "gold_get", offset)
                    or self.matches(rgb, "gold_get_centered", offset)):
                return Screen(
                    "gold", (626, 649 + offset),
                    gold=gold, gems=gems, layout=layout, mimic=mimic, hero_entry=hero_entry,
                )
        return Screen("home", gold=gold, gems=gems, layout=layout, mimic=mimic, hero_entry=hero_entry)

    def captcha_start_visible(self, image):
        frame = viewport(image)
        if frame is None:
            return False
        rgb = np.asarray(frame, dtype=np.int16)
        return self.matches(rgb, "captcha") and self.matches(rgb, "captcha_start")

    def _classify_game_screen(self, rgb):
        if self.matches(rgb, "captcha"):
            return Screen("captcha")
        if self.matches(rgb, "processing"):
            return Screen("processing")
        if self.matches(rgb, "victory"):
            return Screen("victory")
        if self.hero_vision is not None and (hero_panel := self.hero_vision.panel(rgb)):
            return Screen(**hero_panel)
        panel = self.tower.panel(rgb) if self.tower is not None else None
        if panel is not None and panel[0] == "tower":
            kind, gems, level, cost, bulk_cost = panel
            return Screen(kind, gems=gems, level=level, cost=cost, bulk_cost=bulk_cost)
        # ADB captures and emulator scaling can shift the UI vertically by a
        # few pixels. Require the HUD and battle markers to agree on the same
        # offset rather than relaxing their matching thresholds.
        hud_offsets = tuple(dy for dy in (0, -1, 1, -2, 2, -3, 3)
                            if self.matches(rgb, "hud", dy))
        hud = bool(hud_offsets)
        dim = not hud and self.matches(rgb, "hud_dim")
        if (hud or dim) and self.hp_empty(rgb, dim):
            return Screen("defeat")
        if hud and any(self.matches(rgb, "skip_title", dy) and self.matches(rgb, "skip_close", dy)
                       for dy in hud_offsets):
            options = self.detect_skip_options(rgb, target_tier=self.skip_tier)
            return Screen(
                "skip",
                SKIP_CLOSE_POINT,
                gems=self.white_gems(rgb),
                skip_30=any(tier == 30 for tier, _ in options),
                skip_options=options,
            )
        if hud and self.matches(rgb, "upgrade"):
            if self.matches(rgb, "replay_100") and self.matches(rgb, "replay_80"):
                return Screen("replay_menu", (857, 623))
            if self.matches(rgb, "battle") and self.matches(rgb, "replay"):
                return self.home_screen(rgb)
        if self.tower is not None and self.tower.is_home(rgb):
            return self.home_screen(rgb)
        if self.hero_vision is not None and self.hero_vision.is_home(rgb):
            return self.home_screen(rgb)
        if panel is not None:
            kind, gems, level, cost, bulk_cost = panel
            return Screen(kind, gems=gems, level=level, cost=cost, bulk_cost=bulk_cost)
        speed_offsets = tuple(dy for dy in hud_offsets if self.matches(rgb, "speed", dy))
        if speed_offsets:
            wave_visible = any(self.matches(rgb, "wave", dy) for dy in speed_offsets)
            boss_visible = (self.track_boss or not wave_visible) and self.matches_boss(rgb)
            if not (wave_visible or boss_visible):
                return Screen("unknown")
            archers = [index for index, (_, x, bar_y, _) in enumerate(ARCHER_SKILLS)
                       if _skill_bar_full(rgb, x, bar_y)]
            heroes = [index + len(ARCHER_SKILLS) for index in detect_hero_skills(rgb)]
            return Screen(
                "battle",
                ready=tuple(archers + heroes),
                mimic=self.find_mimic(rgb),
                smith_ready=self.smith_skill_ready(rgb),
                hp_low=self.castle_hp_low(rgb),
                boss_visible=boss_visible,
            )
        return Screen("unknown")

    def classify(
        self,
        image,
        watching_ad=False,
        ad_foreground=False,
        *,
        original=None,
        time=0.0,
        adb=None,
        serial=None,
    ):
        rgb = np.asarray(image, dtype=np.int16) if image is not None else None
        if rgb is not None and (watching_ad or ad_foreground):
            if buttons := self.close_video.buttons(rgb):
                close, resume = buttons
                if self.close_video.reward_visible(rgb) or self.reward_close(image) is not None:
                    return Screen("ad_close_video", close)
                return Screen("ad_resume", resume)
            if point := self.reward_close(image):
                return Screen("reward_granted", point)
            if point := self.ad_end_close(rgb):
                return Screen("ad_end_card", point)
            if all(self.matches(rgb, name)
                   for name in ("ad_sheet_close", "ad_sheet_chevron", "ad_sheet_cta")):
                return Screen("ad_end_sheet", (1257, 39))

        if not ad_foreground and rgb is not None:
            game = self._classify_game_screen(rgb)
            if game.name != "unknown":
                self.ad_closer.reset()
                return game

        if watching_ad or ad_foreground:
            raw_frame = original if original is not None else image
            sig = self.ad_closer.inspect(
                time,
                raw_frame,
                adb=adb,
                serial=serial,
                ad_foreground=ad_foreground,
            )
            if sig is not None:
                return Screen(sig.kind, sig.point, raw_point=sig.raw_point, keyevent=sig.keyevent)
            return Screen("ad_playing" if ad_foreground else "unknown")

        self.ad_closer.reset()
        return Screen("unknown")


class Player:
    def __init__(
        self,
        mode,
        report,
        tower=None,
        auto_captcha=False,
        *,
        watch_ads=True,
        skip_waves=False,
        humanize=False,
        rng=None,
        boss_skill=None,
        boss_altar_skill=False,
        hero_upgrade=None,
    ):
        if mode in {"skip", "skip10", "skip20", "skip30"}:
            mode_tier = SKIP_GEM_THRESHOLD if mode == "skip" else int(mode[4:])
            mode = "wave"
            if not skip_waves or skip_waves is True:
                skip_waves = mode_tier
        if skip_waves is True:
            skip_tier = SKIP_GEM_THRESHOLD
        elif skip_waves in (False, None, 0):
            skip_tier = 0
        elif type(skip_waves) is int and skip_waves in SKIP_TIERS:
            skip_tier = skip_waves
        else:
            raise ValueError(f"Unsupported Wave Skip tier: {skip_waves!r} (choose 10, 20, or 30)")
        self.mode = mode
        self.skip_waves = skip_tier
        self.humanize = humanize
        self.rng = rng if rng is not None else random.SystemRandom()
        self.report = report
        self.tower = tower
        self.hero_upgrade = hero_upgrade
        self.auto_captcha = auto_captcha
        self.watch_ads = watch_ads
        self.screen = "unknown"
        self.screen_target = None
        self.since = 0
        self.acted = False
        self.wait_notified = False
        self.paused = False
        self.ad = None
        self.ad_since = 0
        self.ad_warned = False
        self.reward_label_seen = False
        self.ad_close_attempts = {}
        self.last_ad_resume = float("-inf")
        self.ad_taps = 0
        self.skip_offer = False
        self.gold_before = None
        self.after_victory = False
        self.wave_hero_wait_pending = False
        self.skills_used = {}
        self.last_skill = float("-inf")
        self.last_mimic = float("-inf")
        self.skill_order = tuple(index for index in range(len(SKILLS)) if index != boss_skill)
        self.boss_skill_order = self.skill_order
        if boss_skill is not None:
            position = min(boss_skill, len(ARCHER_SKILLS))
            self.boss_skill_order = (*self.skill_order[:position], boss_skill, *self.skill_order[position:])
        self.boss_altar_skill = bool(boss_altar_skill)
        self.boss_altar_used = False
        self.mimic_taps = 0
        self.battle_delay = 0.45
        self.home_delay = 0.85
        self.menu_delay = 0.20
        self.victory_wait = 1.60
        self.waves_completed = 0
        self.in_battle = False
        self.next_break_waves = self.rng.randint(18, 32)
        self.break_logged = False

    def _roll_reaction_delays(self, prev_screen, new_screen):
        self.battle_delay = 0.45
        if not self.humanize:
            self.home_delay = 0.85
            self.menu_delay = 0.20
            self.victory_wait = 1.60
            return
        self.menu_delay = self.rng.uniform(0.24, 0.56)
        self.victory_wait = self.rng.uniform(1.60, 2.25)
        if new_screen == "battle":
            self.in_battle = True
        elif new_screen in {"home", "gold"}:
            if self.in_battle:
                self.waves_completed += 1
                self.in_battle = False
            self.break_logged = False
            if self.waves_completed >= self.next_break_waves:
                self.home_delay = self.rng.uniform(4.0, 8.5)
                self.next_break_waves = self.waves_completed + self.rng.randint(18, 32)
            elif self.rng.random() < 0.08:
                self.home_delay = self.rng.uniform(1.90, 3.30)
            else:
                self.home_delay = self.rng.uniform(0.88, 1.65)

    def collect_mimic(self, time, point):
        if point is None or self.mimic_taps >= 3 or time - self.last_mimic < 1.2:
            return None
        self.last_mimic = time
        self.mimic_taps += 1
        return Action("collect Mimic chest", point)

    def start_ad(self, time, gold=None):
        self.ad, self.ad_since, self.ad_warned = "watch", time, False
        self.gold_before = gold
        self.reward_label_seen = False
        self.ad_close_attempts.clear()
        self.last_ad_resume = float("-inf")
        self.ad_taps = 0

    def feed_ad(self, time, screen, age):
        name = screen.name
        if name == "ad_playing":
            if self.ad == "watch":
                self.ad = "playing"
            if age >= 1.5:
                self.ad_close_attempts.clear()
        elif name == "ad_resume":
            if age >= 0.15 and self.ad_taps < 12 and time - self.last_ad_resume >= 5.0:
                self.ad = "playing"
                self.ad_close_attempts.clear()
                self.last_ad_resume = time
                self.ad_taps += 1
                return Action("resume ad for reward", screen.point, raw_point=screen.raw_point)
        elif name in AD_CLOSE_SCREENS:
            self.ad = "return"
            self.reward_label_seen |= name in {"reward_granted", "ad_close_video"}
            if screen.keyevent is not None:
                attempt_key = (name, "key", screen.keyevent)
            elif screen.point is not None and name in {"ad_ui_close", "ad_visual_close"}:
                attempt_key = (name, screen.point[0] // 24, screen.point[1] // 24)
            else:
                attempt_key = name
            max_attempts = 2 if name == "ad_back" else 3
            cooldown = 4.0 if name == "ad_back" else (2.0 if name == "ad_external_overlay" else 1.5)
            min_age = 0.60 if name == "ad_external_overlay" else 0.20
            attempts, last_tap = self.ad_close_attempts.get(attempt_key, (0, float("-inf")))
            if age >= min_age and attempts < max_attempts and self.ad_taps < 12 and time - last_tap >= cooldown:
                self.ad_close_attempts[attempt_key] = (attempts + 1, time)
                self.ad_taps += 1
                if name == "reward_granted":
                    label = "close granted ad"
                elif name == "ad_close_video":
                    label = "close video"
                elif name == "ad_external_overlay":
                    label = "dismiss ad overlay (BACK)"
                elif name == "ad_back":
                    label = "close finished ad (BACK)"
                else:
                    label = "close ad end card"
                return Action(
                    label,
                    screen.point,
                    raw_point=screen.raw_point,
                    keyevent=screen.keyevent,
                )
        elif name in RETURN_SCREENS and age >= 0.55:
            if self.ad == "return":
                changed = (screen.gold is not None and self.gold_before is not None
                           and np.mean(np.abs(screen.gold - self.gold_before)) > 6)
                message = "gold HUD changed" if changed else "gold HUD change unconfirmed"
                result = "Reward granted" if self.reward_label_seen else "Ad end card closed"
                self.report(time, f"{result}, returned to game ({message})")
                self.ad = None
            elif self.ad == "playing" or time - self.ad_since >= 12:
                self.report(time, "Game returned without a recognized reward")
                self.ad = None
                self.acted = False
        if self.ad and not self.ad_warned and time - self.ad_since >= 90:
            self.report(time, "Ad still open: waiting for recognized reward or manual close")
            self.ad_warned = True
        return None

    def feed(self, time, screen, *, ignore_skill=None):
        if screen.name == "gold" and not self.watch_ads:
            # The inline offer leaves the home controls and tower slots available.
            screen = replace(screen, name="home")
        name = screen.name
        if name != "battle" or self.ad is not None or self.paused:
            self.interrupt_skill_frames()
        target_bucket = (
            (screen.point[0] // 24, screen.point[1] // 24)
            if screen.point is not None and name in AD_CLOSE_SCREENS
            else None
        )
        if name != self.screen or target_bucket != self.screen_target:
            prev_screen = self.screen
            self.screen, self.screen_target, self.since = name, target_bucket, time
            self.acted = False
            self.wait_notified = False
            self._roll_reaction_delays(prev_screen, name)
        age = time - self.since
        if self.ad is None and (name in {"ad_playing", "ad_resume"} or name in AD_CLOSE_SCREENS):
            self.start_ad(time)
            self.skip_offer = True
        if self.ad:
            if self.tower is not None:
                self.tower.suspend()
            if self.hero_upgrade is not None:
                self.hero_upgrade.suspend()
            action = self.feed_ad(time, screen, age)
            if self.ad:
                return action
        if name == "captcha":
            if self.tower is not None:
                self.tower.suspend()
            if self.hero_upgrade is not None:
                self.hero_upgrade.suspend()
            if not self.paused:
                message = "automatic attempt" if self.auto_captcha else "manual solution"
                self.report(time, f"CAPTCHA: paused for {message}")
                self.paused = True
            return None
        if self.paused:
            if name not in RETURN_SCREENS or age < 0.45:
                return None
            self.report(time, "Game screen restored: resumed")
            self.paused = False
        if name == "defeat" and self.mode == "wave":
            self.mode = "replay"
            self.report(time, "Castle HP reached zero: switched to Replay")
        if name == "victory":
            self.skills_used.clear()
            self.boss_altar_used = False
            self.after_victory = self.watch_ads
        elif name == "battle":
            self.after_victory = False
            if self.mode == "wave" and (
                self.hero_upgrade is not None
                or (self.tower is not None and self.tower.target == "hero")
            ):
                self.wave_hero_wait_pending = True
        if name in {"home", "gold", "battle"} and screen.mimic is None:
            self.mimic_taps = 0
        if name in {"home", "battle"} and age >= 0.45:
            self.skip_offer = False
        if name in {"home", "gold"} and screen.mimic is not None and age >= self.menu_delay:
            if action := self.collect_mimic(time, screen.mimic):
                return action
        if name == "home" and self.after_victory:
            if age < self.victory_wait:
                return None
            self.after_victory = False
        if name == "gold" and not self.skip_offer:
            if age < self.menu_delay:
                return None
            self.after_victory = False
            self.start_ad(time, screen.gold)
            self.skip_offer = True
            self.acted = True
            return Action("open gold ad (GET)", screen.point)
        if name == "home" and self.mode == "wave" and self.wave_hero_wait_pending:
            # Observe without starting the upgrade; if a reading just changed,
            # allow it to stabilize before releasing the normal home actions.
            if self.tower is not None and self.tower.target == "hero" and not self.tower.active:
                self.tower.feed(time, screen, allow_start=False)
                if time - self.tower.since < self.tower.stable_delay:
                    return None
            if age < WAVE_HERO_HOME_WAIT:
                return None
            self.wave_hero_wait_pending = False
        if self.tower is not None and name in {"home", "tower", "castles", "unknown"}:
            try:
                handled, action = self.tower.feed(time, screen)
            except RuntimeError as exc:
                try:
                    handled, action = self.tower._abort_to_home(time, name, screen.layout, str(exc))
                except RuntimeError:
                    self.report(time, f"{exc} Exiting upgrade menu to resume play.")
                    self.tower = None
                    handled, action = False, None
            if handled:
                return action
        elif self.tower is not None:
            self.tower.suspend()
        if self.hero_upgrade is not None:
            if name in HERO_MENUS or name in {"home", "unknown"}:
                try:
                    handled, action = self.hero_upgrade.feed(time, screen)
                except RuntimeError as exc:
                    self.report(time, f"{exc}. Hero upgrades disabled for this run.")
                    self.hero_upgrade = None
                else:
                    if handled:
                        return action
            else:
                self.hero_upgrade.suspend()
        if name == "unknown" and age >= 3 and not self.wait_notified:
            self.report(time, "Unknown screen: waiting for the main screen or a battle")
            self.wait_notified = True
        if name == "battle":
            if screen.mimic is not None and age >= 0.20:
                if action := self.collect_mimic(time, screen.mimic):
                    self.observe_skill_bars(screen.ready, smith_ready=screen.smith_ready, ignore_skill=ignore_skill)
                    return action
            if age < 0.45:
                self.observe_skill_bars(screen.ready, smith_ready=screen.smith_ready, ignore_skill=ignore_skill)
                return None
            return self.cast(
                time,
                screen.ready,
                smith_ready=screen.smith_ready,
                hp_low=screen.hp_low,
                boss_visible=screen.boss_visible,
                ignore_skill=ignore_skill,
            )
        if self.acted:
            return None
        # The offer's own countdown is short: a verified requested tier should
        # be tapped on the first frame, not after the ordinary menu delay.
        urgent_skip = name == "skip" and self.skip_waves and self.mode == "wave"
        if age < self.menu_delay and not urgent_skip:
            return None
        if name == "skip":
            if self.skip_waves and self.mode == "wave":
                options = dict(screen.skip_options)
                if 30 not in options and screen.skip_30:
                    options[30] = SKIP_30_POINT
                target_point = options.get(self.skip_waves)
                if screen.gems is not None and screen.gems < self.skip_waves:
                    action = Action("close Wave Skip", screen.point)
                elif screen.gems is not None and target_point is not None:
                    action = Action(f"select Wave Skip {self.skip_waves}", target_point)
                else:
                    # A partially rendered dialog or unreadable gem counter is not
                    # evidence that Skip is unavailable. Keep sampling rather than
                    # closing the offer before its selected button can be verified.
                    if not self.wait_notified:
                        available = ",".join(str(tier) for tier in sorted(options)) or "none"
                        gems = str(screen.gems) if screen.gems is not None else "unreadable"
                        self.report(time, f"Wave Skip {self.skip_waves}: waiting for verification "
                                          f"(white gems: {gems}; detected buttons: {available})")
                        self.wait_notified = True
                    return None
            else:
                action = Action("close Wave Skip", screen.point)
        elif name == "tower" and age >= max(0.35, self.menu_delay):
            action = Action("close detail panel", DETAIL_CLOSE)
        elif name == "castles" and age >= max(0.35, self.menu_delay):
            action = Action("close side menu", LIST_CLOSE)
        elif name in HERO_MENUS and screen.close_point is not None:
            action = Action("close Hero menu", screen.close_point)
        elif name == "replay_menu" and self.mode == "replay":
            action = Action("start Replay 100%", screen.point)
        elif name in {"home", "gold"}:
            if screen.layout == "castle" and self.tower is None:
                if age < max(0.35, self.menu_delay):
                    return None
                action = Action("restore hero slots", EXIT_LAYER_VIEW)
            else:
                if self.humanize and self.home_delay >= 4.0 and not self.break_logged and age >= 0.85:
                    self.report(time, f"Short pause before next wave ({self.home_delay:.1f}s)")
                    self.break_logged = True
                if age < self.home_delay:
                    return None
                if (name == "home" and self.hero_upgrade is not None
                        and self.hero_upgrade.needs_home_reading(time, screen)):
                    if age < HOME_READING_SECONDS:
                        return None
                    self.report(time, "Hero balance or slot could not be verified on Home. Continuing autoplay.")
                self.skills_used.clear()
                self.boss_altar_used = False
                if self.mode == "wave":
                    action = Action("start next Battle", (1191, 648))
                else:
                    action = Action("open Replay menu", (1017, 648))
        else:
            return None
        self.acted = True
        return action

    def interrupt_skill_frames(self):
        # An unobserved bar cannot count toward two consecutive non-full frames.
        for index in self.skills_used:
            self.skills_used[index] = 0

    def invalidate_frame(self, time):
        self.interrupt_skill_frames()
        if self.hero_upgrade is not None:
            self.hero_upgrade.suspend()
        if self.screen == "battle":
            self.screen, self.screen_target, self.since = "unknown", None, time

    def observe_skill_bars(self, ready, *, smith_ready=False, ignore_skill=None):
        for index in tuple(self.skills_used):
            if index == ignore_skill:
                continue
            full = smith_ready if index == "smith" else index in ready
            self.skills_used[index] = 0 if full else self.skills_used[index] + 1
            if self.skills_used[index] >= 2:
                del self.skills_used[index]

    def cast(self, time, ready, *, smith_ready=False, hp_low=False, boss_visible=False, ignore_skill=None):
        self.observe_skill_bars(ready, smith_ready=smith_ready, ignore_skill=ignore_skill)
        if time - self.last_skill < SKILL_CAST_INTERVAL or time - self.last_mimic < 0.30:
            return None
        if boss_visible and self.boss_altar_skill and not self.boss_altar_used:
            self.boss_altar_used = True
            self.last_skill = time
            return SkillAction("cast altar skill", ALTAR_SKILL_POINT, slot="altar")
        if smith_ready and hp_low and "smith" not in self.skills_used:
            self.skills_used["smith"] = 0
            self.last_skill = time
            label, x, _, y = SMITH_SKILL
            return SkillAction(f"cast {label}", (x, y), slot="smith")
        order = self.boss_skill_order if boss_visible else self.skill_order
        for index in order:
            if index in ready and index not in self.skills_used:
                self.skills_used[index] = 0
                self.last_skill = time
                label, x, _, y = SKILLS[index]
                return SkillAction(f"cast {label}", (x, y), slot=index)
        return None


_PLAY_RNG = random.SystemRandom()


def action_jitter(action: Action) -> tuple[float, float]:
    """Return safe (rx, ry) hit-box jitter in 1280x720 viewport pixels."""
    if action.raw_point:
        return (2.0, 2.0)
    label = action.name
    if label in {"start next Battle", "open Replay menu"}:
        return (15.0, 11.0)
    if label.startswith("select Wave Skip "):
        return (15.0, 12.0)
    if label in {"start Replay 100%", "open gold ad (GET)"}:
        return (13.0, 6.5)
    if label == "restore hero slots":
        return (16.0, 9.0)
    if label.startswith("upgrade "):
        return (11.0, 6.0)
    return (5.0, 5.0)


class SkillTapShell:
    """Keep one ADB shell open and wait for each tap to finish before proceeding."""

    ACK = b"autogc-skill-ready"
    FAILED = b"autogc-skill-failed"

    def __init__(self):
        self.process = None
        self.reader = None
        self.replies = None
        self.available = True

    @staticmethod
    def _read_replies(output, replies):
        for line in output:
            reply = line.strip()
            if reply in (SkillTapShell.ACK, SkillTapShell.FAILED):
                replies.put(reply)
        replies.put(None)

    def _command(self, command):
        try:
            self.process.stdin.write(command)
            self.process.stdin.flush()
            return self.replies.get(timeout=2) == self.ACK
        except (OSError, Empty):
            return False

    def start(self, adb, serial):
        if not self.available or (self.process is not None and self.process.poll() is None):
            return
        self.close()
        try:
            self.process = subprocess.Popen(
                [str(adb), "-s", serial, "shell", "-T", "sh"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self.available = False
            return
        self.replies = Queue()
        self.reader = Thread(target=self._read_replies, args=(self.process.stdout, self.replies), daemon=True)
        self.reader.start()
        if not self._command(b"echo " + self.ACK + b"\n"):
            self.available = False
            self.close()

    def tap(self, adb, serial, x, y):
        if self.process is None:
            adb_run(adb, serial, "shell", "input", "tap", str(x), str(y))
            return True
        command = f"input tap {x} {y} && echo ".encode("ascii")
        if self._command(command + self.ACK + b" || echo " + self.FAILED + b"\n"):
            return True
        # The tap may already have reached the game. Do not replay it on another shell.
        self.available = False
        self.close()
        return False

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        with suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self.reader.join()
        process.stdout.close()


def capture_raw_screen(adb, serial):
    try:
        raw = adb_run(adb, serial, "exec-out", "screencap", timeout=2)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if len(raw) < 12:
        return None
    width, height, pixel_format = struct.unpack_from("<III", raw)
    header_size = len(raw) - width * height * 4
    if pixel_format != 1 or not width or not height or header_size not in (12, 16):
        return None
    return Image.frombytes("RGB", (width, height), raw[header_size:], "raw", "RGBX")


class BattleCapture:
    """Use a native RGB stream in battle and let ADB screenshots handle other screens."""

    def __init__(self, *, enabled=True):
        self.stream = None
        self.started_at = None
        self.available = enabled

    def next_frame(self, adb, serial, size):
        if not self.available or size is None:
            return None
        starting = self.stream is None
        if starting:
            try:
                self.stream = BattleFrameStream(adb, serial, size)
            except (OSError, ValueError):
                self.available = False
                return None
            self.started_at = monotonic()
        frame = self.stream.next_frame(timeout=0.85 if starting else 0.45)
        if frame is None:
            # Android screenrecord ends normally after 180 s. Restart after the
            # next ADB frame, but avoid probing a broken backend in every wave.
            self.available = monotonic() - self.started_at >= 179
            self.close()
        return frame

    def close(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.close()


def adb_frames(adb, serial, seconds, *, humanize=True, fast_battle=None, focus_worker=None,
               start=None, battle_capture=None, report=None, screenshot_interval=None):
    start = monotonic() if start is None else start
    raw_supported = True
    size = None
    announced_stream = False
    while not seconds or monotonic() - start < seconds:
        sample = monotonic()
        in_battle = fast_battle is not None and fast_battle()
        if not in_battle and battle_capture is not None:
            battle_capture.close()
        focus = focus_worker.submit(foreground_window, adb, serial) if in_battle and focus_worker else None
        captured_at = sample
        screen = None
        streamed = False
        if in_battle and battle_capture is not None and battle_capture.available and size is not None:
            frame = battle_capture.next_frame(adb, serial, size)
            if frame is not None:
                captured_at, screen = frame
                streamed = True
                if not announced_stream and report is not None:
                    report(captured_at - start, f"Battle capture: Android RGB stream ({size[0]}x{size[1]})")
                    announced_stream = True
            else:
                captured_at = monotonic()
                if focus is not None:
                    focus.cancel()
                    focus = focus_worker.submit(foreground_window, adb, serial)
                if not battle_capture.available and report is not None:
                    report(captured_at - start, "Battle capture: Android RGB stream unavailable; using ADB screenshots")
        if in_battle and screen is None and raw_supported:
            screen = capture_raw_screen(adb, serial)
            raw_supported = screen is not None
            if not raw_supported and focus is not None:
                # The PNG fallback may be captured long after the first window check.
                focus.cancel()
                focus = None
        if screen is None:
            raw = adb_run(adb, serial, "exec-out", "screencap", "-p")
            with Image.open(BytesIO(raw)) as png:
                screen = png.copy()
        size = screen.size
        yield captured_at - start, screen, focus
        interval = SKILL_CAST_INTERVAL if in_battle else ADB_FRAME_INTERVAL
        if screenshot_interval is not None and not streamed:
            # Pace from the fallback screenshot if stream startup took time.
            interval = screenshot_interval
            sample = captured_at
        sleep(max(0, interval - (monotonic() - sample)))


def replay_frames(video):
    next_sample = 0
    for _, time, image in read_video(video):
        if time >= next_sample - 0.002:
            yield time, image
            next_sample += 0.20


def _resolve_skip_tier(mode: str, skip_arg: int | bool | None) -> int:
    if skip_arg is True:
        return SKIP_GEM_THRESHOLD
    if type(skip_arg) is int and skip_arg in SKIP_TIERS:
        return skip_arg
    if mode == "skip":
        return SKIP_GEM_THRESHOLD
    if mode in {"skip10", "skip20", "skip30"}:
        return int(mode[4:])
    return 0


def play(args, target, report, journal):
    stream_enabled = getattr(args, "battle_stream", None)
    if stream_enabled is None:
        stream_enabled = target is not None and target.emulator == "bluestacks"
    with (closing(SkillTapShell()) as skill_taps,
          closing(BattleCapture(enabled=stream_enabled)) as battle_capture,
          ThreadPoolExecutor(max_workers=1) as focus_worker,
          ThreadPoolExecutor(max_workers=1) as cast_worker):
        _play(args, target, report, journal, skill_taps, focus_worker, cast_worker, battle_capture)


def _send_cast(skill_taps, target, point, activity, last_sent_at):
    skill_taps.start(target.adb, target.serial)
    if last_sent_at is not None:
        delay = SKILL_CAST_INTERVAL - (monotonic() - last_sent_at)
        if delay > 0:
            sleep(delay)
    package, current_activity = foreground_window(target.adb, target.serial)
    if (package != "com.raongames.growcastle" or not current_activity
            or is_ad_activity(current_activity) or current_activity != activity):
        return None
    sent_at = monotonic()
    confirmed = skill_taps.tap(target.adb, target.serial, *point)
    return sent_at, monotonic(), confirmed


def _play(args, target, report, journal, skill_taps, focus_worker, cast_worker, battle_capture):
    skip_tier = _resolve_skip_tier(args.mode, args.skip)
    is_wave_mode = args.mode in {"wave", "skip", "skip10", "skip20", "skip30"}
    mode_label = (f"skip{skip_tier}" if skip_tier != SKIP_GEM_THRESHOLD else "skip") if (is_wave_mode and skip_tier) else args.mode
    use_humanize = bool(args.tap and getattr(args, "humanize", True))
    hero_upgrade, hero_point = None, None
    if isinstance(args.upgrade_hero, str):
        _, x, _, y = HERO_SKILLS[HERO_SKILL_SLOTS.index(args.upgrade_hero)]
        hero_point = (x, y)
        hero_upgrade = HeroUpgrade(report, args.upgrade_hero, args.hero_min_gems, journal)
        upgrade_target = None
        gem_threshold = GEM_THRESHOLD
    elif args.upgrade_hero is not None:
        upgrade_target = "hero"
        gem_threshold = args.upgrade_gems if args.upgrade_gems is not None else args.upgrade_hero
    elif args.upgrade_tower is not None:
        upgrade_target = "tower"
        gem_threshold = args.upgrade_gems if args.upgrade_gems is not None else args.upgrade_tower
    else:
        upgrade_target = None
        gem_threshold = GEM_THRESHOLD
    tower = (
        TowerUpgrade(
            report,
            journal,
            target=upgrade_target,
            hero_slot=getattr(args, "hero_slot", None) or DEFAULT_HERO_SLOT,
            gem_threshold=gem_threshold,
            upgrade_count=args.upgrade_levels,
            humanize=use_humanize,
        )
        if upgrade_target
        else None
    )
    upgrade = hero_upgrade if hero_upgrade is not None else tower
    auto_captcha = bool(args.tap and args.auto_captcha)
    boss_slot = getattr(args, "boss_skill", None)
    boss_skill = SKILL_SLOTS.index(boss_slot) if boss_slot is not None else None
    boss_altar_skill = bool(getattr(args, "boss_altar_skill", False))
    player = Player(
        args.mode,
        report,
        tower,
        auto_captcha,
        watch_ads=args.ads,
        skip_waves=skip_tier,
        humanize=use_humanize,
        boss_skill=boss_skill,
        boss_altar_skill=boss_altar_skill,
        hero_upgrade=hero_upgrade,
    )
    vision = Vision(
        True,
        skip_tier=skip_tier if is_wave_mode else 0,
        track_boss=boss_skill is not None or boss_altar_skill,
        hero_point=hero_point,
    )
    prefix = "TAP" if args.tap else "PREVIEW"
    live_start = None
    if args.video:
        frames = ((time, image, None) for time, image in replay_frames(args.video))
        print(f"{prefix} mode={mode_label} source={args.video}", flush=True)
    else:
        connected_serial = ensure_adb_connected(
            target.adb,
            target.serial,
            conf=target.conf,
            instance_key=target.instance if target.emulator == "bluestacks" else None,
            allow_port_refresh=(target.emulator == "bluestacks" and not args.serial),
        )
        target = replace(target, serial=connected_serial)
        live_start = monotonic()
        frames = adb_frames(
            target.adb, target.serial, args.seconds,
            fast_battle=lambda: args.tap and player.screen == "battle" and player.ad is None and not player.paused,
            focus_worker=focus_worker,
            start=live_start,
            battle_capture=battle_capture,
            report=report,
            screenshot_interval=LDPLAYER_CAPTURE_INTERVAL if target.emulator == "ldplayer" else None,
        )
        print(
            f"{prefix} mode={mode_label} emulator={target.emulator} "
            f"instance={target.instance} ({target.display_name!r}) source={target.serial}",
            flush=True,
        )
        if target.emulator == "ldplayer" and not battle_capture.available:
            report(0.0, f"LDPlayer capture: ADB screenshots (min interval {LDPLAYER_CAPTURE_INTERVAL:.2f}s)")

    captcha_attempts = 0
    last_captcha_attempt = float("-inf")
    captcha_retry_delay = CAPTCHA_RETRY_DELAY
    captcha_manual_notified = False
    last_cast_sent_at = None
    pending_cast = None
    verifying_stream_exit = False
    stream_misses = 0

    def finish_cast():
        nonlocal pending_cast, last_cast_sent_at
        action, point, previous_last_skill, future = pending_cast
        pending_cast = None
        result = future.result()
        if result is None:
            time = monotonic() - live_start
            player.skills_used.pop(action.slot, None)
            if action.slot == "altar":
                player.boss_altar_used = False
            player.last_skill = previous_last_skill
            player.invalidate_frame(time)
            report(time, f"{action.name} skipped: game is no longer in the foreground")
            return None
        last_cast_sent_at, finished_at, confirmed = result
        time = last_cast_sent_at - live_start
        if confirmed:
            report(time, f"{prefix} {action.name} {point}")
        else:
            report(time, f"{action.name} tap unconfirmed; using individual ADB commands for future casts")
        return action.slot, finished_at

    for time, original, focus in frames:
        ignore_skill = None
        if pending_cast is not None:
            finished_cast = finish_cast()
            if finished_cast is None:
                # The prefetched image may have been taken before the window changed.
                if focus is not None:
                    focus.cancel()
                battle_capture.close()
                continue
            slot, finished_at = finished_cast
            if battle_capture.stream is not None:
                latest = battle_capture.stream.next_frame(timeout=0)
                if latest is not None:
                    captured_at, original = latest
                    time = captured_at - live_start
            if live_start + time < finished_at:
                # A capture already in progress before ACK cannot rearm this slot.
                ignore_skill = slot
        normalized = viewport(original)
        if normalized is None:
            if focus is not None:
                focus.cancel()
            battle_capture.close()
            player.invalidate_frame(time)
            continue
        activity = None
        if args.video:
            screen = vision.classify(
                normalized,
                watching_ad=player.ad is not None,
                original=original,
                time=time,
            )
        else:
            package, win_activity = (focus.result() if focus is not None
                                     else foreground_window(target.adb, target.serial))
            if package == "com.raongames.growcastle" and win_activity:
                activity = win_activity
                # An ad can show footage of a game. Its Android window owns the
                # frame until it closes, even if the image resembles a game screen.
                screen = vision.classify(
                    normalized,
                    watching_ad=player.ad is not None,
                    ad_foreground=is_ad_activity(activity),
                    original=original,
                    time=time,
                    adb=target.adb,
                    serial=target.serial,
                )
            elif player.ad is not None and package in AD_OVERLAY_PACKAGES:
                screen = Screen("ad_external_overlay", keyevent=KEYCODE_BACK)
            else:
                vision.ad_closer.reset()
                screen = Screen("unfocused")
            if battle_capture.stream is not None:
                if screen.name != "battle":
                    # Use a native ADB screenshot for menus, overlays and CAPTCHA.
                    verifying_stream_exit = True
                    battle_capture.close()
                    player.invalidate_frame(time)
                    continue
                stream_misses = 0
            elif verifying_stream_exit:
                verifying_stream_exit = False
                stream_misses = stream_misses + 1 if screen.name == "battle" else 0
                if stream_misses >= 2:
                    battle_capture.available = False
                    report(time, "Battle capture: RGB stream missed the battle UI; using ADB screenshots")
            if focus is not None and (screen.name != "battle" or screen.mimic is not None):
                if foreground_window(target.adb, target.serial) != (package, win_activity):
                    vision.ad_closer.reset()
                    screen = Screen("unfocused")
        was_paused = player.paused
        previous_last_skill = player.last_skill
        action = player.feed(time, screen, ignore_skill=ignore_skill)
        if not player.paused:
            captcha_attempts = 0
            captcha_retry_delay = CAPTCHA_RETRY_DELAY
            captcha_manual_notified = False
        elif auto_captcha and screen.name == "captcha":
            waited = monotonic() - last_captcha_attempt >= captcha_retry_delay
            if captcha_attempts >= CAPTCHA_MAX_ATTEMPTS:
                # Still on the CAPTCHA after the last attempt (and its wait): stop
                # trying and leave it to the player.
                if waited and not captcha_manual_notified:
                    report(time, f"CAPTCHA: still present after {CAPTCHA_MAX_ATTEMPTS} attempts; "
                                 "stopped, waiting for manual solution")
                    captcha_manual_notified = True
            elif not was_paused or (waited and vision.captcha_start_visible(original)):
                captcha_attempts += 1
                attempt_started = monotonic()
                last_captcha_attempt = attempt_started
                limit = min(25, args.seconds - time) if args.seconds else 25
                label = f"{captcha_attempts}/{CAPTCHA_MAX_ATTEMPTS}"
                try:
                    selected = limit > 0 and watch_target(
                        target, limit, True,
                        start_screen=vision.captcha_start_visible,
                    )
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    report(time + monotonic() - attempt_started, f"CAPTCHA attempt {label} stopped: {exc}")
                    selected = False
                last_captcha_attempt = monotonic()
                elapsed = time + last_captcha_attempt - attempt_started
                if selected == TOP_FALLBACK:
                    captcha_retry_delay = CAPTCHA_FALLBACK_WAIT
                    report(elapsed, f"CAPTCHA attempt {label}: no selection, tapped the top cup; "
                                    f"waiting {CAPTCHA_FALLBACK_WAIT:.0f}s")
                elif selected:
                    captcha_retry_delay = CAPTCHA_RETRY_DELAY
                    report(elapsed, "CAPTCHA: selection sent; waiting for the game to resume")
                else:
                    captcha_retry_delay = CAPTCHA_RETRY_DELAY
                    if captcha_attempts >= CAPTCHA_MAX_ATTEMPTS:
                        report(elapsed, "CAPTCHA: waiting for manual solution")
                        captcha_manual_notified = True
        if action:
            if isinstance(action, SkillAction):
                hero_cast = isinstance(action.slot, int) and action.slot >= len(ARCHER_SKILLS)
                if args.tap:
                    point = (round(action.point[0] * original.width / VIEWPORT[0]),
                             round(action.point[1] * original.height / VIEWPORT[1]))
                    future = cast_worker.submit(
                        _send_cast, skill_taps, target, point, activity, last_cast_sent_at,
                    )
                    pending_cast = action, point, previous_last_skill, future
                elif hero_cast:
                    print(f"PREVIEW {action.name} {action.point}", flush=True)
                else:
                    report(time, f"{prefix} {action.name} {action.point}")
            elif action.keyevent is not None:
                if args.tap:
                    adb_run(target.adb, target.serial, "shell", "input", "keyevent", str(action.keyevent))
                report(time, f"{prefix} {action.name}")
            else:
                tap_logged = action.point
                hero_purchase = hero_upgrade is not None and action.charge is not None
                if args.tap:
                    if action.charge is not None:
                        try:
                            raw = adb_run(target.adb, target.serial, "exec-out", "screencap", "-p")
                            with Image.open(BytesIO(raw)) as check:
                                fresh = viewport(check) if check.size == original.size else None
                                latest = vision.classify(fresh) if fresh is not None else None
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            if not hero_purchase:
                                raise
                            report(time, f"Hero upgrade recheck failed; waiting for a new frame. {exc}")
                            hero_upgrade.suspend()
                            continue
                        if hero_purchase:
                            verified = hero_upgrade.can_tap(action, latest)
                        elif latest is not None and latest.name == "tower":
                            expected_gems, expected_level, expected_cost, step_levels = action.charge
                            observed_cost = latest.bulk_cost if step_levels == 10 else latest.cost
                            verified = (latest.gems, latest.level, observed_cost) == (
                                expected_gems, expected_level, expected_cost
                            )
                        else:
                            verified = False
                        if not verified or foreground_activity(target.adb, target.serial) != activity:
                            upgrade.suspend()
                            continue
                        upgrade.record_tap(action)
                    if use_humanize and not hero_purchase:
                        tap_logged = human_tap(
                            target.adb,
                            target.serial,
                            action.point,
                            frame_size=original.size,
                            viewport_size=VIEWPORT,
                            raw_point=action.raw_point,
                            jitter=action_jitter(action),
                            duration_range=(35, 60) if action.name.startswith("select Wave Skip ") else None,
                            rng=_PLAY_RNG,
                        )
                    else:
                        if action.raw_point:
                            x, y = round(action.point[0]), round(action.point[1])
                        else:
                            x = round(action.point[0] * original.width / VIEWPORT[0])
                            y = round(action.point[1] * original.height / VIEWPORT[1])
                        try:
                            adb_run(target.adb, target.serial, "shell", "input", "tap", str(x), str(y))
                        except (OSError, subprocess.SubprocessError) as exc:
                            if not hero_purchase:
                                raise
                            report(time, f"Hero upgrade tap unconfirmed; checking level and white gems. {exc}")
                            continue
                        tap_logged = (x, y)
                elif action.charge is not None:
                    upgrade.record_tap(action)
                report(time, f"{prefix} {action.name} {tap_logged}")

    if pending_cast is not None:
        finish_cast()


def _device_lock_key(target) -> str:
    if (target.emulator == "bluestacks" and target.conf is not None
            and target.instance and target.instance != target.serial):
        return f"bluestacks:{target.conf.resolve()}:{target.instance}"
    return target.serial


def parse_hero_upgrade(value):
    if value in HERO_SKILL_SLOTS:
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("choose a gem count or a slot from hero-1 to hero-9") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="preview decisions on a recorded MP4")
    parser.add_argument("--mode", choices=("wave", "replay", "skip", "skip10", "skip20", "skip30"), default="wave")
    parser.add_argument("--boss-skill", choices=SKILL_SLOTS, metavar="SLOT",
                        help="reserve one skill for BOSS (archer-1, archer-2, or hero-1 to hero-9)")
    parser.add_argument("--boss-altar-skill", action=argparse.BooleanOptionalAction, default=False,
                        help="tap the Altar skill at (118, 180) once when BOSS appears")
    parser.add_argument("--skip", nargs="?", const=SKIP_GEM_THRESHOLD, type=int, choices=SKIP_TIERS,
                        metavar="{10,20,30}",
                        help="select 10, 20, or 30 WAVES on Wave Skip when white gems >= tier (default: 30)")
    parser.add_argument("--emulator", choices=("bluestacks", "ldplayer"),
                        help="target emulator (default: bluestacks, or ldplayer when --ldconsole is given)")
    parser.add_argument("--instance", help="BlueStacks 5 instance key or window title (e.g. Pie64, Pie64_1)")
    parser.add_argument("--index", type=int, help="emulator instance index (0, 1, 2, ...)")
    parser.add_argument("--serial", help="ADB serial override (e.g. 127.0.0.1:5555 or emulator-5554)")
    parser.add_argument("--adb", type=Path, help="path to HD-Adb.exe or adb.exe (auto-detected by default)")
    parser.add_argument("--conf", type=Path, help="path to bluestacks.conf (auto-detected by default)")
    parser.add_argument("--ldconsole", type=Path, help="path to ldconsole.exe (for LDPlayer 9)")
    parser.add_argument("--list", action="store_true", help="list detected emulator instances and exit")
    parser.add_argument("--seconds", type=float, default=0, help="live capture limit, 0 waits indefinitely")
    parser.add_argument("--battle-stream", action=argparse.BooleanOptionalAction,
                        help="use continuous RGB capture in battle (default: on for BlueStacks, off for LDPlayer)")
    parser.add_argument("--tap", action="store_true", help="send the recognized actions to the emulator")
    parser.add_argument("--humanize", action=argparse.BooleanOptionalAction, default=True,
                        help="humanize touch coordinates, press duration, and timing jitter (default: enabled)")
    parser.add_argument("--ads", action=argparse.BooleanOptionalAction, default=True,
                        help="open gold reward ads automatically (default: enabled)")
    parser.add_argument("--auto-captcha", action=argparse.BooleanOptionalAction, default=True,
                        help="solve diamond CAPTCHA automatically when --tap is enabled (default: enabled)")
    parser.add_argument("--upgrade-hero", nargs="?", const=GEM_THRESHOLD, type=parse_hero_upgrade,
                        metavar="GEMS|hero-N",
                        help="upgrade at GEMS white gems (default: 50), or select hero-1 to hero-9 and reserve one gem")
    parser.add_argument("--hero-min-gems", type=int, metavar="GEMS",
                        help=f"starting threshold for --upgrade-hero hero-N (default: {MINIMUM_GEMS})")
    parser.add_argument("--hero-slot", type=int, choices=range(1, 13), metavar="{1..12}",
                        help="Hero slot to upgrade, left to right and top to bottom (default: 3)")
    parser.add_argument("--upgrade-tower", nargs="?", const=GEM_THRESHOLD, type=int, metavar="GEMS",
                        help="upgrade the Castle tower when white gems >= GEMS (default: 50)")
    parser.add_argument("--upgrade-gems", type=int, metavar="GEMS",
                        help="white-gem threshold override for --upgrade-hero / --upgrade-tower")
    parser.add_argument("--upgrade-levels", type=int, metavar="LEVELS",
                        help="max levels per upgrade batch (default: max(50, threshold))")
    args = parser.parse_args()
    named_hero = isinstance(args.upgrade_hero, str)
    if args.video and args.tap:
        parser.error("--video is preview only")
    if args.upgrade_hero is not None and args.upgrade_tower is not None:
        parser.error("choose either --upgrade-hero or --upgrade-tower, not both")
    if args.hero_slot is not None and args.upgrade_hero is None:
        parser.error("--hero-slot requires --upgrade-hero")
    if named_hero and any(value is not None for value in (args.hero_slot, args.upgrade_levels, args.upgrade_gems)):
        parser.error("use --hero-min-gems with --upgrade-hero hero-N; slot and batch options require a numeric threshold")
    if args.hero_min_gems is not None:
        if not named_hero:
            parser.error("--hero-min-gems requires --upgrade-hero hero-N")
        if args.hero_min_gems < MINIMUM_GEMS:
            parser.error(f"--hero-min-gems must be >= {MINIMUM_GEMS}")
    else:
        args.hero_min_gems = MINIMUM_GEMS
    if (args.upgrade_gems is not None or args.upgrade_levels is not None) and (
            args.upgrade_hero is None and args.upgrade_tower is None):
        parser.error("--upgrade-gems and --upgrade-levels require --upgrade-hero or --upgrade-tower")
    for flag_name, value in (
        ("--upgrade-hero", args.upgrade_hero if not named_hero else None),
        ("--upgrade-tower", args.upgrade_tower),
        ("--upgrade-gems", args.upgrade_gems),
        ("--upgrade-levels", args.upgrade_levels),
    ):
        if value is not None and value < 1:
            parser.error(f"{flag_name} must be >= 1")
    if args.seconds < 0 or (args.index is not None and args.index < 0):
        parser.error("--seconds and --index must be nonnegative")

    def report(time, message):
        print(f"{time:6.2f}s  {message}", flush=True)

    try:
        if args.list:
            emu = args.emulator or ("ldplayer" if args.ldconsole else "bluestacks")
            print(format_instances(emulator=emu, adb=args.adb, conf=args.conf, ldconsole=args.ldconsole))
            return

        target = None if args.video else resolve_target(
            emulator=args.emulator,
            instance=args.instance,
            index=args.index,
            serial=args.serial,
            adb=args.adb,
            conf=args.conf,
            ldconsole=args.ldconsole,
        )
        journal = session_lock = None
        if args.tap and target is not None:
            device_id = sha256(_device_lock_key(target).encode()).hexdigest()[:16]
            session_lock = Path(__file__).with_name(f".autoplay-{device_id}.lock")
            if named_hero:
                journal = Path(__file__).with_name(f".hero-slot-upgrade-{device_id}.json")
            elif args.upgrade_hero is not None:
                journal = Path(__file__).with_name(f".hero-upgrade-{device_id}.json")
            elif args.upgrade_tower is not None:
                journal = Path(__file__).with_name(f".tower-upgrade-{device_id}.json")
        conflict_label = f"{target.instance} ({target.serial})" if target else "device"
        with lock_journal(session_lock, f"Autoplay is already running with --tap on {conflict_label}."), lock_journal(journal):
            play(args, target, report, journal)
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as exc:
        parser.exit(1, f"{exc.stderr.decode(errors='replace').strip() or exc}\n")
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
