#!/usr/bin/env python3
import sys, os, json, asyncio
from dataclasses import dataclass
from typing import Optional, List

from PyQt6 import QtWidgets, QtGui, QtCore
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel, QComboBox, QSlider, QColorDialog,
    QGridLayout, QHBoxLayout, QVBoxLayout, QGroupBox, QRadioButton, QButtonGroup,
    QMessageBox, QCheckBox, QScrollArea
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
        
        # color flash on beat
        self.flash_color = (255, 255, 255)  # Default to white
        self.flash_color_index = 0  # Index for rotating through color wheel
        # Rainbow color wheel (HSV-based colors for smooth transitions)
        self.flash_color_wheel = [
            (255, 0, 0),      # Red
            (255, 127, 0),    # Orange
            (255, 255, 0),    # Yellow
            (0, 255, 0),      # Green
            (0, 255, 255),    # Cyan
            (0, 0, 255),      # Blue
            (127, 0, 255),    # Purple
            (255, 0, 255),    # Magenta
        ]
        
        # live color wheel
        self.live_wheel_active = False
        self.live_wheel_dialog: Optional[QColorDialog] = None

        # manual control
        self.manual_mode = False
        self.manual_energy_tier = None
        self.sensitivity_multiplier = 1.0

        # alternating effects
        self.alternating_enabled = False
        self.alternating_timer = QtCore.QTimer()
        self.alternating_state = 0  # 0 or 1 for A/B switching

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

        # Right: modes + audio + autoloops (in a scroll area for overflow)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right_scroll.setWidget(right_widget)
        root.addWidget(right_scroll, 1)

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

        # Color Flash on Beat panel
        gb_color_flash = QGroupBox("Color Flash on Beat")
        vcf = QVBoxLayout(gb_color_flash)
        
        # Color cycling options
        hcf_mode = QHBoxLayout()
        self.rb_flash_static = QRadioButton("Static Color")
        self.rb_flash_cycle = QRadioButton("Cycle Colors (Rainbow)")
        self.rb_flash_static.setChecked(True)
        self.flash_mode_group = QButtonGroup(self)
        self.flash_mode_group.addButton(self.rb_flash_static)
        self.flash_mode_group.addButton(self.rb_flash_cycle)
        hcf_mode.addWidget(self.rb_flash_static)
        hcf_mode.addWidget(self.rb_flash_cycle)
        vcf.addLayout(hcf_mode)
        
        self.cb_color_flash_enabled = QCheckBox("Enable Flash on Beat")
        vcf.addWidget(self.cb_color_flash_enabled)
        
        hcf = QHBoxLayout()
        self.btn_flash_color_pick = QPushButton("Pick Static Color")
        self.lbl_flash_color = QLabel("● Flash: White")
        self.lbl_flash_color.setStyleSheet("font-weight: bold; font-size: 14px;")
        hcf.addWidget(self.btn_flash_color_pick)
        hcf.addWidget(self.lbl_flash_color)
        vcf.addLayout(hcf)
        right.addWidget(gb_color_flash)

        # Live Color Wheel panel
        gb_live_wheel = QGroupBox("Live Color Wheel")
        vlw = QVBoxLayout(gb_live_wheel)
        self.cb_live_wheel_enabled = QCheckBox("Enable Live Color Wheel")
        vlw.addWidget(self.cb_live_wheel_enabled)
        self.btn_live_color_pick = QPushButton("Open Color Wheel (updates live!)")
        vlw.addWidget(self.btn_live_color_pick)
        self.lbl_live_color = QLabel("Current: Not active")
        vlw.addWidget(self.lbl_live_color)
        right.addWidget(gb_live_wheel)

        # Manual Control & Sensitivity panel
        gb_manual = QGroupBox("Manual Control & Sensitivity")
        vman = QVBoxLayout(gb_manual)
        
        # Mode toggle
        hmode = QHBoxLayout()
        self.rb_auto_mode = QRadioButton("Automatic")
        self.rb_manual_mode = QRadioButton("Manual Override")
        self.rb_auto_mode.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_auto_mode)
        self.mode_group.addButton(self.rb_manual_mode)
        hmode.addWidget(self.rb_auto_mode)
        hmode.addWidget(self.rb_manual_mode)
        vman.addLayout(hmode)
        
        # Manual energy tier buttons
        self.lbl_manual_energy = QLabel("Manual Energy Tier:")
        vman.addWidget(self.lbl_manual_energy)
        hme = QHBoxLayout()
        self.btn_manual_low = QPushButton("LOW")
        self.btn_manual_med = QPushButton("MED")
        self.btn_manual_high = QPushButton("HIGH")
        self.btn_manual_low.setEnabled(False)
        self.btn_manual_med.setEnabled(False)
        self.btn_manual_high.setEnabled(False)
        hme.addWidget(self.btn_manual_low)
        hme.addWidget(self.btn_manual_med)
        hme.addWidget(self.btn_manual_high)
        vman.addLayout(hme)
        
        # Manual trigger buttons
        self.lbl_manual_triggers = QLabel("Manual Triggers:")
        vman.addWidget(self.lbl_manual_triggers)
        hmt = QHBoxLayout()
        self.btn_trigger_build = QPushButton("Trigger Build")
        self.btn_trigger_drop = QPushButton("Trigger Drop")
        hmt.addWidget(self.btn_trigger_build)
        hmt.addWidget(self.btn_trigger_drop)
        vman.addLayout(hmt)
        
        # Sensitivity slider
        self.lbl_sensitivity = QLabel("Sensitivity: 1.0x (Normal)")
        vman.addWidget(self.lbl_sensitivity)
        self.s_sensitivity = QSlider(Qt.Orientation.Horizontal)
        self.s_sensitivity.setRange(50, 200)  # 0.5x to 2.0x
        self.s_sensitivity.setValue(100)  # 1.0x default
        vman.addWidget(self.s_sensitivity)
        
        right.addWidget(gb_manual)

        # Alternating Light Effects panel
        gb_alt = QGroupBox("Alternating Light Effects (A/B)")
        valt = QVBoxLayout(gb_alt)
        
        self.cb_alternating = QCheckBox("Enable Alternating Mode")
        valt.addWidget(self.cb_alternating)
        
        hap = QHBoxLayout()
        hap.addWidget(QLabel("Pattern:"))
        self.combo_alt_pattern = QComboBox()
        self.combo_alt_pattern.addItems([
            "Alternating Flash",
            "Alternating On/Off",
            "Alternating Colors",
            "Chase (A→B→A→B)",
            "Opposite Colors"
        ])
        hap.addWidget(self.combo_alt_pattern)
        valt.addLayout(hap)
        
        has = QHBoxLayout()
        has.addWidget(QLabel("Speed:"))
        self.s_alt_speed = QSlider(Qt.Orientation.Horizontal)
        self.s_alt_speed.setRange(10, 200)  # 100ms to 2000ms
        self.s_alt_speed.setValue(50)  # 500ms default
        has.addWidget(self.s_alt_speed)
        self.lbl_alt_speed = QLabel("500ms")
        has.addWidget(self.lbl_alt_speed)
        valt.addLayout(has)
        
        right.addWidget(gb_alt)

        right.addStretch(1)  # Push everything to top
        
        # Set minimum width for right column to prevent squishing
        right_widget.setMinimumWidth(400)

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

        self.btn_flash_color_pick.clicked.connect(self.pick_flash_color)
        self.btn_live_color_pick.clicked.connect(self.open_live_color_wheel)
        self.cb_live_wheel_enabled.stateChanged.connect(self.toggle_live_wheel)

        self.rb_auto_mode.toggled.connect(self.toggle_manual_mode)
        self.btn_manual_low.clicked.connect(lambda: self.set_manual_energy("LOW"))
        self.btn_manual_med.clicked.connect(lambda: self.set_manual_energy("MED"))
        self.btn_manual_high.clicked.connect(lambda: self.set_manual_energy("HIGH"))
        self.btn_trigger_build.clicked.connect(self.trigger_build_manually)
        self.btn_trigger_drop.clicked.connect(self.trigger_drop_manually)
        self.s_sensitivity.valueChanged.connect(self.update_sensitivity)

        self.cb_alternating.stateChanged.connect(self.toggle_alternating)
        self.s_alt_speed.valueChanged.connect(self.update_alt_speed)
        self.alternating_timer.timeout.connect(self.run_alternating_pattern)

    def open_color_picker(self):
        initial = QtGui.QColor(*self.last_color)
        color = QColorDialog.getColor(initial, self, "Pick Color")
        if color.isValid():
            self.set_color((color.red(), color.green(), color.blue()))

    def pick_flash_color(self):
        """Pick the color for beat-synced color flashes"""
        initial = QtGui.QColor(*self.flash_color)
        color = QColorDialog.getColor(initial, self, "Pick Flash Color")
        if color.isValid():
            self.flash_color = (color.red(), color.green(), color.blue())
            self.lbl_flash_color.setText(f"● Flash: RGB({color.red()},{color.green()},{color.blue()})")
            self.lbl_flash_color.setStyleSheet(
                f"font-weight: bold; font-size: 14px; color: rgb({color.red()},{color.green()},{color.blue()});"
            )

    def open_live_color_wheel(self):
        """Open a persistent color wheel that updates lights in real-time"""
        # If dialog exists and is visible, bring to front
        if self.live_wheel_dialog is not None:
            if self.live_wheel_dialog.isVisible():
                self.live_wheel_dialog.raise_()
                self.live_wheel_dialog.activateWindow()
                return
            else:
                # Dialog exists but not visible - clean it up and recreate
                try:
                    self.live_wheel_dialog.close()
                    self.live_wheel_dialog.deleteLater()
                except Exception:
                    pass
                self.live_wheel_dialog = None

        # Create new dialog
        initial = QtGui.QColor(*self.last_color)
        self.live_wheel_dialog = QColorDialog(initial, self)
        self.live_wheel_dialog.setWindowTitle("Live Color Wheel")
        self.live_wheel_dialog.setOption(QColorDialog.ColorDialogOption.NoButtons)  # No OK/Cancel
        
        # Update lights in real-time as user changes color
        self.live_wheel_dialog.currentColorChanged.connect(self.on_live_color_change)
        
        # When closed, clean up
        self.live_wheel_dialog.finished.connect(self.on_live_wheel_closed)
        
        # Show as a separate window (not modal)
        self.live_wheel_dialog.setWindowFlags(
            Qt.WindowType.Window | 
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.live_wheel_dialog.show()

    def on_live_color_change(self, color: QtGui.QColor):
        """Called continuously as user drags in color wheel"""
        if self.cb_live_wheel_enabled.isChecked():
            rgb = (color.red(), color.green(), color.blue())
            self._set_rgb_targets(*rgb)
            self.lbl_live_color.setText(f"Current: RGB({color.red()},{color.green()},{color.blue()})")
            self.lbl_live_color.setStyleSheet(
                f"color: rgb({color.red()},{color.green()},{color.blue()}); font-weight: bold;"
            )
            # Update last_color so it persists
            self.last_color = rgb
            self.engine.base_color = rgb

    def on_live_wheel_closed(self):
        """Clean up when live color wheel is closed"""
        # Properly disconnect and clean up
        if self.live_wheel_dialog is not None:
            try:
                self.live_wheel_dialog.currentColorChanged.disconnect()
            except Exception:
                pass
            try:
                self.live_wheel_dialog.deleteLater()
            except Exception:
                pass
        self.live_wheel_dialog = None
        
        if not self.cb_live_wheel_enabled.isChecked():
            self.lbl_live_color.setText("Current: Not active")
            self.lbl_live_color.setStyleSheet("")

    def toggle_live_wheel(self, state):
        """Enable/disable live color wheel updates"""
        if state == 0:  # Unchecked
            self.live_wheel_active = False
            self.lbl_live_color.setText("Current: Not active")
            self.lbl_live_color.setStyleSheet("")
            # Close dialog if open
            if self.live_wheel_dialog is not None and self.live_wheel_dialog.isVisible():
                self.live_wheel_dialog.close()
        else:  # Checked
            self.live_wheel_active = True
            # Always try to open when enabled
            self.open_live_color_wheel()

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

    def _flash_color_ms(self, rgb: tuple, ms: int):
        """Flash a specific color for specified duration, then return to last color"""
        self._set_rgb_targets(*rgb)
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
        if self.cb_flash_on_beat.isChecked() or self.cb_color_flash_enabled.isChecked():
            # Choose flash color based on mode
            if self.cb_color_flash_enabled.isChecked():
                if self.rb_flash_cycle.isChecked():
                    # Cycle through rainbow color wheel
                    flash_rgb = self.flash_color_wheel[self.flash_color_index]
                    self.flash_color_index = (self.flash_color_index + 1) % len(self.flash_color_wheel)
                    # Update label to show current color
                    r, g, b = flash_rgb
                    self.lbl_flash_color.setText(f"● Cycling: RGB({r},{g},{b})")
                    self.lbl_flash_color.setStyleSheet(
                        f"font-weight: bold; font-size: 14px; color: rgb({r},{g},{b});"
                    )
                else:
                    # Use static picked color
                    flash_rgb = self.flash_color
            else:
                flash_rgb = (255, 255, 255)  # White (original flash on beat)
            
            if self.autoloops_enabled and hasattr(self.engine, '_energy_tier'):
                tier = self.engine._energy_tier()
                from autoloops import EnergyTier  # To ensure enum is available
                if tier == EnergyTier.HIGH:
                    self._flash_color_ms(flash_rgb, 150)  # Longer, punchier on drops
                elif tier == EnergyTier.MED:
                    self._flash_color_ms(flash_rgb, 90)
                else:
                    self._flash_color_ms(flash_rgb, 50)
            else:
                self._flash_color_ms(flash_rgb, 90)

        if self.autoloops_enabled:
            # Show energy label (require _energy_tier on engine)
            if hasattr(self.engine, '_energy_tier'):
                tier = self.engine._energy_tier()
                
                # Apply sensitivity multiplier to thresholds
                if self.sensitivity_multiplier != 1.0:
                    self.engine._sensitivity_scale = self.sensitivity_multiplier
                
                # Apply manual energy override if active
                if self.manual_mode and self.manual_energy_tier:
                    from autoloops import EnergyTier
                    tier_map = {"LOW": EnergyTier.LOW, "MED": EnergyTier.MED, "HIGH": EnergyTier.HIGH}
                    tier = tier_map[self.manual_energy_tier]
                
                self.lbl_energy.setText(f"Energy: {tier.name} (RMS: {ev.rms:.3f})")
            
            self.engine.on_beat(ev.bpm, ev.rms, ev.high, ev.bass)

    def start_autoloops(self):
        self.engine.set_style(self.combo_style.currentText())
        self.engine.set_palette(self.combo_palette.currentText())
        self.autoloops_enabled = True

    def stop_autoloops(self):
        self.autoloops_enabled = False

    # ---------- Manual Control ----------
    def toggle_manual_mode(self, checked):
        """Toggle between automatic and manual mode"""
        self.manual_mode = not checked  # auto_mode toggled, so manual is opposite
        
        # Enable/disable manual buttons
        self.btn_manual_low.setEnabled(self.manual_mode)
        self.btn_manual_med.setEnabled(self.manual_mode)
        self.btn_manual_high.setEnabled(self.manual_mode)
        
        if not self.manual_mode:
            self.manual_energy_tier = None

    def set_manual_energy(self, tier: str):
        """Set manual energy tier override"""
        self.manual_energy_tier = tier
        self.lbl_manual_energy.setText(f"Manual Energy Tier: {tier}")
        
        # Apply immediately to autoloops if active
        if self.autoloops_enabled:
            from autoloops import EnergyTier
            tier_map = {"LOW": EnergyTier.LOW, "MED": EnergyTier.MED, "HIGH": EnergyTier.HIGH}
            # Force the engine to use this tier (we'll modify handle_beat to check this)

    def trigger_build_manually(self):
        """Manually trigger a build sequence"""
        if self.autoloops_enabled:
            # Enter BUILD_FLASH program for 2 bars
            self.engine._enter_program(
                self.engine._program.__class__.BUILD_FLASH,
                beats=self.engine.state.bar_len * 2,
                bpm=120  # Default BPM
            )

    def trigger_drop_manually(self):
        """Manually trigger a drop sequence"""
        if self.autoloops_enabled:
            from autoloops import Program
            # Force drop sequence: blackout → rainbow strobe
            self.engine._drop_detected = True
            self.engine._drop_countdown = 2

    def update_sensitivity(self, value):
        """Update sensitivity multiplier for energy detection"""
        self.sensitivity_multiplier = value / 100.0  # 50-200 → 0.5-2.0
        self.lbl_sensitivity.setText(f"Sensitivity: {self.sensitivity_multiplier:.1f}x")

    # ---------- Alternating Effects ----------
    def toggle_alternating(self, state):
        """Enable/disable alternating light effects"""
        self.alternating_enabled = (state != 0)
        
        if self.alternating_enabled:
            # Start timer
            speed_ms = self.s_alt_speed.value() * 10  # slider 10-200 → 100-2000ms
            self.alternating_timer.start(speed_ms)
        else:
            self.alternating_timer.stop()

    def update_alt_speed(self, value):
        """Update alternating pattern speed"""
        speed_ms = value * 10  # 10-200 → 100-2000ms
        self.lbl_alt_speed.setText(f"{speed_ms}ms")
        if self.alternating_enabled:
            self.alternating_timer.setInterval(speed_ms)

    def run_alternating_pattern(self):
        """Execute alternating pattern based on selected mode"""
        if not self.lightA or not self.lightB:
            return  # Need both lights for alternating
        
        pattern = self.combo_alt_pattern.currentText()
        self.alternating_state = (self.alternating_state + 1) % 2
        
        if pattern == "Alternating Flash":
            # Flash A, then B, then A, etc.
            if self.alternating_state == 0:
                self._set_rgb_single(self.lightA, *self.last_color)
                self._set_rgb_single(self.lightB, 0, 0, 0)
            else:
                self._set_rgb_single(self.lightA, 0, 0, 0)
                self._set_rgb_single(self.lightB, *self.last_color)
        
        elif pattern == "Alternating On/Off":
            # One on, one off, swap
            if self.alternating_state == 0:
                self._set_rgb_single(self.lightA, *self.last_color)
                self._set_rgb_single(self.lightB, 0, 0, 0)
            else:
                self._set_rgb_single(self.lightA, 0, 0, 0)
                self._set_rgb_single(self.lightB, *self.last_color)
        
        elif pattern == "Alternating Colors":
            # A gets primary color, B gets alt color
            if self.alternating_state == 0:
                self._set_rgb_single(self.lightA, *self.last_color)
                self._set_rgb_single(self.lightB, *self.alt_color)
            else:
                self._set_rgb_single(self.lightA, *self.alt_color)
                self._set_rgb_single(self.lightB, *self.last_color)
        
        elif pattern == "Chase (A→B→A→B)":
            # Quick flash chase effect
            if self.alternating_state == 0:
                self._set_rgb_single(self.lightA, 255, 255, 255)
                QtCore.QTimer.singleShot(50, lambda: self._set_rgb_single(self.lightA, *self.last_color))
            else:
                self._set_rgb_single(self.lightB, 255, 255, 255)
                QtCore.QTimer.singleShot(50, lambda: self._set_rgb_single(self.lightB, *self.last_color))
        
        elif pattern == "Opposite Colors":
            # Complementary colors
            r, g, b = self.last_color
            comp_r, comp_g, comp_b = (255 - r, 255 - g, 255 - b)
            if self.alternating_state == 0:
                self._set_rgb_single(self.lightA, r, g, b)
                self._set_rgb_single(self.lightB, comp_r, comp_g, comp_b)
            else:
                self._set_rgb_single(self.lightA, comp_r, comp_g, comp_b)
                self._set_rgb_single(self.lightB, r, g, b)

    def _set_rgb_single(self, handle: ble.LightHandle, r: int, g: int, b: int):
        """Set RGB for a single light (used by alternating effects)"""
        bscale = self.s_brightness.value() / 100.0
        r = int(r * bscale); g = int(g * bscale); b = int(b * bscale)
        serialized = [handle.__dict__]
        QtCore.QMetaObject.invokeMethod(
            self.ble_worker, "set_rgb_multi",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(list, serialized),
            QtCore.Q_ARG(int, r), QtCore.Q_ARG(int, g), QtCore.Q_ARG(int, b), QtCore.Q_ARG(int, 0)
        )

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
