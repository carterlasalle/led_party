# KSIPZE LightDesk — Live Autoloops Edition (macOS)
Adds a **SoundSwitch‑style Live Autoloops engine** on top of the earlier build.

**What's new**
- Live **Autoloops** generator: beat/bar/phrase‑aware lighting actions
- **Styles**: House, EDM, Hip‑Hop, Chill (pattern weights + palettes)
- **On‑beat actions** (blinder, strobe ramps, color swaps, vendor modes)
- 4‑beat bars, 8‑bar phrases (32 beats) with big accents on phrase changes
- **Spectral analysis**: Bass/mid/high frequency tracking for drop/build detection
- **Energy-adaptive effects**: Detects drops, builds, breakdowns with dynamic intensity
- **Diagnostic logging**: CSV beat-by-beat logs for tuning and debugging
- Still supports manual control, presets, brightness, vendor modes, blackout, white flash

> Works on macOS with built‑in Bluetooth. For system audio, install a loopback input like BlackHole 2ch and choose it under **Audio Sync → Input Device**.

## Run
```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

## Troubleshooting Audio

If lights aren't reacting to music:

1. **Click "Audio Debug (5s)"** - Prints beat-by-beat stats to terminal
2. **Check expected values:**
   - RMS: Silence=0.01-0.03, Music=0.05-0.15, Drops=0.20-0.40+
   - Bass/High: Should be similar scale to RMS (0.0-1.0)
   - BPM: Should lock within 5-10 beats

3. **Common issues:**
   - Wrong audio device selected
   - BlackHole not installed/configured
   - System volume too low
   - Need to enable "system audio" in macOS Privacy settings

4. **CSV logs** are automatically created in your home directory (`~/lightdesk_log_YYYYMMDD_HHMMSS.csv`) when autoloops are active
