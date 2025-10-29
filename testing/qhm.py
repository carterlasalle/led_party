#!/usr/bin/env python3
# qhm.py — control QHM/HappyLighting RGBW BLE controllers
#
# Key points:
# - Write char: 0000ffd9-0000-1000-8000-00805f9b34fb
# - Notify char (optional): 0000ffd4-0000-1000-8000-00805f9b34fb
# - Power: CC 23 33 (on), CC 24 33 (off)
# - Static color/white (7 bytes): 56 R G B W MODE AA
#       MODE = 0xF0 → use RGB (W ignored)
#       MODE = 0x0F → use W   (RGB ignored)
# - Built-in modes: BB <mode> <speed> 44
#       Static "stop animations" code: 0x25
#       White strobe: 0x37 (smaller speed = faster)

import asyncio, argparse, sys
from bleak import BleakScanner, BleakClient, BleakError

# Device names commonly used by these controllers
NAMES = ("QHM-SDA0", "QHM-S281", "Triones", "Dream", "Light", "Flash")

# GATT UUIDs
CHAR_TX = "0000ffd9-0000-1000-8000-00805f9b34fb"  # write (TX to device)
CHAR_RX = "0000ffd4-0000-1000-8000-00805f9b34fb"  # notify (optional RX from device)

# Common packets
PKT_ON     = bytes.fromhex("CC2333")
PKT_OFF    = bytes.fromhex("CC2433")
PKT_STATIC = bytes([0xBB, 0x25, 0x80, 0x44])  # enter static color mode (speed doesn't matter)

def pkt_rgb(r: int, g: int, b: int) -> bytes:
    # 56 R G B W 0xF0 AA  → RGB only, W ignored
    return bytes([0x56, r & 0xFF, g & 0xFF, b & 0xFF, 0x00, 0xF0, 0xAA])

def pkt_white(w: int) -> bytes:
    # 56 00 00 00 W 0x0F AA → W only, RGB ignored (matches HappyLighting behavior)
    return bytes([0x56, 0x00, 0x00, 0x00, w & 0xFF, 0x0F, 0xAA])

def apply_order(r, g, b, order="RGB"):
    order = order.upper()
    if order == "RGB": return (r, g, b)
    if order == "GRB": return (g, r, b)
    if order == "BRG": return (b, r, g)
    if order == "BGR": return (b, g, r)
    if order == "RBG": return (r, b, g)
    if order == "GBR": return (g, b, r)
    raise ValueError("order must be one of RGB, GRB, BRG, BGR, RBG, GBR")

def parse_hex(h: str, order="RGB"):
    h = h.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError("HEX must be 6 chars like FF7F00")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = dict(R=r, G=g, B=b)
    return (m[order[0]], m[order[1]], m[order[2]])

# Optional: apply sRGB → linear gamma to make mid-levels look less neon
def srgb_to_linear_byte(x: int) -> int:
    t = x / 255.0
    if t <= 0.04045:
        u = t / 12.92
    else:
        u = ((t + 0.055) / 1.055) ** 2.4
    return max(0, min(255, int(round(u * 255))))

async def find_device(target_addr: str | None):
    if target_addr:
        return target_addr, f"(manual) {target_addr}"
    devs = await BleakScanner.discover(timeout=6.0)
    pick = next((d for d in devs if (d.name or "").startswith(NAMES)), None)
    if not pick:
        raise SystemExit("No QHM/HappyLighting device advertising (or it’s busy). "
                         "Power-cycle it and ensure the phone app is closed.")
    return pick.address, f"{pick.name}  {pick.address}"

async def write(c: BleakClient, data: bytes, require_response: bool = True):
    try:
        await c.write_gatt_char(CHAR_TX, data, response=require_response)
    except BleakError:
        if require_response:
            # Some firmwares only accept write-without-response
            await c.write_gatt_char(CHAR_TX, data, response=False)
        else:
            raise

class QHM:
    def __init__(self, addr: str):
        self.addr = addr
        self.client: BleakClient | None = None

    async def __aenter__(self):
        self.client = BleakClient(self.addr, timeout=12.0)
        await self.client.__aenter__()
        # RX is optional; subscribe if present
        try:
            await self.client.start_notify(CHAR_RX, lambda *_: None)
        except Exception:
            pass
        return self

    async def __aexit__(self, *exc):
        try:
            if self.client:
                await self.client.stop_notify(CHAR_RX)
        except Exception:
            pass
        if self.client:
            return await self.client.__aexit__(*exc)

    async def ensure_static(self):
        # Power on + enter static color mode, then tiny pause so next 0x56 lands cleanly
        await write(self.client, PKT_ON, True)
        await write(self.client, PKT_STATIC, True)
        await asyncio.sleep(0.03)

    async def power(self, on: bool):
        await write(self.client, PKT_ON if on else PKT_OFF, True)

    async def set_rgb(self, r: int, g: int, b: int, order="RGB"):
        r, g, b = (max(0, min(255, v)) for v in apply_order(r, g, b, order))
        await self.ensure_static()
        await write(self.client, pkt_rgb(r, g, b), True)

    async def set_white(self, level: int):
        level = max(0, min(255, level))
        await self.ensure_static()
        await write(self.client, pkt_white(level), True)

    async def set_mode(self, mode: int, speed: int = 0x80):
        # BB <mode> <speed> 44  (smaller speed = faster)
        await write(self.client, bytes([0xBB, mode & 0xFF, speed & 0xFF, 0x44]), True)

async def cmd(args):
    addr, label = await find_device(args.addr)
    print("Connecting to", label)
    async with QHM(addr) as qhm:
        if args.cmd == "on":
            await qhm.power(True)

        elif args.cmd == "off":
            await qhm.power(False)

        elif args.cmd == "rgb":
            await qhm.set_rgb(args.r, args.g, args.b, order=args.order)

        elif args.cmd == "hex":
            r, g, b = parse_hex(args.hex, order=args.order)
            if args.gamma:
                r, g, b = map(srgb_to_linear_byte, (r, g, b))
            await qhm.set_rgb(r, g, b, order=args.order)

        elif args.cmd == "white":
            await qhm.set_white(args.white)

        elif args.cmd == "mode":
            await qhm.set_mode(args.mode, args.speed)

def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--addr", help="BLE address/UUID (use this for your QHM device)")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("on")
    sub.add_parser("off")

    prgb = sub.add_parser("rgb", help="Set static RGB (W disabled)")
    prgb.add_argument("r", type=int)
    prgb.add_argument("g", type=int)
    prgb.add_argument("b", type=int)
    prgb.add_argument("--order", default="RGB", help="RGB|GRB|BRG|BGR|RBG|GBR")

    phex = sub.add_parser("hex", help="Set static RGB from 6-hex (e.g., FF7F00)")
    phex.add_argument("hex")
    phex.add_argument("--order", default="RGB")
    phex.add_argument("--gamma", action="store_true",
                      help="Apply sRGB→linear gamma (off by default).")

    pw = sub.add_parser("white", help="Set static white channel (RGB disabled)")
    pw.add_argument("white", type=int, help="0..255")

    pm = sub.add_parser("mode", help="Run built-in animation (BB <mode> <speed> 44)")
    pm.add_argument("mode", type=lambda s: int(s, 0), help="e.g. 0x37 = white strobe")
    pm.add_argument("--speed", type=lambda s: int(s, 0), default=0x80,
                    help="Smaller = faster (try 10–40 for fast strobe)")

    return p

def main():
    try:
        asyncio.run(cmd(build_parser().parse_args()))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)

if __name__ == "__main__":
    main()
