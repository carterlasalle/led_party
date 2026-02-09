#!/usr/bin/env python3
"""
Live Autoloops Engine — Club-quality section-aware lighting choreography.

Detects musical sections (verse/build/chorus/drop/breakdown) and runs
A/B-split effects that respond to beat position, onset strength, and
energy dynamics.  15 distinct effects with style-weighted selection.
"""
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional, Callable
from collections import deque
from enum import Enum, auto
import random
import time
from logger import DiagnosticLogger, LogEntry

RGB = Tuple[int, int, int]

# ---------------------------------------------------------------------------
# Color Palettes
# ---------------------------------------------------------------------------
PALETTES: Dict[str, List[RGB]] = {
    "ND":    [(12, 36, 150), (255, 200, 0), (255, 255, 255)],
    "Warm":  [(255, 120, 0), (255, 180, 120), (255, 40, 0), (255, 255, 255)],
    "Cool":  [(0, 180, 255), (0, 255, 150), (0, 80, 255), (255, 255, 255)],
    "Neon":  [(255, 0, 255), (0, 255, 255), (255, 0, 120), (255, 255, 255)],
    "Fire":  [(255, 0, 0), (255, 80, 0), (255, 160, 0), (255, 255, 100)],
    "Ocean": [(0, 30, 180), (0, 120, 255), (0, 200, 200), (150, 220, 255)],
    "UV":    [(100, 0, 255), (180, 0, 255), (255, 0, 200), (255, 100, 255)],
}

# ---------------------------------------------------------------------------
# Vendor mode constants
# ---------------------------------------------------------------------------
MODE_STROBE_RAINBOW = 0x30
MODE_STROBE_WHITE   = 0x37
MODE_STROBE_RED     = 0x31
MODE_STROBE_BLUE    = 0x33
MODE_PULSE_RED      = 0x26
MODE_PULSE_GREEN    = 0x27
MODE_PULSE_BLUE     = 0x28
MODE_PULSE_YELLOW   = 0x29
MODE_PULSE_CYAN     = 0x2A
MODE_PULSE_PURPLE   = 0x2B
MODE_PULSE_WHITE    = 0x2C
MODE_PULSE_RAINBOW  = 0x25

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
@dataclass
class GridState:
    beat: int = 0
    bar_len: int = 4
    phrase_bars: int = 8

class EnergyTier(Enum):
    LOW  = 1
    MED  = 2
    HIGH = 3

class Section(Enum):
    VERSE     = auto()
    BUILD     = auto()
    CHORUS    = auto()
    DROP      = auto()
    BREAKDOWN = auto()

class Effect(Enum):
    # -- Verse (subtle, atmospheric) --
    SINGLE_SPOT   = auto()   # one light on, other off, swap every 2 bars
    COLOR_WASH    = auto()   # both interpolate through palette, B trails A
    SOFT_PULSE    = auto()   # vendor pulse on palette color

    # -- Build (rising intensity) --
    AB_CHASE      = auto()   # A flashes then B, progressively faster
    STROBE_RAMP   = auto()   # white flashes getting shorter
    COLOR_RISE    = auto()   # colors brighten and advance through palette

    # -- Chorus (full energy, dynamic A/B) --
    AB_ALTERNATE  = auto()   # classic A/B color swap every beat
    AB_COMPLEMENT = auto()   # complementary colors, swap every bar
    BEAT_CYCLE    = auto()   # both cycle palette on each beat, offset
    DOWNBEAT_BLAST= auto()   # white on 1, colors build through bar
    STROBE_SPLIT  = auto()   # one strobes while other holds solid

    # -- Drop (maximum impact) --
    BLACKOUT_BLAST= auto()   # 2 beats black → rainbow strobe full blast
    DROP_STROBE   = auto()   # max rainbow strobe + periodic white hits

    # -- Breakdown (cooling down) --
    SLOW_BREATHE  = auto()   # vendor pulse, slow and gentle
    FADE_WALK     = auto()   # gentle palette walk, B slightly dimmer

# backward-compat alias so app.py `from autoloops import Program` still works
class Program(Enum):
    STATIC_COLOR   = auto()
    PULSE_COLOR    = auto()
    SWAP_COLORS    = auto()
    WHITE_STROBE   = auto()
    RAINBOW_STROBE = auto()
    BUILD_FLASH    = auto()

PRESET_NAMES = [
    "Beat: White Flash",
    "Beat: Palette Cycle",
    "Beat: Alternate Base/Alt",
    "Beat: Downbeat Rainbow",
]

# ---------------------------------------------------------------------------
# Effect weights per section (base weights, adjusted by style at runtime)
# ---------------------------------------------------------------------------
_SECTION_EFFECTS: Dict[Section, List[Tuple[Effect, float]]] = {
    Section.VERSE: [
        (Effect.COLOR_WASH,  0.40),
        (Effect.SOFT_PULSE,  0.35),
        (Effect.SINGLE_SPOT, 0.25),
    ],
    Section.BUILD: [
        (Effect.AB_CHASE,    0.40),
        (Effect.STROBE_RAMP, 0.30),
        (Effect.COLOR_RISE,  0.30),
    ],
    Section.CHORUS: [
        (Effect.AB_ALTERNATE,  0.22),
        (Effect.DOWNBEAT_BLAST,0.22),
        (Effect.STROBE_SPLIT,  0.20),
        (Effect.BEAT_CYCLE,    0.18),
        (Effect.AB_COMPLEMENT, 0.18),
    ],
    Section.DROP: [
        (Effect.BLACKOUT_BLAST, 0.55),
        (Effect.DROP_STROBE,    0.45),
    ],
    Section.BREAKDOWN: [
        (Effect.SLOW_BREATHE, 0.50),
        (Effect.FADE_WALK,    0.50),
    ],
}

# style ×effect multipliers (>1 = more likely, <1 = less likely)
_STYLE_BOOSTS: Dict[str, Dict[Effect, float]] = {
    "House": {
        Effect.AB_ALTERNATE: 1.3, Effect.BEAT_CYCLE: 1.2,
        Effect.COLOR_WASH: 1.3, Effect.SOFT_PULSE: 1.2,
    },
    "EDM": {
        Effect.STROBE_RAMP: 1.5, Effect.DOWNBEAT_BLAST: 1.4,
        Effect.BLACKOUT_BLAST: 1.4, Effect.STROBE_SPLIT: 1.3,
    },
    "Hip-Hop": {
        Effect.SINGLE_SPOT: 1.4, Effect.BEAT_CYCLE: 1.3,
        Effect.AB_CHASE: 1.2, Effect.DOWNBEAT_BLAST: 1.2,
    },
    "Chill": {
        Effect.COLOR_WASH: 1.5, Effect.SOFT_PULSE: 1.4,
        Effect.SLOW_BREATHE: 1.3, Effect.FADE_WALK: 1.4,
        Effect.STROBE_RAMP: 0.5, Effect.DROP_STROBE: 0.5,
    },
}

# minimum beats in each section before allowing transition out
_MIN_SECTION_BEATS: Dict[Section, int] = {
    Section.VERSE:     8,
    Section.BUILD:     8,
    Section.CHORUS:    12,
    Section.DROP:      6,
    Section.BREAKDOWN: 8,
}

# Also keep the old dict for any lingering references
STYLE_WEIGHTS = {
    "House":   {"pulse": 0.55, "swap": 0.35, "accent": 0.10},
    "EDM":     {"pulse": 0.40, "swap": 0.25, "accent": 0.35},
    "Hip-Hop": {"pulse": 0.65, "swap": 0.30, "accent": 0.05},
    "Chill":   {"pulse": 0.80, "swap": 0.18, "accent": 0.02},
}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def speed_for_bpm(bpm: float, intensity: float = 1.0) -> int:
    if bpm <= 0: return 20
    base = 600.0 / bpm
    s = int(round(base / intensity))
    return max(2, min(100, s))

def nearest_pulse_mode(c: RGB) -> int:
    r, g, b = c
    if r > 220 and g > 220 and b > 220: return MODE_PULSE_WHITE
    if r > 200 and g > 200 and b < 80:  return MODE_PULSE_YELLOW
    if g > 200 and b > 200 and r < 80:  return MODE_PULSE_CYAN
    if r > 200 and b > 200 and g < 80:  return MODE_PULSE_PURPLE
    if r >= g and r >= b:               return MODE_PULSE_RED
    if g >= r and g >= b:               return MODE_PULSE_GREEN
    return MODE_PULSE_BLUE

def complement(c: RGB) -> RGB:
    return (255 - c[0], 255 - c[1], 255 - c[2])

def dim(c: RGB, factor: float) -> RGB:
    return (int(c[0] * factor), int(c[1] * factor), int(c[2] * factor))

def lerp_color(a: RGB, b: RGB, t: float) -> RGB:
    """Linear interpolation between two colors, t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )

def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  AutoLoopsEngine
# ═══════════════════════════════════════════════════════════════════════════
class AutoLoopsEngine:
    """
    Section-aware choreography engine with A/B split lighting.

    Callbacks
    ---------
    set_rgb(r, g, b)       – set all targets to static color
    set_mode(mode, speed)  – set vendor mode on all targets
    flash_white(ms)        – flash white on all targets for *ms*
    set_rgb_a / set_rgb_b  – set Light A / B independently (optional)
    set_mode_a / set_mode_b– set vendor mode on A / B independently (optional)
    """

    def __init__(self, set_rgb, set_mode, flash_white, *,
                 set_rgb_a=None, set_rgb_b=None,
                 set_mode_a=None, set_mode_b=None,
                 base_color: RGB = (255, 255, 255)):
        # ---- callbacks (all-targets) ----
        self.set_rgb = set_rgb
        self.set_mode = set_mode
        self.flash_white = flash_white

        # ---- callbacks (A/B split; fallback to all-targets) ----
        self._rgb_a  = set_rgb_a  or set_rgb
        self._rgb_b  = set_rgb_b  or set_rgb
        self._mode_a = set_mode_a or set_mode
        self._mode_b = set_mode_b or set_mode

        # ---- grid / style ----
        self.state = GridState()
        self.style = "House"
        self.palette_name = "ND"
        self._pal = list(PALETTES["ND"])
        self._pi = 0           # palette index
        self.base_color = base_color
        self.alt_color: RGB = (12, 36, 150)

        # ---- energy layers ----
        self._ema_fast = 0.0
        self._ema_med  = 0.0
        self._ema_long = 0.0
        self._fast_hist: deque = deque(maxlen=32)
        self._bpm_hist: deque  = deque(maxlen=8)
        self._high_ema  = 0.0
        self._bass_ema  = 0.0
        self._high_hist: deque = deque(maxlen=32)
        self._bass_hist: deque = deque(maxlen=32)
        self._high_avg = 0.0
        self._bass_avg = 0.0
        self._onset_ema = 0.0

        # ---- energy-tier hysteresis ----
        self._tier = EnergyTier.LOW
        self._tier_hold = 0
        self._tier_beats = 0   # consecutive beats at this tier

        # ---- section state ----
        self._section = Section.VERSE
        self._section_beats = 0
        self._prev_section = Section.VERSE

        # ---- effect state ----
        self._effect = Effect.COLOR_WASH
        self._fx_beat = 0      # beats into current effect
        self._fx_dur  = 16     # effect duration in beats

        # ---- drop / cooldown ----
        self._cooldown = 0
        self._in_vendor_mode = False  # track if hardware is running a vendor mode

        # ---- manual / preset ----
        self._manual_tier: Optional[EnergyTier] = None
        self._preset_active = False
        self._preset_name: Optional[str] = None
        self._sensitivity_scale = 1.0

        # ---- cached detections (avoid double-computation per beat) ----
        self._c_build = False
        self._c_breakdown = False
        self._c_drop = False

        # ---- logger ----
        self.logger = DiagnosticLogger(enabled=True)

    # ===================================================================
    #  Lifecycle / UI hooks
    # ===================================================================
    def reset_music_context(self):
        self.state = GridState()
        self._ema_fast = self._ema_med = self._ema_long = 0.0
        self._fast_hist.clear(); self._bpm_hist.clear()
        self._high_ema = self._bass_ema = 0.0
        self._high_hist.clear(); self._bass_hist.clear()
        self._high_avg = self._bass_avg = 0.0
        self._onset_ema = 0.0
        self._tier = EnergyTier.LOW; self._tier_hold = 0; self._tier_beats = 0
        self._section = Section.VERSE; self._section_beats = 0
        self._effect = Effect.COLOR_WASH; self._fx_beat = 0; self._fx_dur = 16
        self._cooldown = 0; self._in_vendor_mode = False
        self._pi = 0; self._c_build = self._c_breakdown = self._c_drop = False

    def set_style(self, name: str):
        if name in _STYLE_BOOSTS or name in STYLE_WEIGHTS:
            self.style = name

    def set_palette(self, name: str):
        if name in PALETTES:
            self.palette_name = name
            self._pal = list(PALETTES[name])
            self._pi = 0

    def set_manual_tier(self, tier: Optional[EnergyTier]):
        self._manual_tier = tier

    def enable_preset(self, name: str):
        self._preset_active = True
        self._preset_name = name

    def disable_preset(self):
        self._preset_active = False
        self._preset_name = None

    def force_build(self):
        """Manual trigger: enter BUILD section immediately."""
        self._transition_section(Section.BUILD)

    def force_drop(self):
        """Manual trigger: enter DROP section immediately."""
        self._transition_section(Section.DROP)
        self._cooldown = 16

    # ===================================================================
    #  Energy tracking
    # ===================================================================
    def _update_energy(self, rms: float, high: float, bass: float, onset: float):
        if self._ema_fast == 0.0:
            self._ema_fast = self._ema_med = self._ema_long = rms
        else:
            self._ema_fast = 0.55 * self._ema_fast + 0.45 * rms
            self._ema_med  = 0.90 * self._ema_med  + 0.10 * rms
            self._ema_long = 0.97 * self._ema_long + 0.03 * rms
        self._fast_hist.append(self._ema_fast)

        if high > 0:
            self._high_ema = 0.8 * self._high_ema + 0.2 * high
            self._high_hist.append(self._high_ema)
        if bass > 0:
            self._bass_ema = 0.8 * self._bass_ema + 0.2 * bass
            self._bass_hist.append(self._bass_ema)
        self._high_avg = mean(list(self._high_hist)) if self._high_hist else 0.0
        self._bass_avg = mean(list(self._bass_hist)) if self._bass_hist else 0.0
        if onset > 0:
            self._onset_ema = 0.7 * self._onset_ema + 0.3 * onset

    # ===================================================================
    #  Energy tier (with hysteresis)
    # ===================================================================
    def _energy_tier(self) -> EnergyTier:
        if self._manual_tier is not None:
            return self._manual_tier
        ratio = (self._ema_fast + 1e-6) / (self._ema_med + 1e-6)
        s = self._sensitivity_scale
        lo = 0.045 / s
        hi = 0.065 / s

        if self._ema_fast < lo or ratio < 0.90:
            raw = EnergyTier.LOW
        elif self._ema_fast > hi or ratio > 1.15:
            raw = EnergyTier.HIGH
        else:
            raw = EnergyTier.MED

        # hysteresis
        if self._tier_hold > 0:
            self._tier_hold -= 1
            return self._tier

        if raw != self._tier:
            if raw.value > self._tier.value:        # escalation: immediate
                self._tier = raw
                self._tier_hold = 2
                self._tier_beats = 0
            else:                                    # de-escalation: 3-beat hold
                self._tier = raw
                self._tier_hold = 3
                self._tier_beats = 0
        else:
            self._tier_beats += 1
        return self._tier

    # ===================================================================
    #  Musical-structure detectors
    # ===================================================================
    def _detect_drop(self) -> bool:
        if len(self._fast_hist) < 16 or self.state.beat < 16:
            return False
        h = list(self._fast_hist)[-16:]
        buildup      = h[7] > h[0] * 1.08
        predrop_dip  = mean(h[-4:-1]) < mean(h[:-4]) * 0.85
        spike        = h[-1] > mean(h[:-1]) * 1.3
        bass_ok      = (self._bass_avg < 0.005) or (self._bass_ema > self._bass_avg * 1.5)
        strong_hit   = self._onset_ema > 0.8
        return (buildup and predrop_dip and spike and bass_ok) or \
               (buildup and spike and strong_hit and bass_ok)

    def _detect_build(self) -> bool:
        if len(self._fast_hist) < 12:
            return False
        h = list(self._fast_hist)[-12:]
        slope = (h[-1] - h[0]) / 11
        rising = slope > 0.003 and h[-1] > h[0] * 1.12
        high_up = (self._high_avg > 0.01 and self._high_ema > self._high_avg * 1.3)
        floor = self._ema_fast > (0.045 / self._sensitivity_scale)
        return rising and floor and (high_up or slope > 0.006)

    def _detect_breakdown(self) -> bool:
        if len(self._fast_hist) < 20:
            return False
        prev = mean(list(self._fast_hist)[-20:-8])
        recent = mean(list(self._fast_hist)[-8:])
        active = prev > (0.035 / self._sensitivity_scale)
        return active and recent < prev * 0.55

    # ===================================================================
    #  Section state machine
    # ===================================================================
    def _update_section(self):
        tier = self._energy_tier()
        slope = 0.0
        if len(self._fast_hist) >= 8:
            r = list(self._fast_hist)[-8:]
            slope = (r[-1] - r[0]) / 7

        new = self._section
        sb  = self._section_beats
        mn  = _MIN_SECTION_BEATS.get(self._section, 8)

        # DROP has highest priority (transient event)
        if self._c_drop and self._cooldown == 0 and self._section != Section.DROP:
            new = Section.DROP

        # BUILD: rising energy, not already dropping
        elif self._c_build and self._section not in (Section.DROP,) and tier != EnergyTier.LOW:
            if self._section != Section.BUILD:
                new = Section.BUILD

        # CHORUS: sustained high
        elif tier == EnergyTier.HIGH and self._tier_beats >= 6:
            if self._section == Section.DROP and sb < 8:
                pass   # let drop play out
            else:
                new = Section.CHORUS

        # BREAKDOWN: coming down from high-energy section
        elif self._section in (Section.CHORUS, Section.DROP) and sb >= mn:
            if tier != EnergyTier.HIGH and slope < 0:
                new = Section.BREAKDOWN

        # VERSE: low/med steady state
        elif tier in (EnergyTier.LOW, EnergyTier.MED) and sb >= mn:
            if self._section == Section.BREAKDOWN and sb >= 8:
                new = Section.VERSE
            elif self._section == Section.BUILD and tier == EnergyTier.LOW:
                new = Section.VERSE

        if new != self._section:
            self._transition_section(new)
        else:
            self._section_beats += 1

    def _transition_section(self, new: Section):
        self._prev_section = self._section
        self._section = new
        self._section_beats = 0
        self._pick_effect(new)

    # ===================================================================
    #  Effect selection
    # ===================================================================
    def _pick_effect(self, section: Section):
        base = _SECTION_EFFECTS.get(section, [])
        if not base:
            return
        boosts = _STYLE_BOOSTS.get(self.style, {})

        # avoid repeating the same effect
        available = [(e, w * boosts.get(e, 1.0)) for e, w in base if e != self._effect]
        if not available:
            available = [(e, w * boosts.get(e, 1.0)) for e, w in base]

        effects = [e for e, _ in available]
        weights = [w for _, w in available]
        self._effect = random.choices(effects, weights=weights, k=1)[0]
        self._fx_beat = 0
        self._fx_dur = self._default_duration(section)
        self._pi += 1  # fresh palette colors on effect change
        self._in_vendor_mode = False

    def _default_duration(self, section: Section) -> int:
        bar = self.state.bar_len
        return {
            Section.DROP:      bar * 2,    # 8 beats — short & intense
            Section.BUILD:     bar * 4,    # 16 beats
            Section.CHORUS:    bar * 4,    # 16 beats then rotate
            Section.BREAKDOWN: bar * 4,
            Section.VERSE:     bar * 4,
        }.get(section, bar * 4)

    # ===================================================================
    #  Effect execution  (the creative core)
    # ===================================================================
    def _execute_effect(self, bpm: float, bar_pos: int, is_downbeat: bool,
                        phrase_boundary: bool, onset: float):
        pal = self._pal
        pi  = self._pi
        beat = self.state.beat
        eb   = self._fx_beat
        bl   = self.state.bar_len

        # Helper: palette color at offset
        def pc(offset: int = 0) -> RGB:
            return pal[(pi + offset) % len(pal)]

        # ─── VERSE ────────────────────────────────────────────────────

        if self._effect == Effect.SINGLE_SPOT:
            c = pc()
            # A on for 2 bars, B off; then swap
            if ((beat - 1) // (bl * 2)) % 2 == 0:
                self._rgb_a(*c); self._rgb_b(0, 0, 0)
            else:
                self._rgb_a(0, 0, 0); self._rgb_b(*c)
            if phrase_boundary:
                self._pi += 1

        elif self._effect == Effect.COLOR_WASH:
            # A and B interpolate through palette, B trails A by 2 beats
            cycle = 8
            t_a = (eb % cycle) / cycle
            t_b = ((eb - 2) % cycle) / cycle
            c1, c2 = pc(0), pc(1)
            self._rgb_a(*lerp_color(c1, c2, t_a))
            self._rgb_b(*lerp_color(c1, c2, t_b))
            if eb > 0 and eb % cycle == 0:
                self._pi += 1

        elif self._effect == Effect.SOFT_PULSE:
            c = pc()
            mode = nearest_pulse_mode(c)
            if not self._in_vendor_mode or is_downbeat:
                self._mode_a(mode, 80)
                self._mode_b(mode, 80)
                self._in_vendor_mode = True
            if phrase_boundary:
                self._pi += 1
                self._in_vendor_mode = False  # force refresh next beat

        # ─── BUILD ────────────────────────────────────────────────────

        elif self._effect == Effect.AB_CHASE:
            progress = min(1.0, eb / max(1, self._fx_dur))
            c = pc()
            bright = dim(c, 0.2 + 0.3 * progress)
            # A and B alternate white flashes — creates motion
            if beat % 2 == 0:
                self._rgb_a(255, 255, 255)
                self._rgb_b(*bright)
            else:
                self._rgb_a(*bright)
                self._rgb_b(255, 255, 255)
            self._in_vendor_mode = False
            if is_downbeat:
                self.flash_white(int(60 + 90 * progress))

        elif self._effect == Effect.STROBE_RAMP:
            progress = min(1.0, eb / max(1, self._fx_dur))
            flash_ms = int(140 - 100 * progress)   # 140 → 40 ms
            c = pc()
            # Flash white + show palette color underneath alternating A/B
            self.flash_white(flash_ms)
            if beat % 2 == 0:
                self._rgb_a(*c)
                self._rgb_b(0, 0, 0)
            else:
                self._rgb_a(0, 0, 0)
                self._rgb_b(*c)
            self._in_vendor_mode = False

        elif self._effect == Effect.COLOR_RISE:
            progress = min(1.0, eb / max(1, self._fx_dur))
            idx = pi + eb // 2  # advance palette every 2 beats
            c = pal[idx % len(pal)]
            # Mix towards white as build progresses
            mixed = lerp_color(dim(c, 0.5), (255, 255, 255), progress * 0.45)
            self._rgb_a(*mixed)
            self._rgb_b(*mixed)
            self._in_vendor_mode = False
            if is_downbeat:
                self.flash_white(int(50 + 100 * progress))

        # ─── CHORUS ───────────────────────────────────────────────────

        elif self._effect == Effect.AB_ALTERNATE:
            c1, c2 = pc(0), pc(1)
            if beat % 2 == 0:
                self._rgb_a(*c1); self._rgb_b(*c2)
            else:
                self._rgb_a(*c2); self._rgb_b(*c1)
            self._in_vendor_mode = False
            # punchy white on strong downbeats
            if is_downbeat and onset > 0.6:
                self.flash_white(80)
            # rotate colors every 8 beats
            if eb > 0 and eb % 8 == 0:
                self._pi += 1

        elif self._effect == Effect.AB_COMPLEMENT:
            c = pc()
            comp = complement(c)
            bar_half = (beat // bl) % 2
            if bar_half == 0:
                self._rgb_a(*c); self._rgb_b(*comp)
            else:
                self._rgb_a(*comp); self._rgb_b(*c)
            self._in_vendor_mode = False
            if phrase_boundary:
                self._pi += 1
                self.flash_white(120)

        elif self._effect == Effect.BEAT_CYCLE:
            c_a = pal[(pi + eb) % len(pal)]
            c_b = pal[(pi + eb + 2) % len(pal)]
            self._rgb_a(*c_a)
            self._rgb_b(*c_b)
            self._in_vendor_mode = False
            if is_downbeat and onset > 0.7:
                self.flash_white(70)

        elif self._effect == Effect.DOWNBEAT_BLAST:
            c1, c2 = pc(0), pc(1)
            if bar_pos == 0:
                # THE ONE — both blast white
                self._rgb_a(255, 255, 255)
                self._rgb_b(255, 255, 255)
            elif bar_pos == 1:
                self._rgb_a(*c1); self._rgb_b(0, 0, 0)
            elif bar_pos == 2:
                self._rgb_a(*c1); self._rgb_b(*c2)
            else:
                self._rgb_a(0, 0, 0); self._rgb_b(*c2)
            self._in_vendor_mode = False
            if phrase_boundary:
                self._pi += 1

        elif self._effect == Effect.STROBE_SPLIT:
            c = pc()
            bar_half = (beat // bl) % 2
            if bar_half == 0:
                # A strobes, B holds solid color
                self._mode_a(MODE_STROBE_WHITE, speed_for_bpm(bpm, 1.2))
                self._rgb_b(*c)
            else:
                # swap
                self._rgb_a(*c)
                self._mode_b(MODE_STROBE_WHITE, speed_for_bpm(bpm, 1.2))
            self._in_vendor_mode = True
            if phrase_boundary:
                self._pi += 1

        # ─── DROP ─────────────────────────────────────────────────────

        elif self._effect == Effect.BLACKOUT_BLAST:
            if eb < 2:
                # total blackout — tension!
                self._rgb_a(0, 0, 0); self._rgb_b(0, 0, 0)
                self._in_vendor_mode = False
            elif eb == 2:
                # THE DROP — blast
                self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm, 1.8))
                self.flash_white(250)
                self._in_vendor_mode = True
            else:
                # sustain strobe, re-assert
                self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm, 1.5))

        elif self._effect == Effect.DROP_STROBE:
            self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm, 1.5))
            self._in_vendor_mode = True
            if eb == 0:
                self.flash_white(200)
            elif beat % 4 == 0:
                self.flash_white(120)

        # ─── BREAKDOWN ────────────────────────────────────────────────

        elif self._effect == Effect.SLOW_BREATHE:
            c = pc()
            mode = nearest_pulse_mode(c)
            if not self._in_vendor_mode or is_downbeat:
                self._mode_a(mode, 95)
                self._mode_b(mode, 95)
                self._in_vendor_mode = True
            if phrase_boundary:
                self._pi += 1
                self._in_vendor_mode = False

        elif self._effect == Effect.FADE_WALK:
            c = pal[(pi + eb // 4) % len(pal)]
            self._rgb_a(*c)
            self._rgb_b(*dim(c, 0.5))  # B dimmer for depth
            self._in_vendor_mode = False

        # ─── advance ──────────────────────────────────────────────────
        self._fx_beat += 1
        if self._fx_beat >= self._fx_dur:
            self._pick_effect(self._section)

    # ===================================================================
    #  Phrase boundary accent
    # ===================================================================
    def _on_phrase(self, bpm: float, onset: float):
        if self._section == Section.CHORUS:
            self.flash_white(180)
            self._pi += 1
        elif self._section == Section.VERSE:
            self._pi += 1
        elif self._section == Section.BUILD:
            progress = min(1.0, self._fx_beat / max(1, self._fx_dur))
            self.flash_white(int(80 + 120 * progress))

    # ===================================================================
    #  Preset executor (backward compat)
    # ===================================================================
    def _run_preset(self, name: str, bpm: float, is_downbeat: bool):
        if name == "Beat: White Flash":
            self.flash_white(70)
        elif name == "Beat: Palette Cycle":
            c = self._pal[self._pi % len(self._pal)]
            self._pi += 1
            self.set_rgb(*c)
        elif name == "Beat: Alternate Base/Alt":
            if self.state.beat % 2 == 0: self.set_rgb(*self.alt_color)
            else: self.set_rgb(*self.base_color)
        elif name == "Beat: Downbeat Rainbow":
            if is_downbeat:
                self.set_mode(MODE_STROBE_RAINBOW, speed_for_bpm(bpm))
            else:
                self.set_rgb(*self.base_color)

    # ===================================================================
    #  backward-compat _enter_program (used by old manual trigger code)
    # ===================================================================
    def _enter_program(self, prg, beats: int, bpm: float, **kw):
        """Compat shim: maps old Program enum to new section/effect."""
        if prg == Program.BUILD_FLASH:
            self.force_build()
        elif prg == Program.RAINBOW_STROBE:
            self.force_drop()
        else:
            self.set_rgb(*self.base_color)

    # ===================================================================
    #  Main beat entry
    # ===================================================================
    def on_beat(self, bpm: float, rms: float, high: float = 0.0,
                bass: float = 0.0, onset_strength: float = 0.0):
        # ---- grid tick ----
        self.state.beat += 1
        bar_pos = (self.state.beat - 1) % self.state.bar_len
        is_downbeat = (bar_pos == 0)
        bar_index = (self.state.beat - 1) // self.state.bar_len
        phrase = (bar_index % self.state.phrase_bars == 0) and is_downbeat and self.state.beat > 1

        # ---- energy / detection ----
        self._update_energy(rms, high, bass, onset_strength)
        if bpm > 0:
            self._bpm_hist.append(bpm)
        if self._cooldown > 0:
            self._cooldown -= 1

        self._c_drop      = self._detect_drop()
        self._c_build     = self._detect_build()
        self._c_breakdown = self._detect_breakdown()

        # ---- preset override ----
        if self._preset_active and self._preset_name:
            self._run_preset(self._preset_name, bpm, is_downbeat)
            self._log(bpm, rms, bass, high, bar_pos, phrase)
            return

        # ---- section update (may change effect) ----
        self._update_section()

        # ---- phrase accent ----
        if phrase:
            self._on_phrase(bpm, onset_strength)

        # ---- execute current effect ----
        self._execute_effect(bpm, bar_pos, is_downbeat, phrase, onset_strength)

        # ---- log ----
        self._log(bpm, rms, bass, high, bar_pos, phrase)

    # ===================================================================
    #  Logging
    # ===================================================================
    def _log(self, bpm, rms, bass, high, bar_pos, phrase):
        self.logger.log(LogEntry(
            timestamp=time.time(),
            beat_num=self.state.beat,
            bpm=bpm,
            rms=rms,
            bass=bass,
            mid=0.0,
            high=high,
            ema_fast=self._ema_fast,
            ema_med=self._ema_med,
            ema_long=self._ema_long,
            energy_tier=self._energy_tier().name,
            program=f"{self._section.name}/{self._effect.name}",
            bar_pos=bar_pos,
            phrase_boundary=phrase,
            drop_detected=self._c_drop,
            build_detected=self._c_build,
            breakdown_detected=self._c_breakdown,
        ))
