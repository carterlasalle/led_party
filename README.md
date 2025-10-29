# KSIPZE LightDesk — Live Autoloops Edition (macOS)
Adds a **SoundSwitch‑style Live Autoloops engine** on top of the earlier build.

**What’s new**
- Live **Autoloops** generator: beat/bar/phrase‑aware lighting actions
- **Styles**: House, EDM, Hip‑Hop, Chill (pattern weights + palettes)
- **On‑beat actions** (blinder, strobe ramps, color swaps, vendor modes)
- 4‑beat bars, 8‑bar phrases (32 beats) with big accents on phrase changes
- Energy sensitivity from audio RMS to intensify effects on drops/peaks
- Still supports manual control, presets, brightness, vendor modes, blackout, white flash

> Works on macOS with built‑in Bluetooth. For system audio, install a loopback input like BlackHole 2ch and choose it under **Audio Sync → Input Device**.

## Run
```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```
