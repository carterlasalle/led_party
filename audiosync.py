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
    onset_strength: float = 0.0  # 0..1 how hard the beat hit

def _median(xs: List[float]) -> float:
    a = sorted(xs)
    n = len(a)
    if n == 0: return 0.0
    m = n // 2
    return a[m] if n % 2 else 0.5 * (a[m-1] + a[m])

class AudioBeatDetector(threading.Thread):
    """
    Beat stream with robust BPM:
      - Onset/beat detection (aubio if present; otherwise spectral-flux gated)
      - Robust tempo from windowed pairwise-lag estimator over recent beats
      - Half/double folding only; quick re-lock on real tempo change
      - Silence watchdog resets between songs / when stopped
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
        # Bounded queue: drop stale frames under load instead of backing up
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=8)

        # Beat timing
        self._beats: Deque[float] = deque(maxlen=48)
        self._last_beat_ts: Optional[float] = None
        self._bpm_stable: float = 0.0
        self._deviation_beats = 0

        # Energy tracking - faster EMA for transient response
        self._rms_ema = 0.0
        self._rms_peak = 0.0  # recent peak for onset strength calculation

        # Spectral energy tracking
        self._fft_window = np.hanning(self.hop_size)
        self._bass_ema = 0.0
        self._mid_ema = 0.0
        self._high_ema = 0.0

        # Spectral flux for onset detection (better fallback than RMS gating)
        self._prev_spectrum: Optional[np.ndarray] = None
        self._flux_ema = 0.0
        self._onset_strength = 0.0

        # Precompute frequency bin indices for band extraction
        freqs = np.fft.rfftfreq(self.hop_size, 1.0 / self.sample_rate)
        self._bass_mask = (freqs >= 20) & (freqs < 150)
        self._mid_mask = (freqs >= 150) & (freqs < 4000)
        self._high_mask = (freqs >= 4000) & (freqs < 12000)

        # Config (tuneable)
        self._refractory = 0.25
        self._bpm_min, self._bpm_max = 70.0, 180.0
        self._unlock_pct = 0.20
        self._unlock_needed = 5
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
        self._rms_peak = 0.0
        self._bass_ema = 0.0
        self._mid_ema = 0.0
        self._high_ema = 0.0
        self._prev_spectrum = None
        self._flux_ema = 0.0
        self._onset_strength = 0.0

    # -------- Internals --------
    def _register_beat(self, t_now: float, rms: float):
        # Refractory to avoid double hits
        if self._last_beat_ts is not None and (t_now - self._last_beat_ts) < self._refractory:
            return
        self._last_beat_ts = t_now
        self._beats.append(t_now)

        # Compute onset strength: how hard this beat is relative to recent peak
        self._rms_peak = max(self._rms_peak * 0.95, rms)  # decay peak slowly
        strength = min(1.0, rms / max(1e-6, self._rms_peak))
        self._onset_strength = strength

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
                        self._bpm_stable = bpm_new
                        self._deviation_beats = 0
                else:
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
                high=float(self._high_ema),
                onset_strength=float(self._onset_strength),
            ))

    def _estimate_bpm_pairwise(self) -> float:
        """
        Optimized period estimate: use only last 24 beats (windowed) and skip
        pairs with large gaps to reduce O(n^2) to a practical ~200 pairs.
        """
        ts = list(self._beats)
        n = len(ts)
        if n < 6:
            return 0.0

        # Use only the most recent 24 beats for BPM estimation
        if n > 24:
            ts = ts[-24:]
            n = 24

        per_beats = []
        min_dt = 60.0 / self._bpm_max
        max_dt = 60.0 / self._bpm_min

        for i in range(n - 1):
            ti = ts[i]
            # Only compare with beats up to 8 positions ahead (reduces pairs)
            for j in range(i + 1, min(i + 9, n)):
                k = j - i
                dt = (ts[j] - ti) / k
                if min_dt <= dt <= max_dt:
                    per_beats.append(dt)

        if len(per_beats) < 4:
            return 0.0

        dt_med = _median(per_beats)
        bpm_raw = 60.0 / max(1e-6, dt_med)

        # Fold into window, prefer stability
        candidates = [bpm_raw, bpm_raw * 2.0, bpm_raw * 0.5]
        if self._bpm_stable > 0:
            best = min(candidates, key=lambda c: abs(c - self._bpm_stable))
        else:
            in_win = [c for c in candidates if self._bpm_min <= c <= self._bpm_max]
            best = min(in_win or candidates, key=lambda c: abs(c - 120.0))
        if best < self._bpm_min:
            best *= 2.0
        if best > self._bpm_max:
            best *= 0.5
        return float(best)

    def _analyze_spectrum(self, x: np.ndarray):
        """Extract bass/mid/high energy and compute spectral flux for onset detection."""
        windowed = x * self._fft_window
        spectrum = np.abs(np.fft.rfft(windowed))

        # Band energies using precomputed masks
        bass_raw = np.sum(spectrum[self._bass_mask])
        mid_raw = np.sum(spectrum[self._mid_mask])
        high_raw = np.sum(spectrum[self._high_mask])

        # Normalize by hop size
        bass = bass_raw / (self.hop_size * 2.5)
        mid = mid_raw / (self.hop_size * 8)
        high = high_raw / (self.hop_size * 2.5)

        # Smooth with EMA
        self._bass_ema = 0.7 * self._bass_ema + 0.3 * bass
        self._mid_ema = 0.7 * self._mid_ema + 0.3 * mid
        self._high_ema = 0.7 * self._high_ema + 0.3 * high

        # Spectral flux: sum of positive differences from previous frame
        if self._prev_spectrum is not None:
            diff = spectrum - self._prev_spectrum
            flux = float(np.sum(np.maximum(diff, 0)))
            flux /= (self.hop_size * 4)  # normalize
            self._flux_ema = 0.8 * self._flux_ema + 0.2 * flux
        self._prev_spectrum = spectrum

        return (self._bass_ema, self._mid_ema, self._high_ema)

    def run(self):
        self._running.set()
        last_energy_ts = time.time()  # track last significant energy, not just beats

        def callback(indata, frames, time_info, status):
            x = indata if indata.ndim == 1 else np.mean(indata, axis=1)
            try:
                self._q.put_nowait(x.astype(np.float32))
            except queue.Full:
                # Drop oldest frame if backed up, keep newest
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(x.astype(np.float32))
                except queue.Full:
                    pass

        with sd.InputStream(device=self.device_index, channels=1, samplerate=self.sample_rate,
                            blocksize=self.hop_size, callback=callback):
            while self._running.is_set():
                now = time.time()

                # Silence watchdog: reset on no significant energy (not just no beats)
                if (now - last_energy_ts) > self._silence_reset_seconds and self._rms_ema < 0.02:
                    self.reset()
                    last_energy_ts = now  # prevent repeated resets

                try:
                    x = self._q.get(timeout=0.25)
                except queue.Empty:
                    continue

                # Energy tracking - faster response to transients
                rms = float(np.sqrt(np.mean(x * x)))
                self._rms_ema = 0.75 * self._rms_ema + 0.25 * rms

                # Track significant energy for silence watchdog
                if rms > 0.02:
                    last_energy_ts = now

                # Beat detection (run BEFORE _analyze_spectrum so
                # the non-aubio path can use _prev_spectrum correctly)
                beat_now = False
                if HAVE_AUBIO:
                    is_beat = float(self._tempo(x).flatten()[0])
                    beat_now = (is_beat > 0.0)
                else:
                    # Spectral flux onset detection (better than simple RMS gating)
                    if self._prev_spectrum is not None and self._flux_ema > 0:
                        cur_spectrum = np.abs(np.fft.rfft(x * self._fft_window))
                        flux = float(np.sum(np.maximum(
                            cur_spectrum - self._prev_spectrum, 0
                        ))) / (self.hop_size * 4)
                        thr = max(0.05, 1.8 * self._flux_ema)
                        beat_now = (flux > thr) and (rms > 0.03)
                    else:
                        # Startup: simple adaptive gate
                        thr = max(0.05, 0.7 * self._rms_ema)
                        beat_now = (rms > thr)

                # Spectral analysis (updates _prev_spectrum and band EMAs)
                self._analyze_spectrum(x)

                if beat_now:
                    self._register_beat(now, rms)

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
