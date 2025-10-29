#!/usr/bin/env python3
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
from collections import deque
from enum import Enum, auto
import random
import time
from logger import DiagnosticLogger, LogEntry

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

def speed_for_bpm(bpm: float, intensity: float = 1.0) -> int:
    """intensity: 0.5=slow, 1.0=normal, 2.0=insane"""
    if bpm <= 0: return 20
    base = 600.0 / bpm
    s = int(round(base / intensity))
    return max(2, min(100, s))

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
        self._fast_hist = deque(maxlen=32)  # buffer for at least 16 beats, up to 32 for drops
        self._bpm_hist = deque(maxlen=8)
        # Optionally: store high/bass energy history for advanced drop/build detection
        self._high_ema = 0.0
        self._high_hist = deque(maxlen=32)
        self._bass_ema = 0.0
        self._bass_hist = deque(maxlen=32)

        # running averages for features
        self._high_hist_avg = 0.0
        self._bass_hist_avg = 0.0

        # Drop state machine
        self._in_predrop = False
        self._drop_detected = False
        self._drop_countdown = 0

        # program machine
        self._program: Program = Program.STATIC_COLOR
        self._prog_beats_left: int = 0
        self._program_intensity: float = 1.0
        self._cooldown_beats: int = 0

        # user preset override
        self._preset_active: bool = False
        self._preset_name: Optional[str] = None

        # diagnostic logging
        self.logger = DiagnosticLogger(enabled=True)  # Set False to disable

    # ---- lifecycle ----
    def reset_music_context(self):
        self.state = GridState()
        self._ema_fast = self._ema_med = self._ema_long = 0.0
        self._fast_hist.clear()
        self._bpm_hist.clear()
        self._high_ema = 0.0
        self._high_hist.clear()
        self._bass_ema = 0.0
        self._bass_hist.clear()
        self._high_hist_avg = 0.0
        self._bass_hist_avg = 0.0
        self._program = Program.STATIC_COLOR
        self._prog_beats_left = 0
        self._program_intensity = 1.0
        self._cooldown_beats = 0
        self._pal_index = 0
        self._in_predrop = False
        self._drop_detected = False
        self._drop_countdown = 0

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
    def _update_energy(self, rms: float, high: float = 0.0, bass: float = 0.0):
        """Update all running energy metrics. Optionally supply high, bass spectral content for advanced drop/build detection."""
        if self._ema_fast == 0.0:
            self._ema_fast = self._ema_med = self._ema_long = rms
        else:
            self._ema_fast = 0.55 * self._ema_fast + 0.45 * rms
            self._ema_med  = 0.90 * self._ema_med  + 0.10 * rms
            self._ema_long = 0.97 * self._ema_long + 0.03 * rms
        self._fast_hist.append(self._ema_fast)

        # Track highs (e.g. hi-hats or risers) and bass (for drops)
        self._high_ema = 0.8 * self._high_ema + 0.2 * high if high > 0 else self._high_ema
        self._bass_ema = 0.8 * self._bass_ema + 0.2 * bass if bass > 0 else self._bass_ema
        if high > 0:
            self._high_hist.append(self._high_ema)
        if bass > 0:
            self._bass_hist.append(self._bass_ema)
        # Update averages for high/bass
        self._high_hist_avg = mean(list(self._high_hist)) if self._high_hist else 0.0
        self._bass_hist_avg = mean(list(self._bass_hist)) if self._bass_hist else 0.0

    def _energy_tier(self) -> EnergyTier:
        ratio = (self._ema_fast + 1e-6) / (self._ema_med + 1e-6)
        if self._ema_fast < 0.07 or ratio < 0.85:
            return EnergyTier.LOW
        if self._ema_fast > 0.14 or ratio > 1.25:
            return EnergyTier.HIGH
        return EnergyTier.MED

    def _detect_drop(self) -> bool:
        """Detect classic EDM drop: buildup → silence → BOOM"""
        if len(self._fast_hist) < 16 or self.state.beat < 16:
            return False

        recent = list(self._fast_hist)[-16:]
        first_half = recent[:8]
        buildup = (recent[7] > recent[0] * 1.3)  # 30% increase over 8 beats

        predrop = recent[-4:-1]
        predrop_quiet = mean(predrop) < mean(recent[:-4]) * 0.6

        drop_spike = recent[-1] > mean(recent[:-1]) * 2.0

        bass_spike = self._bass_ema > self._bass_hist_avg * 2.5 if hasattr(self, '_bass_ema') and self._bass_hist_avg > 0 else True

        return buildup and predrop_quiet and drop_spike and bass_spike

    def _detect_build(self) -> bool:
        """Detect buildup sections (rising energy + hi-hats)"""
        if len(self._fast_hist) < 12:
            return False

        recent = list(self._fast_hist)[-12:]
        slope = (recent[-1] - recent[0]) / 11
        rising = slope > 0.01 and recent[-1] > recent[0] * 1.2

        high_rising = self._high_ema > self._high_hist_avg * 1.4 if hasattr(self, '_high_ema') and self._high_hist_avg > 0 else False

        return rising and high_rising and self._ema_fast > 0.10

    def _bpm_trending_up(self) -> bool:
        # No longer used for builds! Retained for fallback or legacy, but not build detection.
        if len(self._bpm_hist) < 5: return False
        xs = list(self._bpm_hist)[-5:]
        if any(b <= 0 for b in xs): return False
        slope = (xs[-1] - xs[0]) / 4.0
        return slope > 6.0

    def _detect_lull_spike_drop(self) -> bool:
        # Legacy method; replaced by _detect_drop for "real" EDM drops, but kept for possible use elsewhere.
        if len(self._fast_hist) < 8: return False
        pre = list(self._fast_hist)[:-2]
        if len(pre) < 6: return False
        pre_tail = pre[-6:-2]
        lull  = mean(pre_tail) < max(0.06, 0.8*self._ema_med)
        spike = mean(list(self._fast_hist)[-2:]) > max(0.12, 1.4*self._ema_med)
        return lull and spike

    # ---- programs ----
    def _enter_program(self, prg: Program, beats: int, bpm: float, color: Optional[RGB] = None, intensity: float = 1.0):
        self._program = prg
        self._prog_beats_left = max(1, beats)
        self._program_intensity = intensity  # PRESERVE for reassertion
        if prg == Program.RAINBOW_STROBE:
            self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm, intensity))
        elif prg == Program.WHITE_STROBE:
            self.set_mode(MODE_STROBE_WHITE, speed_for_bpm(bpm, intensity))
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

    def _detect_breakdown(self) -> bool:
        """Low energy after high = breakdown/bridge"""
        if len(self._fast_hist) < 20:
            return False
        prev = list(self._fast_hist)[-20:-8]
        recent = list(self._fast_hist)[-8:]
        was_high = mean(prev) > 0.15
        now_low = mean(recent) < 0.08
        return was_high and now_low and self._program in (Program.RAINBOW_STROBE, Program.WHITE_STROBE)

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
    def on_beat(self, bpm: float, rms: float, high: float = 0.0, bass: float = 0.0):
        # grid tick
        self.state.beat += 1
        bar_pos = (self.state.beat - 1) % self.state.bar_len
        is_downbeat = (bar_pos == 0)
        bar_index = (self.state.beat - 1) // self.state.bar_len
        phrase_boundary = (bar_index % self.state.phrase_bars == 0) and is_downbeat and self.state.beat > 1

        # sensors
        self._update_energy(rms, high, bass)
        if bpm > 0: self._bpm_hist.append(bpm)
        if self._cooldown_beats > 0: self._cooldown_beats -= 1

        # Update running high/bass averages (needed for _detect_drop/_detect_build logic)
        self._high_hist_avg = mean(list(self._high_hist)) if self._high_hist else 0.0
        self._bass_hist_avg = mean(list(self._bass_hist)) if self._bass_hist else 0.0

        # preset override
        if self._preset_active and self._preset_name:
            self._run_preset(self._preset_name, bpm, is_downbeat)
            return

        # ---- INTENSE DROP DETECTION AND STAGING ----
        if self._detect_drop() and self._cooldown_beats == 0 and not self._drop_detected:
            # Stage the drop: we'll execute it in 2 beats (at next phrase boundary)
            self._drop_detected = True
            self._drop_countdown = 2  # pre-drop in 2 beats

        # Execute pre-drop blackout
        if self._drop_detected and self._drop_countdown == 1:
            self.set_rgb(0, 0, 0)  # BLACKOUT
            self._in_predrop = True
            self._drop_countdown -= 1
            return

        # Execute THE DROP
        if self._drop_detected and self._drop_countdown == 0 and is_downbeat:
            self._in_predrop = False
            self._drop_detected = False
            self._enter_program(Program.RAINBOW_STROBE, beats=32, bpm=bpm, intensity=1.5)  # INSANE STROBE
            self.flash_white(200)
            self._cooldown_beats = 16
            return

        # Countdown for staged drop
        if self._drop_countdown > 0:
            self._drop_countdown -= 1

        # ---- END INTENSE DROP ----

        # phrase accent (but not during drops)
        if phrase_boundary and not self._drop_detected:
            tier = self._energy_tier()
            if tier == EnergyTier.HIGH:
                self.flash_white(250)  # LONGER on hype
            else:
                self.flash_white(140)

        # breakdown detection (post-drop chill)
        if self._detect_breakdown() and self._cooldown_beats == 0:
            self._enter_program(Program.PULSE_COLOR, beats=16, bpm=bpm)
            self._pal_index += 1
            return

        # build-up → white hits each beat, using energy-based build detector
        if self._detect_build():
            self._enter_program(Program.BUILD_FLASH, beats=self.state.bar_len*2, bpm=bpm)
            self.flash_white(60)
            return

        tier = self._energy_tier()

        # escalate to hype on sustained HIGH energy
        if tier == EnergyTier.HIGH and self._program not in (Program.RAINBOW_STROBE, Program.BUILD_FLASH):
            self._enter_program(Program.RAINBOW_STROBE, beats=self.state.bar_len*2, bpm=bpm, intensity=1.3)

        # per-beat runtime behavior (also reassert strobe so it never "sticks")
        if self._program == Program.BUILD_FLASH:
            self.flash_white(70)
        elif self._program == Program.RAINBOW_STROBE:
            # Reassert with PRESERVED intensity (maintains drop hype!)
            self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm, self._program_intensity))
        elif self._program == Program.WHITE_STROBE:
            self.set_mode(MODE_STROBE_WHITE, speed_for_bpm(bpm, self._program_intensity))
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

        # ---- DIAGNOSTIC LOGGING ----
        self.logger.log(LogEntry(
            timestamp=time.time(),
            beat_num=self.state.beat,
            bpm=bpm,
            rms=rms,
            bass=bass,
            mid=0.0,  # mid not currently used in detection, but logged for analysis
            high=high,
            ema_fast=self._ema_fast,
            ema_med=self._ema_med,
            ema_long=self._ema_long,
            energy_tier=self._energy_tier().name,
            program=self._program.name,
            bar_pos=bar_pos,
            phrase_boundary=phrase_boundary,
            drop_detected=self._drop_detected,
            build_detected=self._detect_build(),
            breakdown_detected=self._detect_breakdown()
        ))
