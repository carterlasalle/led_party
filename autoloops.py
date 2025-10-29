#!/usr/bin/env python3
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
from collections import deque
from enum import Enum, auto
import random

RGB = Tuple[int, int, int]

PALETTES: Dict[str, List[RGB]] = {
    "ND":   [(12, 36, 150), (255, 200, 0), (255, 255, 255)],
    "Warm": [(255, 120, 0), (255, 180, 120), (255, 40, 0), (255, 255, 255)],
    "Cool": [(0, 180, 255), (0, 255, 150), (0, 80, 255), (255, 255, 255)],
    "Neon": [(255, 0, 255), (0, 255, 255), (255, 0, 120), (255, 255, 255)],
}

# Vendor mode constants
MODE_STROBE_RAINBOW = 0x30
MODE_STROBE_WHITE   = 0x37
MODE_PULSE_RED      = 0x26
MODE_PULSE_GREEN    = 0x27
MODE_PULSE_BLUE     = 0x28
MODE_PULSE_YELLOW   = 0x29
MODE_PULSE_CYAN     = 0x2A
MODE_PULSE_PURPLE   = 0x2B
MODE_PULSE_WHITE    = 0x2C

@dataclass
class GridState:
    beat: int = 0
    bar_len: int = 4
    phrase_bars: int = 8

class EnergyTier(Enum):
    LOW = auto()
    MED = auto()
    HIGH = auto()

class Program(Enum):
    STATIC_COLOR   = auto()
    PULSE_COLOR    = auto()
    SWAP_COLORS    = auto()
    WHITE_STROBE   = auto()
    RAINBOW_STROBE = auto()
    BUILD_FLASH    = auto()  # white hit every beat during build

PRESET_NAMES = [
    "Beat: White Flash",
    "Beat: Palette Cycle",
    "Beat: Alternate Base/Alt",
    "Beat: Downbeat Rainbow",
]

STYLE_WEIGHTS = {
    "House":   {"pulse": 0.55, "swap": 0.35, "accent": 0.10},
    "EDM":     {"pulse": 0.40, "swap": 0.25, "accent": 0.35},
    "Hip-Hop": {"pulse": 0.65, "swap": 0.30, "accent": 0.05},
    "Chill":   {"pulse": 0.80, "swap": 0.18, "accent": 0.02},
}

def speed_for_bpm(bpm: float) -> int:
    if bpm <= 0: return 20
    s = int(round(600.0 / bpm))  # smaller = faster
    return max(2, min(60, s))

def nearest_pulse_mode(c: RGB) -> int:
    r, g, b = c
    if r>220 and g>220 and b>220: return MODE_PULSE_WHITE
    if r>200 and g>200 and b<80:  return MODE_PULSE_YELLOW
    if g>200 and b>200 and r<80:  return MODE_PULSE_CYAN
    if r>200 and b>200 and g<80:  return MODE_PULSE_PURPLE
    if r>=g and r>=b:             return MODE_PULSE_RED
    if g>=r and g>=b:             return MODE_PULSE_GREEN
    return MODE_PULSE_BLUE

def mean(xs: List[float]) -> float:
    return sum(xs)/len(xs) if xs else 0.0

class AutoLoopsEngine:
    def __init__(self, set_rgb, set_mode, flash_white, base_color=(255,255,255)):
        self.set_rgb = set_rgb
        self.set_mode = set_mode
        self.flash_white = flash_white

        self.state = GridState()
        self.style = "House"
        self.palette_name = "ND"
        self._current_palette = list(PALETTES[self.palette_name])
        self._pal_index = 0
        self.base_color = base_color
        self.alt_color = (12, 36, 150)

        # energy layers
        self._ema_fast = 0.0
        self._ema_med  = 0.0
        self._ema_long = 0.0
        self._fast_hist = deque(maxlen=16)
        self._bpm_hist = deque(maxlen=8)

        # program machine
        self._program: Program = Program.STATIC_COLOR
        self._prog_beats_left: int = 0
        self._cooldown_beats: int = 0

        # user preset override
        self._preset_active: bool = False
        self._preset_name: Optional[str] = None

    # ---- lifecycle ----
    def reset_music_context(self):
        self.state = GridState()
        self._ema_fast = self._ema_med = self._ema_long = 0.0
        self._fast_hist.clear()
        self._bpm_hist.clear()
        self._program = Program.STATIC_COLOR
        self._prog_beats_left = 0
        self._cooldown_beats = 0
        self._pal_index = 0

    # ---- UI hooks ----
    def set_style(self, name: str):
        if name in STYLE_WEIGHTS:
            self.style = name

    def set_palette(self, name: str):
        if name in PALETTES:
            self.palette_name = name
            self._current_palette = list(PALETTES[name])
            self._pal_index = 0

    def enable_preset(self, name: str):
        self._preset_active = True
        self._preset_name = name

    def disable_preset(self):
        self._preset_active = False
        self._preset_name = None

    # ---- detectors ----
    def _update_energy(self, rms: float):
        if self._ema_fast == 0.0:
            self._ema_fast = self._ema_med = self._ema_long = rms
        else:
            self._ema_fast = 0.55 * self._ema_fast + 0.45 * rms
            self._ema_med  = 0.90 * self._ema_med  + 0.10 * rms
            self._ema_long = 0.97 * self._ema_long + 0.03 * rms
        self._fast_hist.append(self._ema_fast)

    def _energy_tier(self) -> EnergyTier:
        ratio = (self._ema_fast + 1e-6) / (self._ema_med + 1e-6)
        if self._ema_fast < 0.07 or ratio < 0.85:
            return EnergyTier.LOW
        if self._ema_fast > 0.14 or ratio > 1.25:
            return EnergyTier.HIGH
        return EnergyTier.MED

    def _detect_lull_spike_drop(self) -> bool:
        if len(self._fast_hist) < 8: return False
        pre = list(self._fast_hist)[:-2]
        if len(pre) < 6: return False
        pre_tail = pre[-6:-2]
        lull  = mean(pre_tail) < max(0.06, 0.8*self._ema_med)
        spike = mean(list(self._fast_hist)[-2:]) > max(0.12, 1.4*self._ema_med)
        return lull and spike

    def _bpm_trending_up(self) -> bool:
        if len(self._bpm_hist) < 5: return False
        xs = list(self._bpm_hist)[-5:]
        if any(b <= 0 for b in xs): return False
        slope = (xs[-1] - xs[0]) / 4.0
        return slope > 6.0

    # ---- programs ----
    def _enter_program(self, prg: Program, beats: int, bpm: float, color: Optional[RGB] = None):
        self._program = prg
        self._prog_beats_left = max(1, beats)
        if prg == Program.RAINBOW_STROBE:
            self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm))
        elif prg == Program.WHITE_STROBE:
            self.set_mode(MODE_STROBE_WHITE, speed_for_bpm(bpm))
        elif prg == Program.PULSE_COLOR:
            c = color or self._current_palette[self._pal_index % len(self._current_palette)]
            self.set_mode(nearest_pulse_mode(c), 90)
        elif prg == Program.SWAP_COLORS:
            self.set_rgb(*self.base_color)
        elif prg == Program.STATIC_COLOR:
            c = color or self._current_palette[self._pal_index % len(self._current_palette)]
            self.set_rgb(*c)
        elif prg == Program.BUILD_FLASH:
            # handled per-beat
            pass

    def _choose_calm_program(self, bpm: float) -> Program:
        w = STYLE_WEIGHTS[self.style]
        return random.choices(
            [Program.PULSE_COLOR, Program.SWAP_COLORS, Program.STATIC_COLOR],
            weights=[w["pulse"], w["swap"], max(0.01, w["accent"]*0.6)],
            k=1
        )[0]

    # ---- preset executor ----
    def _run_preset(self, name: str, bpm: float, is_downbeat: bool):
        if name == "Beat: White Flash":
            self.flash_white(70)
        elif name == "Beat: Palette Cycle":
            c = self._current_palette[self._pal_index % len(self._current_palette)]
            self._pal_index += 1
            self.set_rgb(*c)
        elif name == "Beat: Alternate Base/Alt":
            if (self.state.beat % 2) == 0: self.set_rgb(*self.alt_color)
            else: self.set_rgb(*self.base_color)
        elif name == "Beat: Downbeat Rainbow":
            if is_downbeat:
                self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm))
            else:
                self.set_rgb(*self.base_color)

    # ---- main beat entry ----
    def on_beat(self, bpm: float, rms: float):
        # grid tick
        self.state.beat += 1
        bar_pos = (self.state.beat - 1) % self.state.bar_len
        is_downbeat = (bar_pos == 0)
        bar_index = (self.state.beat - 1) // self.state.bar_len
        phrase_boundary = (bar_index % self.state.phrase_bars == 0) and is_downbeat and self.state.beat > 1

        # sensors
        self._update_energy(rms)
        if bpm > 0: self._bpm_hist.append(bpm)
        if self._cooldown_beats > 0: self._cooldown_beats -= 1

        # preset override
        if self._preset_active and self._preset_name:
            self._run_preset(self._preset_name, bpm, is_downbeat)
            return

        # phrase accent
        if phrase_boundary:
            self.flash_white(140)

        # drop → long rainbow strobe + white accent
        if self._detect_lull_spike_drop() and self._cooldown_beats == 0:
            self._enter_program(Program.RAINBOW_STROBE, beats=self.state.bar_len*4, bpm=bpm)  # 16 beats hype
            self.flash_white(120)
            self._cooldown_beats = self.state.bar_len * 2
            return

        # build-up → white hits each beat
        if self._bpm_trending_up():
            self._enter_program(Program.BUILD_FLASH, beats=self.state.bar_len*2, bpm=bpm)
            self.flash_white(60)
            return

        tier = self._energy_tier()

        # escalate to hype on sustained HIGH energy
        if tier == EnergyTier.HIGH and self._program not in (Program.RAINBOW_STROBE, Program.BUILD_FLASH):
            self._enter_program(Program.RAINBOW_STROBE, beats=self.state.bar_len*2, bpm=bpm)

        # per-beat runtime behavior (also reassert strobe so it never “sticks”)
        if self._program == Program.BUILD_FLASH:
            self.flash_white(70)
        elif self._program == Program.RAINBOW_STROBE:
            self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm))
        elif self._program == Program.WHITE_STROBE:
            self.set_mode(MODE_STROBE_WHITE, speed_for_bpm(bpm))
        elif self._program == Program.SWAP_COLORS:
            if (self.state.beat % 2) == 0: self.set_rgb(*self.alt_color)
            else: self.set_rgb(*self.base_color)
        elif self._program == Program.STATIC_COLOR:
            if is_downbeat and random.random() < 0.08:
                self.flash_white(50)

        # duration / transitions (prefer changes on downbeats)
        self._prog_beats_left = max(0, self._prog_beats_left - 1)
        if self._prog_beats_left > 0 and not is_downbeat:
            return

        if tier == EnergyTier.LOW:
            hold = self.state.bar_len
            next_prog = random.choices(
                [Program.PULSE_COLOR, Program.STATIC_COLOR],
                weights=[0.6, 0.4],
                k=1
            )[0]
            self._pal_index += 1
            self._enter_program(next_prog, beats=hold, bpm=bpm)

        elif tier == EnergyTier.MED:
            hold = self.state.bar_len * 2
            next_prog = self._choose_calm_program(bpm)
            if next_prog in (Program.PULSE_COLOR, Program.STATIC_COLOR):
                self._pal_index += 1
            self._enter_program(next_prog, beats=hold, bpm=bpm)

        else:  # HIGH
            if self._program != Program.RAINBOW_STROBE:
                self._enter_program(Program.RAINBOW_STROBE, beats=self.state.bar_len*2, bpm=bpm)
            else:
                # give ears a breather while keeping movement
                self._enter_program(Program.SWAP_COLORS, beats=self.state.bar_len, bpm=bpm)
