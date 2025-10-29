#!/usr/bin/env python3
import sys, os, json, asyncio
from dataclasses import dataclass
from typing import Optional, List

from PyQt6 import QtWidgets, QtGui, QtCore
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel, QComboBox, QSlider, QColorDialog,
    QGridLayout, QHBoxLayout, QVBoxLayout, QGroupBox, QRadioButton, QButtonGroup,
    QMessageBox, QCheckBox
)

import ble_control as ble
from modes import MODES
from audiosync import AudioBeatDetector, list_input_devices, BeatEvent
from autoloops import AutoLoopsEngine, PALETTES

CONFIG_PATH = os.path.expanduser("~/.ksipze_lightdesk.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_config(cfg:dict):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

@dataclass
class ConnectedLight:
    info: ble.LightInfo
    handle: ble.LightHandle

class BLEWorker(QtCore.QObject):
    scanned = QtCore.pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()

    @QtCore.pyqtSlot()
    def scan(self):
        async def _scan():
            devs = await ble.scan_devices(timeout=6.0)
            self.scanned.emit([(d.address, d.name) for d in devs])
        self.loop.run_until_complete(_scan())

    def _multi_payload(self, handles, payload: bytes):
        async def _mw():
            # Prefer low-latency persistent writes if available
            try:
                await ble.multi_write_fast(handles, payload)   # new fast path
            except AttributeError:
                await ble.multi_write(handles, payload)
        self.loop.run_until_complete(_mw())

    @QtCore.pyqtSlot(list, int, int, int, int)
    def set_rgb_multi(self, handles_serialized, r, g, b, ww):
        handles = [ble.LightHandle(**h) for h in handles_serialized]
        payload = ble.frame_rgb(r, g, b, ww)
        self._multi_payload(handles, payload)

    @QtCore.pyqtSlot(list, int, int)
    def mode_multi(self, handles_serialized, mode, speed):
        handles = [ble.LightHandle(**h) for h in handles_serialized]
        payload = ble.frame_mode(mode, speed)
        self._multi_payload(handles, payload)

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KSIPZE LightDesk — Live Autoloops")
        self.resize(1040, 720)
        self.cfg = load_config()

        # BLE worker thread
        self.ble_thread = QtCore.QThread()
        self.ble_worker = BLEWorker()
        self.ble_worker.moveToThread(self.ble_thread)
        self.ble_thread.start()

        self.discovered = []
        self.lightA: Optional[ble.LightHandle] = None
        self.lightB: Optional[ble.LightHandle] = None

        # audio
        self.beat_detector: Optional[AudioBeatDetector] = None
        self.last_color = (255, 255, 255)
        self.alt_color = (12, 36, 150)

        # autoloops
        self.autoloops_enabled = False
        self.engine = AutoLoopsEngine(
            set_rgb=self._set_rgb_targets,
            set_mode=self._set_mode_targets,
            flash_white=self._flash_white_ms,
            base_color=self.last_color
        )

        self._build_ui()
        self._wire()
        self._restore_devices()

        QtCore.QTimer.singleShot(200, self.scan_devices)

    # ---------- UI ----------
    def _build_ui(self):
        root = QHBoxLayout(self)

        # Left: discovery + manual
        left = QVBoxLayout()
        root.addLayout(left, 1)

        gb_disc = QGroupBox("Discover & Connect")
        vdisc = QVBoxLayout(gb_disc)
        self.btn_scan = QPushButton("Scan")
        self.list_devices = QComboBox()
        self.list_devices.setMinimumWidth(340)
        hsel = QHBoxLayout()
        self.btn_assign_A = QPushButton("Assign → A")
        self.btn_assign_B = QPushButton("Assign → B")
        hsel.addWidget(self.btn_assign_A); hsel.addWidget(self.btn_assign_B)
        self.lbl_A = QLabel("A: <not set>"); self.lbl_B = QLabel("B: <not set>")
        self.target_group = QButtonGroup(self)
        self.rb_A = QRadioButton("Control A")
        self.rb_B = QRadioButton("Control B")
        self.rb_both = QRadioButton("Control Both"); self.rb_both.setChecked(True)
        for rb in (self.rb_A, self.rb_B, self.rb_both): self.target_group.addButton(rb)
        for w in (self.btn_scan, self.list_devices, hsel, self.lbl_A, self.lbl_B, self.rb_A, self.rb_B, self.rb_both):
            (vdisc.addLayout(w) if isinstance(w, QHBoxLayout) else vdisc.addWidget(w))
        left.addWidget(gb_disc)

        gb_color = QGroupBox("Color & Brightness")
        vcol = QVBoxLayout(gb_color)
        self.swatch_layout = QGridLayout()
        presets = [
            ("Red",(255,0,0)), ("Green",(0,255,0)), ("Blue",(0,0,255)),
            ("White",(255,255,255)), ("Warm",(255,180,120)),
            ("ND Blue",(12,36,150)), ("Gold",(255,200,0)),
            ("Purple",(170,0,255)), ("Cyan",(0,255,255)), ("Amber",(255,120,0))
        ]
        r=c=0
        for name, rgb in presets:
            btn = QPushButton(name)
            btn.setStyleSheet(f"background-color: rgb({rgb[0]},{rgb[1]},{rgb[2]}); color: #000;")
            btn.clicked.connect(lambda checked, rgb=rgb: self.set_color(rgb))
            self.swatch_layout.addWidget(btn, r, c)
            c += 1
            if c >= 3: r += 1; c = 0
        vcol.addLayout(self.swatch_layout)

        self.btn_pick = QPushButton("Pick Color…"); vcol.addWidget(self.btn_pick)
        hb = QHBoxLayout(); hb.addWidget(QLabel("Brightness"))
        self.s_brightness = QSlider(Qt.Orientation.Horizontal)
        self.s_brightness.setRange(1,100)
        self.s_brightness.setValue(self.cfg.get("brightness", 100))
        hb.addWidget(self.s_brightness); vcol.addLayout(hb)
        hb2 = QHBoxLayout()
        self.btn_blackout = QPushButton("Blackout")
        self.btn_whiteflash = QPushButton("White (Hold)")
        hb2.addWidget(self.btn_blackout); hb2.addWidget(self.btn_whiteflash)
        vcol.addLayout(hb2)
        left.addWidget(gb_color)

        # Right: modes + audio + autoloops
        right = QVBoxLayout(); root.addLayout(right,1)

        gb_modes = QGroupBox("Vendor Modes")
        vm = QVBoxLayout(gb_modes)
        self.combo_mode = QComboBox()
        for name, mid in MODES: self.combo_mode.addItem(f"{name} ({mid})", mid)
        self.combo_mode.setCurrentIndex(0)
        hs = QHBoxLayout()
        hs.addWidget(QLabel("Speed"))
        self.s_speed = QSlider(Qt.Orientation.Horizontal)
        self.s_speed.setRange(1,255)
        self.s_speed.setValue(128)
        hs.addWidget(self.s_speed)
        self.btn_set_mode = QPushButton("Activate Mode")
        for w in (self.combo_mode, hs, self.btn_set_mode):
            (vm.addLayout(w) if isinstance(w, QHBoxLayout) else vm.addWidget(w))
        right.addWidget(gb_modes)

        gb_audio = QGroupBox("Audio Sync")
        va = QVBoxLayout(gb_audio)
        self.combo_input = QComboBox()
        for idx,name in list_input_devices(): self.combo_input.addItem(f"{name}", idx)
        if self.cfg.get("audio_device_index") is not None:
            ix = self.combo_input.findData(self.cfg["audio_device_index"])
            if ix >= 0: self.combo_input.setCurrentIndex(ix)
        hb3 = QHBoxLayout(); hb3.addWidget(QLabel("Input Device")); hb3.addWidget(self.combo_input); va.addLayout(hb3)
        self.lbl_bpm = QLabel("BPM: --"); va.addWidget(self.lbl_bpm)
        self.cb_flash_on_beat = QCheckBox("Flash on Beat")      # <-- NEW
        self.cb_flash_on_beat.setChecked(True)
        va.addWidget(self.cb_flash_on_beat)
        self.btn_audio_start = QPushButton("Start Audio Sync")
        self.btn_audio_stop = QPushButton("Stop Audio Sync")
        self.btn_audio_debug = QPushButton("Audio Debug (5s)")
        va.addWidget(self.btn_audio_start); va.addWidget(self.btn_audio_stop)
        va.addWidget(self.btn_audio_debug)
        right.addWidget(gb_audio)

        gb_auto = QGroupBox("Live Autoloops (SoundSwitch-style)")
        vao = QVBoxLayout(gb_auto)
        self.combo_style = QComboBox(); self.combo_style.addItems(["House","EDM","Hip-Hop","Chill"]); vao.addWidget(self.combo_style)
        self.combo_palette = QComboBox()
        for name in PALETTES.keys(): self.combo_palette.addItem(name)
        vao.addWidget(self.combo_palette)
        self.cb_8bar_blinder = QCheckBox("Blinder on phrase (8 bars)")
        self.cb_8bar_blinder.setChecked(True)
        vao.addWidget(self.cb_8bar_blinder)
        self.btn_auto_start = QPushButton("Start Live Autoloops")
        self.btn_auto_stop = QPushButton("Stop Live Autoloops")
        vao.addWidget(self.btn_auto_start); vao.addWidget(self.btn_auto_stop)
        # ADDED energy label
        self.lbl_energy = QLabel("Energy: --")
        vao.addWidget(self.lbl_energy)
        right.addWidget(gb_auto)

        right.addStretch(1)

    def _wire(self):
        self.btn_scan.clicked.connect(self.scan_devices)
        self.ble_worker.scanned.connect(self.on_scanned)
        self.btn_assign_A.clicked.connect(lambda: self.assign_light(True))
        self.btn_assign_B.clicked.connect(lambda: self.assign_light(False))

        self.btn_pick.clicked.connect(self.open_color_picker)
        self.btn_blackout.clicked.connect(lambda: self.set_color((0,0,0)))
        self.btn_whiteflash.pressed.connect(lambda: self.set_color((255,255,255)))
        self.btn_whiteflash.released.connect(lambda: self.set_color(self.last_color))
        self.s_brightness.valueChanged.connect(self.on_brightness_change)

        self.btn_set_mode.clicked.connect(self.activate_mode)

        self.btn_audio_start.clicked.connect(self.start_audio)
        self.btn_audio_stop.clicked.connect(self.stop_audio)
        self.btn_audio_debug.clicked.connect(self.audio_debug)

        self.btn_auto_start.clicked.connect(self.start_autoloops)
        self.btn_auto_stop.clicked.connect(self.stop_autoloops)
        self.combo_style.currentTextChanged.connect(lambda s: self.engine.set_style(s))
        self.combo_palette.currentTextChanged.connect(lambda p: self.engine.set_palette(p))

    def open_color_picker(self):
        initial = QtGui.QColor(*self.last_color)
        color = QColorDialog.getColor(initial, self, "Pick Color")
        if color.isValid():
            self.set_color((color.red(), color.green(), color.blue()))

    # ---------- BLE helpers ----------
    def current_targets(self) -> List[ble.LightHandle]:
        out = []
        if self.rb_A.isChecked() and self.lightA: out.append(self.lightA)
        elif self.rb_B.isChecked() and self.lightB: out.append(self.lightB)
        elif self.rb_both.isChecked():
            if self.lightA: out.append(self.lightA)
            if self.lightB: out.append(self.lightB)
        return out

    def _set_rgb_targets(self, r, g, b):
        targets = self.current_targets()
        if not targets: return
        bscale = self.s_brightness.value() / 100.0
        r = int(r * bscale); g = int(g * bscale); b = int(b * bscale)
        serialized = [t.__dict__ for t in targets]
        QtCore.QMetaObject.invokeMethod(
            self.ble_worker, "set_rgb_multi",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(list, serialized),
            QtCore.Q_ARG(int, r), QtCore.Q_ARG(int, g), QtCore.Q_ARG(int, b), QtCore.Q_ARG(int, 0)
        )

    def _set_mode_targets(self, mid, spd):
        targets = self.current_targets()
        if not targets: return
        serialized = [t.__dict__ for t in targets]
        QtCore.QMetaObject.invokeMethod(
            self.ble_worker, "mode_multi",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(list, serialized),
            QtCore.Q_ARG(int, int(mid)), QtCore.Q_ARG(int, int(spd))
        )

    def _flash_white_ms(self, ms:int):
        self._set_rgb_targets(255,255,255)
        QtCore.QTimer.singleShot(ms, lambda: self._set_rgb_targets(*self.last_color))

    # ---------- Discovery ----------
    def scan_devices(self):
        self.list_devices.clear()
        self.list_devices.addItem("(Scanning…)", None)
        QtCore.QMetaObject.invokeMethod(self.ble_worker, "scan")

    def on_scanned(self, pairs):
        self.discovered = pairs
        self.list_devices.clear()
        if not pairs:
            self.list_devices.addItem("(No devices found)", None); return
        for addr,name in pairs:
            self.list_devices.addItem(f"{name} — {addr}", addr)

    def assign_light(self, isA: bool):
        idx = self.list_devices.currentIndex()
        addr = self.list_devices.itemData(idx)
        if addr is None:
            return

        async def go():
            info = ble.LightInfo(address=addr, name=addr)
            return await ble.connect_and_identify(info)

        loop = asyncio.new_event_loop()
        try:
            handle = loop.run_until_complete(go())
        except Exception as e:
            QMessageBox.warning(
                self,
                "Connect failed",
                f"Could not connect to the selected device.\n\n{type(e).__name__}: {e}"
            )
            return
        finally:
            try: loop.close()
            except Exception: pass

        if isA:
            self.lightA = handle
            self.lbl_A.setText(f"A: {handle.name} — {handle.address}")
            self.cfg["lightA"] = handle.__dict__
        else:
            self.lightB = handle
            self.lbl_B.setText(f"B: {handle.name} — {handle.address}")
            self.cfg["lightB"] = handle.__dict__
        save_config(self.cfg)

    def _restore_devices(self):
        if self.cfg.get("lightA"):
            try:
                d = self.cfg["lightA"]; self.lightA = ble.LightHandle(**d)
                self.lbl_A.setText(f"A: {self.lightA.name} — {self.lightA.address}")
            except Exception: pass
        if self.cfg.get("lightB"):
            try:
                d = self.cfg["lightB"]; self.lightB = ble.LightHandle(**d)
                self.lbl_B.setText(f"B: {self.lightB.name} — {self.lightB.address}")
            except Exception: pass

    # ---------- Manual control ----------
    def set_color(self, rgb):
        if rgb != (255,255,255):
            self.last_color = rgb
            self.engine.base_color = rgb
        self._set_rgb_targets(*rgb)
        self.cfg["brightness"] = self.s_brightness.value(); save_config(self.cfg)

    def on_brightness_change(self, _):
        self._set_rgb_targets(*self.last_color)

    def activate_mode(self):
        mid = self.combo_mode.currentData()
        spd = self.s_speed.value()
        self._set_mode_targets(mid, spd)

    # ---------- Audio & Autoloops ----------
    def start_audio(self):
        # stop if already running
        if self.beat_detector:
            self.stop_audio()
        idx = self.combo_input.currentData()
        self.cfg["audio_device_index"] = idx; save_config(self.cfg)

        def on_beat(ev: BeatEvent):
            QtCore.QMetaObject.invokeMethod(
                self, "handle_beat",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(object, ev)
            )

        self.beat_detector = AudioBeatDetector(device_index=idx, on_beat=on_beat)
        self.beat_detector.reset()   # ensure a clean lock each start
        self.beat_detector.start()

    def stop_audio(self):
        if self.beat_detector:
            try:
                self.beat_detector.stop()
            except Exception:
                pass
            self.beat_detector = None
            self.lbl_bpm.setText("BPM: --")

    @QtCore.pyqtSlot(object)
    def handle_beat(self, ev: BeatEvent):
        if ev.bpm > 0:
            self.lbl_bpm.setText(f"BPM: {ev.bpm:.1f}")

        # EXACT flash on every beat, adaptive duration/energy
        if self.cb_flash_on_beat.isChecked():
            if self.autoloops_enabled and hasattr(self.engine, '_energy_tier'):
                tier = self.engine._energy_tier()
                from autoloops import EnergyTier  # To ensure enum is available
                if tier == EnergyTier.HIGH:
                    self._flash_white_ms(150)  # Longer, punchier on drops
                elif tier == EnergyTier.MED:
                    self._flash_white_ms(90)
                else:
                    self._flash_white_ms(50)
            else:
                self._flash_white_ms(90)

        if self.autoloops_enabled:
            # Show energy label (require _energy_tier on engine)
            if hasattr(self.engine, '_energy_tier'):
                tier = self.engine._energy_tier()
                self.lbl_energy.setText(f"Energy: {tier.name} (RMS: {ev.rms:.3f})")
            self.engine.on_beat(ev.bpm, ev.rms, ev.high, ev.bass)

    def start_autoloops(self):
        self.engine.set_style(self.combo_style.currentText())
        self.engine.set_palette(self.combo_palette.currentText())
        self.autoloops_enabled = True

    def stop_autoloops(self):
        self.autoloops_enabled = False

    def audio_debug(self):
        """Print 5 seconds of raw audio stats for diagnostics"""
        if not self.beat_detector:
            QMessageBox.warning(self, "Error", "Start Audio Sync first!")
            return
        
        print("\n" + "="*60)
        print("AUDIO DEBUG - Watching for 5 seconds...")
        print("="*60)
        print("Expected values:")
        print("  RMS: Silence=0.01-0.03, Music=0.05-0.15, Drops=0.20-0.40+")
        print("  Bass/High: Should be similar scale to RMS (0.0-1.0)")
        print("  BPM: Should lock within 5-10 beats")
        print("-"*60)
        
        def debug_callback(ev):
            beat_num = self.engine.state.beat if self.autoloops_enabled else "N/A"
            print(f"Beat #{beat_num}: "
                  f"RMS={ev.rms:.4f} Bass={ev.bass:.4f} High={ev.high:.4f} "
                  f"BPM={ev.bpm:.1f}")
        
        # Temporarily wrap callback
        old_callback = self.beat_detector.on_beat
        def wrapped_callback(ev):
            old_callback(ev)
            debug_callback(ev)
        
        self.beat_detector.on_beat = wrapped_callback
        
        # Restore after 5 seconds
        QtCore.QTimer.singleShot(5000, lambda: (
            setattr(self.beat_detector, 'on_beat', old_callback),
            print("="*60),
            print("DEBUG COMPLETE - Check values above"),
            print("="*60 + "\n")
        ))

def main():
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
