# KSIPZE LightDesk — Live Autoloops Edition (macOS)

A **professional-grade lighting control system** with SoundSwitch-style Live Autoloops for Bluetooth LED controllers. Features real-time beat detection, intelligent energy analysis, and musical structure awareness for dynamic, responsive lighting effects.

## ✨ Features

### Core Functionality
- **Live Autoloops Engine**: Beat/bar/phrase-aware lighting choreography
- **4 Musical Styles**: House, EDM, Hip-Hop, Chill (different pattern weights)
- **Color Palettes**: ND, Warm, Cool, Neon with auto-cycling
- **Dual Light Control**: Manage two lights independently (A/B) or synchronized (Both)

### Audio Analysis
- **Real-time Beat Detection**: Uses aubio if available, falls back to adaptive RMS gating
- **Robust BPM Tracking**: Pairwise lag estimation, auto-locks tempo (70-180 BPM)
- **Spectral Analysis**: Bass/mid/high frequency tracking for intelligent drop/build detection
- **Silence Watchdog**: Auto-resets between songs

### Intelligent Effects
- **Drop Detection**: Staged 2-beat countdown (blackout → rainbow strobe) with bass spike analysis
- **Build Detection**: Rising energy + hi-hat tracking triggers build-flash sequences
- **Breakdown Detection**: Post-drop chill sections with smooth transitions
- **Energy Tiers**: 3-level classification (LOW/MED/HIGH) with adaptive thresholds
- **Phrase Awareness**: 32-beat phrases with accent hits on boundaries

### Interactive Controls
- **Color Flash on Beat**: Static color OR rainbow cycling on every beat
- **Live Color Wheel**: Real-time color updates with persistent BLE connections (<50ms latency)
- **Manual Override**: Force energy tiers, manually trigger drops/builds
- **Sensitivity Control**: 0.5x-2.0x multiplier for energy detection
- **Alternating Effects**: 5 A/B patterns (flash, on/off, colors, chase, opposite)
- **Vendor Modes**: 21 built-in hardware effects (strobes, pulses, jumps)

### Developer Tools
- **Diagnostic Logging**: CSV beat-by-beat logs with 17 metrics
- **Audio Debug Mode**: 5-second terminal output for troubleshooting
- **Protocol Auto-Detection**: Supports multiple BLE LED families

## 🚀 Quick Start

### Installation
```bash
# Clone and setup
git clone <your-repo>
cd led_party

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run application
python app.py
```

### First Use
1. **Scan for Devices**: Click "Scan" to discover nearby BLE LED lights
2. **Assign Lights**: Select device, click "Assign → A" (and/or "Assign → B")
3. **Setup Audio**: 
   - For system audio: Install [BlackHole 2ch](https://existential.audio/blackhole/)
   - Configure as Multi-Output Device in Audio MIDI Setup
   - Select "BlackHole 2ch" in app's Input Device dropdown
4. **Choose Style & Palette**: Pick your vibe (House/EDM/Hip-Hop/Chill)
5. **Start Audio Sync**: Click "Start Audio Sync" (wait for BPM lock)
6. **Start Live Autoloops**: Click "Start Live Autoloops"
7. **Rock Out**: Lights will intelligently react to your music!

## 🔧 Troubleshooting

### Lights Not Reacting to Music

**1. Use Audio Debug Tool**
- Click "Audio Debug (5s)" button
- Watch terminal output for 5 seconds
- Compare values to expected ranges:

```
Expected Values:
  RMS: Silence=0.01-0.03, Music=0.05-0.15, Drops=0.20-0.40+
  Bass/High: Should be similar scale to RMS (0.0-1.0)
  BPM: Should lock within 5-10 beats
```

**2. Common Issues**

| Issue | Solution |
|-------|----------|
| BPM shows "--" | Wrong audio device, or no music playing |
| RMS too low (<0.03) | Increase system volume or check BlackHole routing |
| No beat detection | Enable microphone access in macOS Privacy settings |
| Lights flash but no pattern | Autoloops not started (click "Start Live Autoloops") |
| Connection fails | Light already connected elsewhere, turn off/on |

**3. Sensitivity Tuning**
- If lights react too much: Lower sensitivity slider (0.5x-0.8x)
- If lights don't react enough: Raise sensitivity slider (1.2x-2.0x)
- Or use Manual Override to force energy tiers

**4. Check Diagnostic Logs**
- CSV logs auto-created: `~/lightdesk_log_YYYYMMDD_HHMMSS.csv`
- Open in Excel/Numbers to analyze:
  - `ema_fast` values should vary 0.03-0.15 during music
  - `energy_tier` should transition between LOW/MED/HIGH
  - `drop_detected` flags should align with actual drops

### BLE Connection Issues

**Power-on on connection:** The app automatically sends a power-on command (`0xCC 0x23 0x33`) when connecting to lights. This wakes devices in standby so color/mode commands work immediately—no need to "turn on from phone first."

**Persistent "Connect failed"**
- Lights may be paired to another device (phone/tablet)
- Power cycle the LED controller (unplug/replug)
- Move closer to computer (within 10 feet)
- Restart Bluetooth: `System Settings → Bluetooth → Turn Off/On`

**Lights disconnect randomly**
- Check battery if battery-powered
- Reduce distance between computer and lights
- Disable other Bluetooth devices causing interference

## 📊 Advanced Usage

### Manual Control Mode
1. Toggle "Manual Override" radio button
2. Force energy tier: LOW/MED/HIGH buttons
3. Manually trigger: "Trigger Build" or "Trigger Drop"
4. Use during setup/testing to verify effects work

### Alternating Light Effects
1. Enable "Alternating Mode" checkbox
2. Choose pattern (Alternating Flash, Chase, etc.)
3. Adjust speed slider (100-2000ms)
4. Requires both Light A and Light B assigned

### Color Flash on Beat
1. Enable "Enable Flash on Beat" checkbox
2. Choose mode:
   - **Static Color**: Pick one color, flashes that color every beat
   - **Cycle Colors**: Rotates through rainbow palette on each beat
3. Flash duration adapts to energy tier (longer on drops)

## 🎛️ Musical Style Guide

| Style | Characteristics | Best For |
|-------|----------------|----------|
| **House** | Smooth pulses, subtle swaps | Four-on-the-floor, deep house |
| **EDM** | Aggressive accents, frequent strobes | Festival drops, big room |
| **Hip-Hop** | Heavy pulses, minimal strobes | Trap, hip-hop, R&B |
| **Chill** | Gentle pulses, rare accents | Lo-fi, ambient, chill-out |

## 📁 File Structure

```
led_party/
├── app.py              # Main GUI application (PyQt6)
├── audiosync.py        # Beat detection & audio analysis
├── autoloops.py        # Lighting choreography engine
├── ble_control.py      # Bluetooth LED protocol implementation
├── logger.py           # Diagnostic CSV logging
├── modes.py            # Vendor mode constants
├── requirements.txt    # Python dependencies
├── README.md           # This file
└── PROTOCOL.md         # BLE protocol reverse engineering docs
```

## 🔬 Technical Details

- **Audio Processing**: 44.1kHz sample rate, 1024-sample hop size
- **Beat Latency**: ~20-50ms (depends on BLE connection mode)
- **BPM Range**: 70-180 BPM with half/double folding
- **Energy Detection**: Triple EMA (fast/med/long) with ratio analysis
- **Supported Devices**: HappyLighting, ELK-BLEDOM, Triones, QHM-series, generic BLE LED controllers
- **Power-on on connect**: Sends wake command (`0xCC 0x23 0x33`) for both FFE9 and FFF3 protocols; fixes cold-connect / standby behavior

For deep technical details on the BLE protocol, see **[PROTOCOL.md](PROTOCOL.md)**.

## 🐛 Known Issues

1. **First connection slow**: Initial BLE probe takes 2-3 seconds (protocol auto-detect)
2. **CSV logs accumulate**: Manually delete old logs from `~/`
3. **No hot-swap**: Must restart app to switch audio devices
4. **macOS only**: Uses CoreBluetooth via bleak (Linux/Windows untested)

## 📝 License

This project documents reverse-engineered BLE protocols for educational purposes. Use responsibly and respect manufacturer protocols.

## 🤝 Contributing

Found a bug? Have a new protocol to add? Open an issue or PR!

For protocol reverse engineering guidance, see **[PROTOCOL.md](PROTOCOL.md)**.
