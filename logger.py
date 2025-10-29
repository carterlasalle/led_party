#!/usr/bin/env python3
"""
Diagnostic Logger for LED Party Light Controller

This file provides comprehensive beat-by-beat logging for debugging
audio analysis, energy detection, and program transitions.

When to update:
- When adding new detectors or energy metrics
- When debugging drop/build/breakdown detection
- When tuning thresholds or parameters
"""
import csv
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class LogEntry:
    timestamp: float
    beat_num: int
    bpm: float
    rms: float
    bass: float
    mid: float
    high: float
    ema_fast: float
    ema_med: float
    ema_long: float
    energy_tier: str
    program: str
    bar_pos: int
    phrase_boundary: bool
    drop_detected: bool
    build_detected: bool
    breakdown_detected: bool

class DiagnosticLogger:
    """
    CSV logger that writes beat-by-beat diagnostics for post-analysis.
    Creates timestamped files in user's home directory.
    """
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.file = None
        self.writer = None
        if enabled:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = Path.home() / f"lightdesk_log_{timestamp}.csv"
            self.file = open(filename, 'w', newline='')
            self.writer = csv.DictWriter(self.file, fieldnames=[
                'timestamp', 'beat_num', 'bpm', 'rms', 'bass', 'mid', 'high',
                'ema_fast', 'ema_med', 'ema_long', 'energy_tier', 'program',
                'bar_pos', 'phrase_boundary', 'drop_detected', 'build_detected', 'breakdown_detected'
            ])
            self.writer.writeheader()
            print(f"📊 Diagnostic logging enabled: {filename}")
    
    def log(self, entry: LogEntry):
        """Write a single beat's data to CSV"""
        if self.enabled and self.writer:
            self.writer.writerow(asdict(entry))
            self.file.flush()  # Write immediately for real-time monitoring
    
    def close(self):
        """Clean shutdown - close file handle"""
        if self.file:
            self.file.close()
            print("📊 Diagnostic log closed")

