# Vendor mode ids for HappyLighting-style controllers (send as: BB <mode> <speed> 44)
# Note: "White Strobe" is mode 55 (0x37). Smaller speed = faster.
# Empirical speed scale: speed ≈ 10 / Hz  →  Hz ≈ 10 / speed
#   speed=5  → ~2 Hz  (≈20 flashes / 10s)
#   speed=10 → ~1 Hz  (≈10 flashes / 10s)

MODES = [
    ("Normal (static)", 36),           # 0x24 (some firmwares use 0x25; static color packets also stop animations)
    ("Pulsating Rainbow", 37),         # 0x25
    ("Pulsating Red", 38),             # 0x26
    ("Pulsating Green", 39),           # 0x27
    ("Pulsating Blue", 40),            # 0x28
    ("Pulsating Yellow", 41),          # 0x29
    ("Pulsating Cyan", 42),            # 0x2A
    ("Pulsating Purple", 43),          # 0x2B
    ("Pulsating White", 44),           # 0x2C
    ("Pulsating Red+Green", 45),       # 0x2D
    ("Pulsating Red+Blue", 46),        # 0x2E
    ("Pulsating Green+Blue", 47),      # 0x2F
    ("Rainbow Strobe", 48),            # 0x30
    ("Red Strobe", 49),                # 0x31
    ("Green Strobe", 50),              # 0x32
    ("Blue Strobe", 51),               # 0x33
    ("Yellow Strobe", 52),             # 0x34
    ("Cyan Strobe", 53),               # 0x35
    ("Purple Strobe", 54),             # 0x36
    ("White Strobe", 55),              # 0x37  ← verified by your test
    ("Rainbow Jump", 56),              # 0x38
    ("RGB Pulsating", 57),             # 0x39
    ("RGB Jump", 58),                  # 0x3A
]
