# scan_ble.py
import asyncio
from bleak import BleakScanner

async def main():
    devices = await BleakScanner.discover(timeout=6.0)
    for d in devices:
        print(d.name, d.address)

asyncio.run(main())
