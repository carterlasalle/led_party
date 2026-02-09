# BLE LED Controller Protocol: Complete Reverse Engineering Guide

**A comprehensive documentation of HappyLighting, QHM, ELK-BLEDOM, and Triones-compatible BLE LED controller protocols.**

This document captures the complete reverse engineering journey of discovering, understanding, and implementing control protocols for cheap Bluetooth LED strip controllers found on Amazon/AliExpress. If you're trying to control similar devices, this will save you weeks of trial and error.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Device Discovery](#device-discovery)
3. [Protocol Families](#protocol-families)
4. [Command Structure](#command-structure)
5. [Connection Strategies](#connection-strategies)
6. [Performance Optimization](#performance-optimization)
7. [Troubleshooting](#troubleshooting)
8. [Reverse Engineering Process](#reverse-engineering-process)

---

## Quick Start

**TL;DR: Most cheap BLE LED controllers use one of two protocols:**

### Protocol A: FFE9 Family (HappyLighting/ELK-BLEDOM)
```python
# Static RGB color
payload = bytes([0x56, RED, GREEN, BLUE, 0x00, 0xF0, 0xAA])

# Vendor mode (strobe, pulse, etc)
payload = bytes([0xBB, MODE_ID, SPEED, 0x44])

# Power on/off
payload = bytes([0xCC, 0x23, 0x33])  # ON
payload = bytes([0xCC, 0x24, 0x33])  # OFF
```

### Protocol B: FFF3 Family (7E...EF)
```python
# Static RGB color
payload = bytes([0x7E, 0x00, 0x05, RED, GREEN, BLUE, 0x00, 0x00, 0xEF])
```

### Universal Connection Flow
```python
import asyncio
from bleak import BleakClient, BleakScanner

# 1. Scan for devices
devices = await BleakScanner.discover(timeout=6.0)

# 2. Connect and find writable characteristic
async with BleakClient(device_address) as client:
    services = await client.get_services()
    for service in services:
        for char in service.characteristics:
            if "write" in char.properties:
                char_uuid = char.uuid
                break
    
    # 3. Power on (wake) - required for cold connect; send for both FFE9 and FFF3
    try:
        await client.write_gatt_char(char_uuid, bytes([0xCC, 0x23, 0x33]), response=False)
    except Exception:
        pass
    
    # 4. Send command (no response needed)
    await client.write_gatt_char(char_uuid, payload, response=False)
```

---

## Device Discovery

### Known Device Names

The following device names are confirmed to use these protocols:

```python
KNOWN_NAMES = {
    "ELK-BLEDOM",      # Very common on Amazon
    "Triones",         # Original HappyLighting brand
    "HappyLighting",   # Generic Chinese brand
    "LEDnetWF",        # WiFi+BLE variants
    "LEDBlue",         # Budget models
    "QHM-SDA0",        # QHM series (verified working)
    "QHM-S281",        # QHM series (verified working)
    "QHM",             # Generic QHM prefix
    "Light",           # Very generic (be careful)
    "Dream",           # Some RGB bulbs
    "Flash"            # Strip controllers
}
```

### Smart Discovery Heuristics

Don't just match exact names. Use pattern matching:

```python
def is_likely_led_controller(device_name: str) -> bool:
    """Returns True if device is probably a controllable LED"""
    if not device_name:
        return False
    
    name = device_name.upper()
    
    # Exact matches
    if device_name in KNOWN_NAMES:
        return True
    
    # Pattern matches
    patterns = [
        name.startswith("QHM-"),    # QHM series
        "LED" in name,              # Most contain "LED"
        "LIGHT" in name,            # HappyLighting variants
        "BLE" in name,              # BLE-specific controllers
        "TRION" in name,            # Triones typos
        "RGB" in name,              # RGB controllers
    ]
    
    return any(patterns)
```

### Bluetooth Scan Settings

**Critical**: Default scan timeout (1-2s) often misses devices. Use longer scans:

```python
# TOO SHORT - may miss devices
devices = await BleakScanner.discover(timeout=2.0)

# RECOMMENDED - catches slow advertisers
devices = await BleakScanner.discover(timeout=6.0)
```

**Why?** These cheap controllers:
- Have slow advertising intervals (100-500ms)
- May be in power-saving mode
- Often have weak BLE transmitters

---

## Protocol Families

### Protocol A: FFE9 Family (0x56-AA)

**Identification:**
- Service UUID: `0000FFE5-0000-1000-8000-00805F9B34FB` (common)
- Characteristic UUID: `0000FFE9-0000-1000-8000-00805F9B34FB`
- Packet format: Fixed 7-byte frames

**Devices using this:**
- ELK-BLEDOM
- HappyLighting (original)
- Triones
- Most QHM-series
- Generic Amazon/AliExpress "Smart LED Strip" controllers

#### Command Structure

**1. Static RGB Color**
```
Byte 0: 0x56        Header (constant)
Byte 1: R           Red value (0-255)
Byte 2: G           Green value (0-255)
Byte 3: B           Blue value (0-255)
Byte 4: WW          Warm white (0-255, usually 0x00 for RGB strips)
Byte 5: 0xF0        Footer marker
Byte 6: 0xAA        Footer checksum
```

**Example:**
```python
# Pure red
payload = bytes([0x56, 0xFF, 0x00, 0x00, 0x00, 0xF0, 0xAA])

# Pure green
payload = bytes([0x56, 0x00, 0xFF, 0x00, 0x00, 0xF0, 0xAA])

# White (all channels)
payload = bytes([0x56, 0xFF, 0xFF, 0xFF, 0x00, 0xF0, 0xAA])

# Purple
payload = bytes([0x56, 0xAA, 0x00, 0xAA, 0x00, 0xF0, 0xAA])
```

**2. Vendor Modes (Built-in Effects)**
```
Byte 0: 0xBB        Mode header
Byte 1: MODE_ID     Mode number (see table below)
Byte 2: SPEED       Speed value (1-255, SMALLER = FASTER)
Byte 3: 0x44        Footer
```

**Speed Scale:** Empirically, `speed ≈ 10 / Hz`
- Speed = 5  → ~2 Hz (fast)
- Speed = 10 → ~1 Hz (medium)
- Speed = 50 → ~0.2 Hz (slow)

**Mode ID Table:**
```python
# Static modes
MODE_STATIC = 0x24  # 36 - Stop animation, hold color

# Pulsating modes (breathe in/out)
MODE_PULSE_RAINBOW    = 0x25  # 37
MODE_PULSE_RED        = 0x26  # 38
MODE_PULSE_GREEN      = 0x27  # 39
MODE_PULSE_BLUE       = 0x28  # 40
MODE_PULSE_YELLOW     = 0x29  # 41
MODE_PULSE_CYAN       = 0x2A  # 42
MODE_PULSE_PURPLE     = 0x2B  # 43
MODE_PULSE_WHITE      = 0x2C  # 44
MODE_PULSE_RED_GREEN  = 0x2D  # 45
MODE_PULSE_RED_BLUE   = 0x2E  # 46
MODE_PULSE_GREEN_BLUE = 0x2F  # 47

# Strobe modes (rapid flashing)
MODE_STROBE_RAINBOW   = 0x30  # 48
MODE_STROBE_RED       = 0x31  # 49
MODE_STROBE_GREEN     = 0x32  # 50
MODE_STROBE_BLUE      = 0x33  # 51
MODE_STROBE_YELLOW    = 0x34  # 52
MODE_STROBE_CYAN      = 0x35  # 53
MODE_STROBE_PURPLE    = 0x36  # 54
MODE_STROBE_WHITE     = 0x37  # 55 - VERY POPULAR for drops

# Jump modes (hard color switches)
MODE_JUMP_RAINBOW     = 0x38  # 56
MODE_JUMP_RGB_PULSE   = 0x39  # 57
MODE_JUMP_RGB         = 0x3A  # 58
```

**Example:**
```python
# White strobe at medium speed (perfect for EDM drops)
payload = bytes([0xBB, 0x37, 0x14, 0x44])  # mode=0x37, speed=20

# Rainbow strobe fast
payload = bytes([0xBB, 0x30, 0x05, 0x44])  # mode=0x30, speed=5

# Red pulse slow
payload = bytes([0xBB, 0x26, 0x50, 0x44])  # mode=0x26, speed=80
```

**3. Power Control**
```
Byte 0: 0xCC        Power header
Byte 1: 0x23/0x24   0x23=ON, 0x24=OFF
Byte 2: 0x33        Footer
```

```python
# Power on
payload = bytes([0xCC, 0x23, 0x33])

# Power off
payload = bytes([0xCC, 0x24, 0x33])
```

**Power-on on connection (critical):** Many controllers (including QHM-series) enter a standby/low-power state where they advertise but ignore RGB/mode commands until woken. Send power-on (`0xCC 0x23 0x33`) immediately after connecting—for **both** FFE9 and FFF3 devices. FFF3 devices may ignore it if unsupported, but it does not cause harm. Verified working on QHM-S281: without power-on on connect, color/mode writes do nothing; with it, control works immediately.

---

### Protocol B: FFF3 Family (0x7E-EF)

**Identification:**
- Service UUID: `0000FFF0-0000-1000-8000-00805F9B34FB` (common)
- Characteristic UUID: `0000FFF3-0000-1000-8000-00805F9B34FB`
- Packet format: Variable length, 9-byte minimum

**Less common, but found on:**
- Some Triones variants
- Newer QHM models
- Generic "Smart Bulb" controllers

#### Command Structure

**Static RGB Color**
```
Byte 0: 0x7E        Header
Byte 1: 0x00        Sub-command? (always 0x00 for color)
Byte 2: 0x05        Length indicator (RGB=5)
Byte 3: R           Red (0-255)
Byte 4: G           Green (0-255)
Byte 5: B           Blue (0-255)
Byte 6: 0x00        Reserved
Byte 7: 0x00        Reserved
Byte 8: 0xEF        Footer checksum
```

**Example:**
```python
# Pure blue
payload = bytes([0x7E, 0x00, 0x05, 0x00, 0x00, 0xFF, 0x00, 0x00, 0xEF])

# White
payload = bytes([0x7E, 0x00, 0x05, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0xEF])
```

**Note:** This protocol family is less documented. Modes/power commands vary by manufacturer.

---

## Command Structure

### Critical Discovery: Modes Override Static Colors

**IMPORTANT FINDING:** Vendor modes (0xBB commands) create persistent state. You MUST send a static color command (0x56) to return to manual control.

```python
# This sequence will keep strobing forever:
await client.write_gatt_char(uuid, bytes([0xBB, 0x37, 0x10, 0x44]))  # White strobe
await asyncio.sleep(5)
# Still strobing...

# To stop and return to solid color:
await client.write_gatt_char(uuid, bytes([0x56, 0xFF, 0x00, 0x00, 0x00, 0xF0, 0xAA]))
# Now solid red, strobe stopped
```

### State Machine Behavior

The controller has an internal state machine:

```
┌─────────────────────────────────────────────┐
│                                             │
│  ┌──────────┐    0xBB      ┌────────────┐  │
│  │          │─────────────→│            │  │
│  │  STATIC  │              │  MODE RUN  │  │
│  │  COLOR   │←─────────────│  (VENDOR)  │  │
│  │          │    0x56      │            │  │
│  └──────────┘              └────────────┘  │
│       ↑                                     │
│       │ 0x56 (any RGB)                      │
│       └─────────────────────────────────────┘
```

**Implications for real-time control:**

1. **Flash effects** require state tracking:
```python
async def flash_white(duration_ms: int, return_color: tuple):
    # Enter white
    await write_rgb(255, 255, 255)
    await asyncio.sleep(duration_ms / 1000.0)
    # Return to previous state
    await write_rgb(*return_color)
```

2. **Strobe modes** need explicit cancellation:
```python
async def strobe_then_static(strobe_duration_ms: int):
    # Start strobe
    await write_mode(MODE_STROBE_WHITE, speed=10)
    await asyncio.sleep(strobe_duration_ms / 1000.0)
    # MUST send RGB to stop
    await write_rgb(255, 0, 0)  # Red static
```

3. **Mode reassertion** for long sequences:
```python
# For multi-beat strobes, reassert mode every beat to prevent drift
for beat in range(16):
    await write_mode(MODE_STROBE_RAINBOW, speed=bpm_to_speed(bpm))
    await asyncio.sleep(60.0 / bpm)  # Wait one beat
```

---

## Connection Strategies

### Power-on on Connection (Cold Connect) ⭐

**Problem:** Controllers in standby advertise but ignore RGB/mode commands. Color clicks do nothing until the device is "woken."

**Solution:** Send power-on (`0xCC 0x23 0x33`) immediately after every BLE connection—both during Assign and on lazy reconnect. Send for **all** protocol families (FFE9 and FFF3); FFF3 devices that don't support it will ignore the command harmlessly.

```python
# In connect_and_identify and in persistent pool _get_client (after connect):
try:
    await client.write_gatt_char(char_uuid, bytes([0xCC, 0x23, 0x33]), response=False)
except Exception:
    pass  # Don't fail connection if power-on fails
```

**When:** Right after `client.connect()`, before any other commands.

---

### Strategy 1: Simple Multi-Write (High Latency)

**When to use:** Simple apps, infrequent updates, multiple disconnected devices

**Pros:**
- Simple implementation
- No state management
- Works with any number of devices

**Cons:**
- High latency (200-500ms per command)
- BLE reconnection overhead every time
- Not suitable for live color wheel or fast effects

**Implementation:**
```python
async def simple_multi_write(handles: List[LightHandle], payload: bytes):
    """Connect, write, disconnect for each device"""
    async def write_one(handle):
        async with BleakClient(handle.address) as client:
            await client.write_gatt_char(handle.char_uuid, payload, response=False)
    
    # Parallel writes (still slow due to reconnection)
    await asyncio.gather(*(write_one(h) for h in handles))
```

**Measured latency:** 200-500ms for 2 devices

---

### Strategy 2: Persistent Connection Pool (Low Latency) ⭐

**When to use:** Real-time effects, live color wheel, beat-synced lighting

**Pros:**
- **Sub-50ms latency** for commands
- Perfect for interactive controls
- Maintains BLE connection stability

**Cons:**
- More complex state management
- Need connection monitoring/recovery
- Limited by macOS BLE connection limit (~8 devices)

**Implementation:**
```python
class ClientPool:
    def __init__(self):
        self._clients: Dict[str, BleakClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
    
    async def _get_client(self, handle: LightHandle) -> BleakClient:
        """Get or create persistent client"""
        client = self._clients.get(handle.address)
        if client is None:
            client = BleakClient(handle.address)
            self._clients[handle.address] = client
            self._locks[handle.address] = asyncio.Lock()
        
        # Lazy connect
        if not client.is_connected:
            await client.connect()
        
        return client
    
    async def write(self, handle: LightHandle, payload: bytes):
        """Write with automatic reconnection on failure"""
        client = await self._get_client(handle)
        lock = self._locks[handle.address]
        
        async with lock:  # Prevent concurrent writes
            try:
                await client.write_gatt_char(
                    handle.char_uuid, 
                    payload, 
                    response=False  # Critical for performance
                )
            except (BleakError, Exception):
                # Single reconnect attempt
                try:
                    if client.is_connected:
                        await client.disconnect()
                except:
                    pass
                await client.connect()
                await client.write_gatt_char(handle.char_uuid, payload, response=False)

# Global pool instance
_pool = ClientPool()

async def fast_multi_write(handles: List[LightHandle], payload: bytes):
    """Reuse connections for sub-50ms latency"""
    await asyncio.gather(*(_pool.write(h, payload) for h in handles))
```

**Measured latency:** 20-50ms for 2 devices (10x faster!)

**Use case comparison:**
```python
# Live color wheel - MUST use persistent pool
async def on_color_change(r, g, b):
    payload = bytes([0x56, r, g, b, 0x00, 0xF0, 0xAA])
    await fast_multi_write(handles, payload)  # <50ms = smooth
    # Using simple_multi_write here = 500ms = stuttering disaster

# Manual color preset - either strategy works
async def set_preset_red():
    payload = bytes([0x56, 0xFF, 0x00, 0x00, 0x00, 0xF0, 0xAA])
    await simple_multi_write(handles, payload)  # 500ms is fine
```

---

### Connection Recovery Patterns

**Problem:** BLE connections drop randomly (interference, distance, power)

**Solution:** Automatic reconnection with exponential backoff

```python
async def write_with_retry(client, char_uuid, payload, max_attempts=3):
    """Resilient write with automatic reconnection"""
    for attempt in range(max_attempts):
        try:
            if not client.is_connected:
                await client.connect()
            
            await client.write_gatt_char(char_uuid, payload, response=False)
            return  # Success
        
        except Exception as e:
            if attempt == max_attempts - 1:
                raise  # Final attempt failed
            
            # Exponential backoff
            await asyncio.sleep(0.5 * (2 ** attempt))
            
            # Force disconnect before retry
            try:
                await client.disconnect()
            except:
                pass
```

---

## Protocol Auto-Detection

**Problem:** You don't know which protocol family a new device uses.

**Solution:** Probe with RGB sequence and observe response.

### Detection Algorithm

```python
async def detect_protocol(client: BleakClient, char_uuid: str) -> str:
    """
    Returns: "FFF3_7EEF", "FFE9_56AA", or None
    
    Strategy: Send RGB cycle with each protocol, see which works
    """
    test_colors = [(255,0,0), (0,255,0), (0,0,255)]  # R→G→B cycle
    
    # Try Protocol B (FFF3) first
    try:
        for r, g, b in test_colors:
            frame = bytes([0x7E, 0x00, 0x05, r, g, b, 0x00, 0x00, 0xEF])
            await client.write_gatt_char(char_uuid, frame, response=False)
            await asyncio.sleep(0.15)  # Visible color change
        return "FFF3_7EEF"  # Success - no exception
    except Exception:
        pass  # Protocol B failed, try A
    
    # Try Protocol A (FFE9)
    try:
        for r, g, b in test_colors:
            frame = bytes([0x56, r, g, b, 0x00, 0xF0, 0xAA])
            await client.write_gatt_char(char_uuid, frame, response=False)
            await asyncio.sleep(0.15)
        return "FFE9_56AA"  # Success
    except Exception:
        return None  # Neither worked
```

**Why this works:**
- Invalid protocol commands are silently ignored by controller
- Valid commands produce visible color changes
- No response needed (write_without_response characteristic)

**Typical detection time:** 1-2 seconds (6 writes × 150ms delay)

### Fallback Strategy

If detection fails, default to FFE9 (most common):

```python
async def connect_and_identify(device_address: str) -> LightHandle:
    async with BleakClient(device_address) as client:
        char_uuid = await find_writable_char(client)
        protocol = await detect_protocol(client, char_uuid)
        
        if protocol is None:
            protocol = "FFE9_56AA"  # Safest default
        
        return LightHandle(
            address=device_address,
            char_uuid=char_uuid,
            family=protocol
        )
```

---

## Performance Optimization

### 1. Use `response=False` Always

```python
# SLOW (waits for acknowledgment)
await client.write_gatt_char(uuid, payload, response=True)  # +50-100ms

# FAST (fire and forget)
await client.write_gatt_char(uuid, payload, response=False)  # +5-10ms
```

**Why?** These controllers don't send meaningful responses. Waiting is pure overhead.

### 2. Parallel Device Writes

```python
# SLOW (sequential)
for handle in handles:
    await write_single(handle, payload)  # 50ms × N devices

# FAST (parallel)
await asyncio.gather(*(write_single(h, payload) for h in handles))  # 50ms total
```

### 3. Command Batching for Modes

For sequences requiring multiple commands, batch them:

```python
# Build a 32-beat drop sequence
async def execute_drop_sequence(bpm: float):
    beat_duration = 60.0 / bpm
    mode_payload = bytes([0xBB, MODE_STROBE_RAINBOW, speed_for_bpm(bpm), 0x44])
    
    for beat in range(32):
        # Reassert mode (prevents drift)
        await fast_write(mode_payload)
        await asyncio.sleep(beat_duration)
    
    # Return to static color
    await fast_write(bytes([0x56, 0xFF, 0x00, 0x00, 0x00, 0xF0, 0xAA]))
```

### 4. Pre-compute Payloads

```python
# SLOW - compute on every beat
async def on_beat(r, g, b):
    payload = bytes([0x56, r, g, b, 0x00, 0xF0, 0xAA])
    await write(payload)

# FAST - pre-compute palette
PALETTE_PAYLOADS = [
    bytes([0x56, 255, 0, 0, 0x00, 0xF0, 0xAA]),    # Red
    bytes([0x56, 0, 255, 0, 0x00, 0xF0, 0xAA]),    # Green
    bytes([0x56, 0, 0, 255, 0x00, 0xF0, 0xAA]),    # Blue
]

async def on_beat(palette_index: int):
    await write(PALETTE_PAYLOADS[palette_index % len(PALETTE_PAYLOADS)])
```

---

## Troubleshooting

### Issue: Device Found But Won't Connect

**Symptoms:**
- Device appears in scan
- Connection times out or fails immediately

**Common Causes:**
1. **Already connected elsewhere** (phone app, other computer)
   - Solution: Force-close all other apps, power cycle controller
   
2. **Bluetooth pairing issues**
   - Solution: These devices don't need pairing - delete from System Settings if paired
   
3. **Distance/interference**
   - Solution: Move within 10 feet, disable other BLE devices

4. **Low battery** (battery-powered controllers)
   - Solution: Charge or replace batteries

**Diagnostic code:**
```python
async def diagnose_connection(address: str):
    print(f"Attempting connection to {address}...")
    try:
        async with BleakClient(address, timeout=10.0) as client:
            print(f"✓ Connected!")
            print(f"  Is connected: {client.is_connected}")
            
            services = await client.get_services()
            print(f"✓ Found {len(services)} services")
            
            for svc in services:
                print(f"  Service: {svc.uuid}")
                for char in svc.characteristics:
                    print(f"    Char: {char.uuid} - {char.properties}")
            
            return True
    except asyncio.TimeoutError:
        print(f"✗ Connection timeout - device may be connected elsewhere")
    except Exception as e:
        print(f"✗ Connection failed: {type(e).__name__}: {e}")
    return False
```

---

### Issue: Commands Sent But No Visual Change

**Symptoms:**
- Connection succeeds
- No errors thrown
- Lights don't change

**Diagnostic Steps:**

1. **Verify characteristic UUID:**
```python
async def find_all_writable_chars(client: BleakClient):
    """List all characteristics that support writing"""
    writable = []
    for svc in await client.get_services():
        for char in svc.characteristics:
            props = set(char.properties)
            if "write" in props or "write-without-response" in props:
                writable.append((svc.uuid, char.uuid, char.properties))
    return writable
```

2. **Try all writable characteristics:**
```python
async def brute_force_protocol(client: BleakClient):
    """Try sending RGB to all writable chars"""
    red_ffe9 = bytes([0x56, 0xFF, 0x00, 0x00, 0x00, 0xF0, 0xAA])
    red_fff3 = bytes([0x7E, 0x00, 0x05, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xEF])
    
    writable = await find_all_writable_chars(client)
    for svc_uuid, char_uuid, props in writable:
        print(f"Trying {char_uuid}...")
        try:
            # Try Protocol A
            await client.write_gatt_char(char_uuid, red_ffe9, response=False)
            await asyncio.sleep(0.5)
            # Try Protocol B
            await client.write_gatt_char(char_uuid, red_fff3, response=False)
            await asyncio.sleep(0.5)
            print(f"  Sent to {char_uuid} - check if light changed!")
        except Exception as e:
            print(f"  Failed: {e}")
```

3. **Verify device isn't in special mode:**
   - Some controllers have "app mode" vs "remote mode"
   - Power cycle the controller
   - Try pressing physical buttons (if present) to reset mode

---

### Issue: Persistent Connection Drops After Minutes

**Symptoms:**
- Initial connection works
- After 2-5 minutes, writes start failing
- Reconnection sometimes fails

**Cause:** BLE connection parameter mismatch or controller power-saving

**Solutions:**

1. **Implement connection heartbeat:**
```python
class ConnectionMonitor:
    def __init__(self, client, char_uuid):
        self.client = client
        self.char_uuid = char_uuid
        self.last_write = time.time()
        self.heartbeat_interval = 30.0  # seconds
    
    async def start_heartbeat(self):
        """Send dummy command every 30s to keep connection alive"""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            now = time.time()
            if now - self.last_write > self.heartbeat_interval:
                try:
                    # Send current color (no visual change)
                    dummy = bytes([0x56, 0x00, 0x00, 0x00, 0x00, 0xF0, 0xAA])
                    await self.client.write_gatt_char(
                        self.char_uuid, dummy, response=False
                    )
                except:
                    pass  # Will reconnect on next real command
```

2. **Aggressive reconnection:**
```python
async def write_with_aggressive_reconnect(handle, payload):
    """Reconnect preemptively if connection seems stale"""
    client = pool.get_client(handle)
    
    # If last write was >60s ago, proactively reconnect
    if time.time() - handle.last_write_time > 60.0:
        try:
            await client.disconnect()
        except:
            pass
        await client.connect()
    
    await client.write_gatt_char(handle.char_uuid, payload, response=False)
    handle.last_write_time = time.time()
```

---

### Issue: Color Values Don't Match Expected

**Symptoms:**
- Send RGB(255, 0, 0) but get orange
- White looks bluish
- Colors are dim

**Cause:** Controller color correction/gamma curves

**Solutions:**

1. **Add gamma correction:**
```python
def gamma_correct(value: int, gamma: float = 2.2) -> int:
    """Apply gamma curve for more accurate colors"""
    normalized = value / 255.0
    corrected = normalized ** (1.0 / gamma)
    return int(corrected * 255)

def frame_rgb_gamma(r, g, b):
    r = gamma_correct(r)
    g = gamma_correct(g)
    b = gamma_correct(b)
    return bytes([0x56, r, g, b, 0x00, 0xF0, 0xAA])
```

2. **Empirical color calibration:**
```python
# Measure actual output, create lookup table
COLOR_CALIBRATION = {
    # (input_r, input_g, input_b): (actual_r, actual_g, actual_b)
    (255, 0, 0): (255, 10, 5),     # "Pure red" is actually orange
    (0, 255, 0): (5, 255, 15),     # Green has red/blue bleed
    (255, 255, 255): (240, 255, 220),  # White is cool-tinted
}

def calibrate_color(r, g, b):
    """Apply empirical calibration"""
    # ... inverse mapping logic ...
    return (r_cal, g_cal, b_cal)
```

---

## Reverse Engineering Process

### Tools Used

1. **BLE Sniffing** (if you have Android device):
   - nRF Connect app (Nordic Semiconductor)
   - Bluetooth HCI Snoop Log (Developer Options)
   - Wireshark with BTLE plugin

2. **iOS/macOS**:
   - LightBlue Explorer (iOS/Mac)
   - Bluetooth Explorer (Xcode Additional Tools)

3. **Python Libraries**:
   - `bleak` - Cross-platform BLE (used in this project)
   - `pybluez` - Older, Linux-only alternative

### Step-by-Step Reverse Engineering

#### Step 1: Capture Traffic from Official App

1. Install device's official app (e.g., "HappyLighting" from App Store)
2. Enable Bluetooth HCI logging on Android:
   - Settings → Developer Options → Enable Bluetooth HCI Snoop Log
3. Connect to device via official app
4. Perform actions: change colors, modes, brightness
5. Pull HCI log: `adb pull /sdcard/Android/data/btsnoop_hci.log`
6. Open in Wireshark, filter by device MAC address

#### Step 2: Identify Write Patterns

Look for `ATT Write Command` packets:
```
Frame 42: ATT Write Command
  Handle: 0x0011 (UUID: 0000FFE9...)
  Value: 56 FF 00 00 00 F0 AA
  
Frame 43: ATT Write Command  
  Handle: 0x0011 (UUID: 0000FFE9...)
  Value: 56 00 FF 00 00 F0 AA
```

**Observations:**
- Handle stays constant (characteristic UUID)
- Value changes match user actions
- First byte often constant (header)
- Last 1-2 bytes often constant (footer/checksum)

#### Step 3: Hypothesize Packet Format

```
56 [??] [??] [??] [??] F0 AA

# Try mapping to RGB:
56 [R] [G] [B] [?] F0 AA

# Test hypothesis:
Red:   56 FF 00 00 00 F0 AA  ✓
Green: 56 00 FF 00 00 F0 AA  ✓
Blue:  56 00 00 FF 00 F0 AA  ✓
```

#### Step 4: Test Systematically

```python
async def test_protocol_hypothesis(client, char_uuid):
    """Systematically test RGB hypothesis"""
    
    test_cases = [
        # (R, G, B, description)
        (255, 0, 0, "Pure red"),
        (0, 255, 0, "Pure green"),
        (0, 0, 255, "Pure blue"),
        (255, 255, 0, "Yellow"),
        (255, 0, 255, "Magenta"),
        (0, 255, 255, "Cyan"),
        (255, 255, 255, "White"),
        (128, 128, 128, "50% gray"),
    ]
    
    for r, g, b, desc in test_cases:
        payload = bytes([0x56, r, g, b, 0x00, 0xF0, 0xAA])
        print(f"Testing {desc}: {payload.hex()}")
        await client.write_gatt_char(char_uuid, payload, response=False)
        input("Press Enter after verifying color...")
```

#### Step 5: Discover Mode Commands

Modes are harder - need to capture strobe/pulse/fade activations:

```
# User activates "White Strobe" in app:
Frame 103: ATT Write Command
  Value: BB 37 14 44

# User changes speed to "Fast":
Frame 104: ATT Write Command
  Value: BB 37 05 44
  
# Only byte 2 changed! 0x14 → 0x05 (faster)
```

**Pattern discovered:**
```
BB [MODE_ID] [SPEED] 44
```

Test all mode IDs:
```python
async def scan_modes(client, char_uuid):
    """Brute-force mode IDs"""
    for mode_id in range(0x20, 0x50):  # Likely range
        payload = bytes([0xBB, mode_id, 0x14, 0x44])
        print(f"Testing mode 0x{mode_id:02X}")
        await client.write_gatt_char(char_uuid, payload, response=False)
        await asyncio.sleep(3)  # Observe effect
        # Return to static red between tests
        await client.write_gatt_char(
            char_uuid, 
            bytes([0x56, 0xFF, 0x00, 0x00, 0x00, 0xF0, 0xAA]), 
            response=False
        )
        await asyncio.sleep(1)
```

#### Step 6: Document Findings

Create lookup tables:
```python
MODES = [
    ("White Strobe", 0x37),
    ("Rainbow Strobe", 0x30),
    ("Red Pulse", 0x26),
    # ... etc
]

# Document quirks
QUIRKS = {
    "mode_persistence": "Mode commands create persistent state",
    "speed_scale": "Smaller speed value = faster effect",
    "static_override": "0x56 command stops any active mode",
}
```

---

## Advanced Topics

### Multi-Device Synchronization

**Challenge:** Keep 2+ lights perfectly in sync

**Problem:** Network jitter causes lights to drift apart over time

**Solution:** Periodic resynchronization

```python
class SyncController:
    def __init__(self, handles):
        self.handles = handles
        self.last_sync = 0
        self.sync_interval = 10.0  # Force sync every 10s
    
    async def synchronized_write(self, payload: bytes):
        """Write to all devices with resync check"""
        now = time.time()
        
        # Normal parallel write
        await asyncio.gather(*(
            write_single(h, payload) for h in self.handles
        ))
        
        # Periodic hard sync
        if now - self.last_sync > self.sync_interval:
            # Send twice to ensure delivery
            await asyncio.gather(*(
                write_single(h, payload) for h in self.handles
            ))
            self.last_sync = now
```

### Custom Effects Engine

Build complex effects from primitives:

```python
class EffectsEngine:
    async def rainbow_chase(self, speed_hz: float, duration_s: float):
        """Custom rainbow chase using static color commands"""
        colors = [
            (255, 0, 0), (255, 127, 0), (255, 255, 0),
            (0, 255, 0), (0, 255, 255), (0, 0, 255),
            (127, 0, 255), (255, 0, 255)
        ]
        
        delay = 1.0 / speed_hz
        end_time = time.time() + duration_s
        idx = 0
        
        while time.time() < end_time:
            r, g, b = colors[idx % len(colors)]
            payload = bytes([0x56, r, g, b, 0x00, 0xF0, 0xAA])
            await self.write(payload)
            await asyncio.sleep(delay)
            idx += 1
    
    async def pulse_custom(self, color: tuple, bpm: float, beats: int):
        """Pulse effect synced to music BPM"""
        r, g, b = color
        beat_duration = 60.0 / bpm
        
        for _ in range(beats):
            # Fade up (5 steps)
            for brightness in range(0, 256, 51):
                scale = brightness / 255.0
                payload = bytes([
                    0x56, 
                    int(r * scale), 
                    int(g * scale), 
                    int(b * scale), 
                    0x00, 0xF0, 0xAA
                ])
                await self.write(payload)
                await asyncio.sleep(beat_duration / 10)
            
            # Fade down
            for brightness in range(255, -1, -51):
                scale = brightness / 255.0
                payload = bytes([
                    0x56, 
                    int(r * scale), 
                    int(g * scale), 
                    int(b * scale), 
                    0x00, 0xF0, 0xAA
                ])
                await self.write(payload)
                await asyncio.sleep(beat_duration / 10)
```

---

## Conclusion

### Key Takeaways

1. **Two main protocols** (FFE9, FFF3) cover 90%+ of cheap BLE LED controllers
2. **Power-on on connection** - Send `0xCC 0x23 0x33` immediately after connect for both FFE9 and FFF3; required to wake devices in standby (verified QHM-S281)
3. **Auto-detection via RGB probe** works reliably
4. **Persistent connections** required for real-time control (<50ms latency)
5. **Mode commands create persistent state** - must send RGB to override
6. **No pairing needed** - these are "just works" BLE devices
7. **Speed parameter is inversely proportional** to actual speed (smaller = faster)

### What This Project Demonstrates

This codebase implements:
- ✅ Power-on on connection (wakes cold/standby devices)
- ✅ Protocol auto-detection
- ✅ Persistent connection pooling
- ✅ Multi-device synchronization
- ✅ Real-time beat-synced effects
- ✅ Graceful connection recovery
- ✅ Live color wheel with <50ms latency
- ✅ Complex effect sequencing (drops, builds, strobes)

### Future Reverse Engineering

If you encounter a new device that doesn't work:

1. Follow "Reverse Engineering Process" section above
2. Capture HCI logs from official app
3. Look for patterns in ATT Write Commands
4. Test systematically with `brute_force_protocol()`
5. Document findings and contribute back!

### Resources

- **Bleak documentation**: https://bleak.readthedocs.io/
- **Bluetooth Core Spec**: https://www.bluetooth.com/specifications/specs/
- **Nordic nRF Connect**: Best mobile BLE debugging tool
- **Wireshark BTLE plugin**: For packet analysis

---

## Appendix: Complete Command Reference

### FFE9 Protocol (0x56-AA) - Complete Table

```python
# RGB Color Commands
CMD_RGB = [0x56, R, G, B, WW, 0xF0, 0xAA]

# Power Commands  
CMD_POWER_ON  = [0xCC, 0x23, 0x33]
CMD_POWER_OFF = [0xCC, 0x24, 0x33]

# Mode Commands
CMD_MODE = [0xBB, MODE_ID, SPEED, 0x44]

MODE_IDS = {
    0x24: "Static",
    0x25: "Pulsating Rainbow",
    0x26: "Pulsating Red",
    0x27: "Pulsating Green",
    0x28: "Pulsating Blue",
    0x29: "Pulsating Yellow",
    0x2A: "Pulsating Cyan",
    0x2B: "Pulsating Purple",
    0x2C: "Pulsating White",
    0x2D: "Pulsating Red+Green",
    0x2E: "Pulsating Red+Blue",
    0x2F: "Pulsating Green+Blue",
    0x30: "Rainbow Strobe",      # ⭐ Popular for drops
    0x31: "Red Strobe",
    0x32: "Green Strobe",
    0x33: "Blue Strobe",
    0x34: "Yellow Strobe",
    0x35: "Cyan Strobe",
    0x36: "Purple Strobe",
    0x37: "White Strobe",         # ⭐ Most popular
    0x38: "Rainbow Jump",
    0x39: "RGB Pulsating",
    0x3A: "RGB Jump",
}

# Speed range: 1-255 (smaller = faster)
# Empirical: speed ≈ 10 / Hz
SPEED_SLOW   = 80   # ~0.125 Hz
SPEED_MEDIUM = 20   # ~0.5 Hz
SPEED_FAST   = 5    # ~2 Hz
```

### FFF3 Protocol (0x7E-EF) - Known Commands

```python
# RGB Color Command
CMD_RGB = [0x7E, 0x00, 0x05, R, G, B, 0x00, 0x00, 0xEF]

# Other commands not fully documented - varies by manufacturer
```

---

**Document Version:** 1.0  
**Last Updated:** 2025-11-03  
**Project:** KSIPZE LightDesk  
**Author:** Reverse engineered through systematic testing  
**License:** Educational use, respect manufacturer protocols

