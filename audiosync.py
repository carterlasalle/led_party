#!/usr/bin/env python3
import threading, queue, time
from dataclasses import dataclass
from typing import Callable, Optional, Deque, List
from collections import deque
import numpy as np
import sounddevice as sd

# Optional: aubio improves onset detection if available
try:
    import aubio
    HAVE_AUBIO = True
except ImportError:
    HAVE_AUBIO = False

@dataclass
class BeatEvent:
    timestamp: float
    bpm: float
    rms: float
    bass: float = 0.0
    mid: float = 0.0
    high: float = 0.0

def _median(xs: List[float]) -> float:
    a = sorted(xs)
    n = len(a)
    if n == 0: return 0.0
    m = n // 2
    return a[m] if n % 2 else 0.5 * (a[m-1] + a[m])

class AudioBeatDetector(threading.Thread):
    """
    Beat stream with robust BPM:
      • Onset/beat detection (aubio if present; otherwise gated-RMS)
      • Robust tempo from a pairwise-lag estimator over recent beats
      • Half/double folding only; quick re-lock on real tempo change
      • Silence watchdog resets between songs / when stopped
    """
    def __init__(self, device_index: Optional[int], sample_rate: int = 44100, hop_size: int = 1024,
                 on_beat: Optional[Callable[[BeatEvent], None]] = None):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.hop_size = hop_size
        self.on_beat = on_beat

        self._running = threading.Event()
        self._running.clear()
        self._q: "queue.Queue[np.ndarray]" = queue.Queue()

        # Beat timing
        self._beats: Deque[float] = deque(maxlen=48)   # last beat timestamps (seconds)
        self._last_beat_ts: Optional[float] = None
        self._bpm_stable: float = 0.0
        self._deviation_beats = 0

        # Energy
        self._rms_ema = 0.0

        # Add spectral energy tracking (NEW)
        self._fft_window = np.hanning(self.hop_size)
        self._bass_ema = 0.0
        self._mid_ema = 0.0
        self._high_ema = 0.0

        # Config (tuneable)
        self._refractory = 0.18        # ignore re-triggers inside this (s)
        self._bpm_min, self._bpm_max = 70.0, 180.0
        self._unlock_pct = 0.12        # relative delta to start counting deviants
        self._unlock_needed = 3        # beats beyond threshold needed to re-lock
        self._silence_reset_seconds = 1.6

        # Aubio onset/tempo
        if HAVE_AUBIO:
            self._tempo = aubio.tempo("default", 2048, hop_size, sample_rate)
            self._tempo.set_silence(-40)
            self._tempo.set_threshold(0.3)

    # -------- External controls --------
    def reset(self):
        self._beats.clear()
        self._last_beat_ts = None
        self._bpm_stable = 0.0
        self._deviation_beats = 0
        self._rms_ema = 0.0
        # Reset spectral EMAs too
        self._bass_ema = 0.0
        self._mid_ema = 0.0
        self._high_ema = 0.0

    # -------- Internals --------
    def _register_beat(self, t_now: float):
        # Refractory to avoid double hits
        if self._last_beat_ts is not None and (t_now - self._last_beat_ts) < self._refractory:
            return
        self._last_beat_ts = t_now
        self._beats.append(t_now)

        bpm_new = self._estimate_bpm_pairwise()
        if bpm_new <= 0:
            bpm_out = self._bpm_stable or 0.0
        else:
            if self._bpm_stable <= 0.0:
                self._bpm_stable = bpm_new
                self._deviation_beats = 0
            else:
                rel = abs(bpm_new - self._bpm_stable) / max(1e-6, self._bpm_stable)
                if rel > self._unlock_pct:
                    self._deviation_beats += 1
                    if self._deviation_beats >= self._unlock_needed:
                        # accept the new tempo quickly
                        self._bpm_stable = bpm_new
                        self._deviation_beats = 0
                else:
                    # gently smooth when consistent
                    self._bpm_stable = 0.75 * self._bpm_stable + 0.25 * bpm_new
                    self._deviation_beats = max(0, self._deviation_beats - 1)
            bpm_out = self._bpm_stable

        if self.on_beat:
            self.on_beat(BeatEvent(
                timestamp=t_now,
                bpm=float(bpm_out),
                rms=float(self._rms_ema),
                bass=float(self._bass_ema),
                mid=float(self._mid_ema),
                high=float(self._high_ema)
            ))

    def _estimate_bpm_pairwise(self) -> float:
        """
        Robust period estimate from pairwise lags:
          For all i<j in recent beats, compute per-beat period = (t[j]-t[i])/(j-i).
          Take median of values in target tempo window (70..180 BPM).
          Then fold (×2, ×0.5) to keep inside window.
        """
        ts = list(self._beats)
        n = len(ts)
        if n < 6:
            return 0.0

        per_beats = []
        for i in range(n - 1):
            ti = ts[i]
            for j in range(i + 1, n):
                k = j - i
                lag = ts[j] - ti
                # convert to per-beat period
                dt = lag / k
                if 60.0 / self._bpm_max <= dt <= 60.0 / self._bpm_min:
                    per_beats.append(dt)

        if len(per_beats) < 6:
            return 0.0

        dt_med = _median(per_beats)       # robust per-beat period (s)
        bpm_raw = 60.0 / max(1e-6, dt_med)

        # fold into window
        candidates = [bpm_raw, bpm_raw * 2.0, bpm_raw * 0.5]
        # Prefer staying near previous tempo if we have one
        if self._bpm_stable > 0:
            best = min(candidates, key=lambda c: abs(c - self._bpm_stable))
        else:
            # Otherwise choose the one closest to a neutral 120 BPM within window
            in_win = [c for c in candidates if self._bpm_min <= c <= self._bpm_max]
            best = min(in_win or candidates, key=lambda c: abs(c - 120.0))
        # Final clamp
        if best < self._bpm_min:
            best *= 2.0
        if best > self._bpm_max:
            best *= 0.5
        return float(best)

    def _analyze_spectrum(self, x: np.ndarray):
        """Extract bass/mid/high energy for better detection"""
        # Apply window to input chunk
        windowed = x * self._fft_window
        fft = np.fft.rfft(windowed)
        mag = np.abs(fft)
        freqs = np.fft.rfftfreq(len(x), 1/self.sample_rate)

        # Frequency bands
        bass = np.sum(mag[(freqs >= 20) & (freqs < 150)])
        mid = np.sum(mag[(freqs >= 150) & (freqs < 4000)])
        high = np.sum(mag[(freqs >= 4000) & (freqs < 12000)])

        # Smooth with exponential moving average
        self._bass_ema = 0.7 * self._bass_ema + 0.3 * bass
        self._mid_ema = 0.7 * self._mid_ema + 0.3 * mid
        self._high_ema = 0.7 * self._high_ema + 0.3 * high

        return (self._bass_ema, self._mid_ema, self._high_ema)

    def run(self):
        self._running.set()

        def callback(indata, frames, time_info, status):
            if status:
                # drop status warnings silently
                pass
            x = indata if indata.ndim == 1 else np.mean(indata, axis=1)
            self._q.put(x.astype(np.float32))

        with sd.InputStream(device=self.device_index, channels=1, samplerate=self.sample_rate,
                            blocksize=self.hop_size, callback=callback):
            last_activity = time.time()
            while self._running.is_set():
                now = time.time()
                # Silence / inactivity watchdog → hard reset between songs
                if (now - last_activity) > self._silence_reset_seconds and self._rms_ema < 0.02:
                    self.reset()

                try:
                    x = self._q.get(timeout=0.25)
                except queue.Empty:
                    continue

                # Energy tracking
                rms = float(np.sqrt(np.mean(x * x)))
                self._rms_ema = 0.85 * self._rms_ema + 0.15 * rms

                # Spectral energy update (NEW)
                self._analyze_spectrum(x)

                # Beat detection
                beat_now = False
                if HAVE_AUBIO:
                    is_beat = float(self._tempo(x).flatten()[0])
                    beat_now = (is_beat > 0.0)
                else:
                    # Simple adaptive gate fallback
                    thr = max(0.05, 0.7 * self._rms_ema)
                    beat_now = (rms > thr)

                if beat_now:
                    last_activity = now
                    self._register_beat(now)

    def stop(self):
        self._running.clear()

def list_input_devices():
    """Return [(index, name), ...] for devices that can capture audio."""
    devices = sd.query_devices()
    out = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            out.append((i, d["name"]))
    return out
