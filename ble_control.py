#!/usr/bin/env python3
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Dict
from bleak import BleakScanner, BleakClient, BleakError

# Names we consider "likely LED controllers"
KNOWN_NAMES = {
    "ELK-BLEDOM", "Triones", "HappyLighting", "LEDnetWF", "LEDBlue",
    "QHM-SDA0", "QHM-S281", "QHM", "Light", "Dream", "Flash"
}

@dataclass
class LightInfo:
    address: str
    name: str

@dataclass
class LightHandle:
    address: str
    name: str
    char_uuid: str
    family: str  # "FFE9_56AA" (HappyLighting-style) or "FFF3_7EEF"

# ---------------------------
# Packet builders (FFE9 / 0x56-AA family)
# ---------------------------
def frame_rgb(r: int, g: int, b: int, ww: int = 0x00) -> bytes:
    """Static RGB (W ignored): 56 R G B W F0 AA"""
    return bytes([0x56, r & 0xFF, g & 0xFF, b & 0xFF, ww & 0xFF, 0xF0, 0xAA])

def frame_power(on: bool) -> bytes:
    # CC 23 33 = on, CC 24 33 = off
    return bytes([0xCC, 0x23 if on else 0x24, 0x33])

def frame_mode(mode: int, speed: int) -> bytes:
    # BB <mode> <speed> 44   (smaller = faster)
    return bytes([0xBB, mode & 0xFF, speed & 0xFF, 0x44])

# ---------------------------
# Scanning / Connect & identify
# ---------------------------
async def scan_devices(timeout: float = 6.0) -> List[LightInfo]:
    devs = await BleakScanner.discover(timeout=timeout)
    out: List[LightInfo] = []
    for d in devs:
        nm = d.name or "unknown"
        if (
            nm in KNOWN_NAMES
            or nm.startswith("QHM-")
            or "LED" in nm
            or "Light" in nm
            or "BLE" in nm
            or "Trion" in nm
        ):
            out.append(LightInfo(d.address, nm))
    return out

async def _find_write_char(client: BleakClient) -> str:
    svcs = await client.get_services()
    for s in svcs:
        for ch in s.characteristics:
            props = set(ch.properties)
            if ("write" in props) or ("write_without_response" in props):
                return ch.uuid
    raise RuntimeError("No writable characteristic found")

def _frame_f56(r,g,b):  # FFE9 (HappyLighting)
    return bytes([0x56, r & 0xFF, g & 0xFF, b & 0xFF, 0x00, 0xF0, 0xAA])

def _frame_f7e(r,g,b):  # FFF3 (7E ... EF) family
    # Minimal static color (RGB only)
    return bytes([0x7E, 0x00, 0x05, r & 0xFF, g & 0xFF, b & 0xFF, 0x00, 0x00, 0xEF])

async def _probe_family(client: BleakClient, char_uuid: str) -> Optional[str]:
    try:
        for rgb in [(255,0,0),(0,255,0),(0,0,255)]:
            await client.write_gatt_char(char_uuid, _frame_f7e(*rgb), response=False)
            await asyncio.sleep(0.15)
        return "FFF3_7EEF"
    except Exception:
        pass
    try:
        for rgb in [(255,0,0),(0,255,0),(0,0,255)]:
            await client.write_gatt_char(char_uuid, _frame_f56(*rgb), response=False)
            await asyncio.sleep(0.15)
        return "FFE9_56AA"
    except Exception:
        return None

async def connect_and_identify(light: LightInfo, timeout: float = 8.0) -> LightHandle:
    async with BleakClient(light.address, timeout=timeout) as c:
        char_uuid = await _find_write_char(c)
        fam = await _probe_family(c, char_uuid)
        if fam is None:
            fam = "FFE9_56AA"  # default to HappyLighting-style
        return LightHandle(light.address, light.name, char_uuid, fam)

# ---------------------------
# Simple (non-persistent) multi-write
# ---------------------------
async def multi_write(handles: List[LightHandle], payload: bytes):
    async def write_one(h: LightHandle):
        async with BleakClient(h.address) as c:
            await c.write_gatt_char(h.char_uuid, payload, response=False)
    await asyncio.gather(*(write_one(h) for h in handles))

# ---------------------------
# Low-latency persistent pool
# ---------------------------
class _ClientPool:
    def __init__(self):
        self._clients: Dict[str, BleakClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def _get_client(self, h: LightHandle) -> BleakClient:
        cli = self._clients.get(h.address)
        if cli is None:
            cli = BleakClient(h.address)
            self._clients[h.address] = cli
            self._locks[h.address] = asyncio.Lock()
        if not cli.is_connected:
            await cli.connect()
        return cli

    async def write(self, h: LightHandle, payload: bytes):
        cli = await self._get_client(h)
        lock = self._locks[h.address]
        async with lock:
            try:
                await cli.write_gatt_char(h.char_uuid, payload, response=False)
            except (BleakError, Exception):
                # try one reconnect
                try:
                    if cli.is_connected:
                        await cli.disconnect()
                except Exception:
                    pass
                await cli.connect()
                await cli.write_gatt_char(h.char_uuid, payload, response=False)

    async def close_all(self):
        tasks = []
        for cli in list(self._clients.values()):
            if cli.is_connected:
                tasks.append(cli.disconnect())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()
        self._locks.clear()

_pool = _ClientPool()

async def multi_write_fast(handles: List[LightHandle], payload: bytes):
    """Reuse connections for much lower latency; good for live color wheel."""
    await asyncio.gather(*(_pool.write(h, payload) for h in handles))
