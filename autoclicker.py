#!/usr/bin/env python3
"""
WayClick — Wayland-Native Kernel-Level Auto Clicker & Key Presser
=================================================================
Designed for Linux (KDE Plasma / Wayland & XWayland / Proton Games).
Uses Linux kernel `evdev` for global hardware hotkey detection and `uinput`
for hardware-level virtual mouse/keyboard injection with zero external daemons.

Includes real-time App/Game Window Tracking & Whitelist Locking to prevent
unwanted auto-clicks from firing outside your designated target game or app.

All configuration and game/app presets are stored strictly inside `./config.json`
in this directory to adhere to a strict Clean System Policy.
"""

from __future__ import annotations

import json
import os
import random
import select
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import evdev
from evdev import InputDevice, UInput, ecodes

# Use XCB (XWayland) backend so global cursor positioning (`QCursor.pos()`)
# and per-screen countdown overlays map 1:1 across HiDPI displays on KDE Wayland.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PyQt6.QtCore import QObject, QRectF, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QScreen,
)
from PyQt6.QtDBus import QDBusConnection, QDBusInterface
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
ICON_SVG_PATH = APP_DIR / "icon.svg"
ICON_PNG_PATH = APP_DIR / "icon.png"
VIRTUAL_DEVICE_NAME = "WayClick-Virtual-Input"


def build_app_icon(active: bool = False, paused: bool = False) -> QIcon:
    """Load the custom WayClick icon, optionally badged with a live green (active) or yellow (paused) dot."""
    base_path = str(ICON_SVG_PATH if ICON_SVG_PATH.exists() else ICON_PNG_PATH)
    base_icon = QIcon(base_path)
    if not active:
        return base_icon

    pix = base_icon.pixmap(QSize(128, 128))
    if pix.isNull():
        return base_icon
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor("#12141c"), 6.0))
    badge_color = QColor("#f6c177") if paused else QColor("#56d364")
    painter.setBrush(badge_color)
    painter.drawEllipse(QRectF(82.0, 82.0, 40.0, 40.0))
    painter.end()
    return QIcon(pix)


# ---------------------------------------------------------------------------
# Key & Button Lookup Helpers
# ---------------------------------------------------------------------------

MOUSE_BUTTONS: List[Tuple[str, int]] = [
    ("Left Mouse Button (LMB)", ecodes.BTN_LEFT),
    ("Right Mouse Button (RMB)", ecodes.BTN_RIGHT),
    ("Middle Mouse Button (MMB)", ecodes.BTN_MIDDLE),
    ("Mouse Side Back (MB4)", ecodes.BTN_SIDE),
    ("Mouse Side Forward (MB5)", ecodes.BTN_EXTRA),
]

COMMON_KEYBOARD_KEYS: List[Tuple[str, int]] = [
    ("Spacebar (KEY_SPACE)", ecodes.KEY_SPACE),
    ("E (KEY_E)", ecodes.KEY_E),
    ("F (KEY_F)", ecodes.KEY_F),
    ("Enter (KEY_ENTER)", ecodes.KEY_ENTER),
    ("Left Shift (KEY_LEFTSHIFT)", ecodes.KEY_LEFTSHIFT),
    ("Left Ctrl (KEY_LEFTCTRL)", ecodes.KEY_LEFTCTRL),
    ("Tab (KEY_TAB)", ecodes.KEY_TAB),
    ("1 (KEY_1)", ecodes.KEY_1),
    ("2 (KEY_2)", ecodes.KEY_2),
    ("3 (KEY_3)", ecodes.KEY_3),
    ("4 (KEY_4)", ecodes.KEY_4),
    ("5 (KEY_5)", ecodes.KEY_5),
    ("Q (KEY_Q)", ecodes.KEY_Q),
    ("R (KEY_R)", ecodes.KEY_R),
    ("W (KEY_W)", ecodes.KEY_W),
    ("A (KEY_A)", ecodes.KEY_A),
    ("S (KEY_S)", ecodes.KEY_S),
    ("D (KEY_D)", ecodes.KEY_D),
    ("C (KEY_C)", ecodes.KEY_C),
    ("V (KEY_V)", ecodes.KEY_V),
    ("X (KEY_X)", ecodes.KEY_X),
    ("Z (KEY_Z)", ecodes.KEY_Z),
]


def format_ecode_name(code: int) -> str:
    """Return a clean human-readable name for an evdev key/button code."""
    custom_names = {
        ecodes.BTN_LEFT: "Left Mouse (BTN_LEFT)",
        ecodes.BTN_RIGHT: "Right Mouse (BTN_RIGHT)",
        ecodes.BTN_MIDDLE: "Middle Mouse (BTN_MIDDLE)",
        ecodes.BTN_SIDE: "Mouse Back / MB4 (BTN_SIDE)",
        ecodes.BTN_EXTRA: "Mouse Forward / MB5 (BTN_EXTRA)",
    }
    if code in custom_names:
        return custom_names[code]

    raw = ecodes.keys.get(code)
    if isinstance(raw, (list, tuple)):
        raw = raw[0]
    if not raw:
        return f"Code {code}"
    clean = raw.replace("KEY_", "").replace("BTN_", "Mouse ")
    return f"{clean} ({raw})"


# ---------------------------------------------------------------------------
# Configuration & App/Game Presets Model
# ---------------------------------------------------------------------------

@dataclass
class ClickerConfig:
    # Target action: "mouse" or "keyboard"
    action_type: str = "mouse"
    mouse_button_code: int = ecodes.BTN_LEFT
    keyboard_key_code: int = ecodes.KEY_SPACE
    double_click: bool = False

    # Speed & timing
    interval_ms: float = 100.0        # Base delay between clicks (100ms = 10 CPS)
    hold_duration_ms: float = 15.0    # How long the button stays held down per click

    # Humanizer / Random Jitter
    jitter_enabled: bool = False
    jitter_ms: float = 20.0           # +/- random ms added to interval

    # App / Game Window Whitelist & Target Lock
    window_lock_enabled: bool = False
    window_whitelist: str = ""        # Comma-separated app classes or window titles
    window_lock_mode: str = "pause"   # "pause" (suspend clicks while unfocused) or "stop"

    # Activation & Trigger
    hotkey_code: int = ecodes.KEY_F6
    trigger_mode: str = "toggle"      # "toggle" or "hold"
    gui_start_delay_sec: float = 2.0  # 2s default delay when clicking GUI Start button

    # Stop conditions
    limit_mode: str = "infinite"      # "infinite", "count", "duration"
    max_clicks: int = 1000
    max_duration_sec: float = 60.0
    stop_on_real_click: bool = True   # Stop immediately when a physical mouse click is detected

    # Window behavior
    always_on_top: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "ClickerConfig":
        valid_keys = {k for k in cls.__dataclass_fields__.keys()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class PresetStore:
    """Manages multiple named app/game presets persisted in `./config.json`."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self.presets: Dict[str, ClickerConfig] = {}
        self.active_name: str = "Default (10 CPS LMB)"
        self.load()

    @staticmethod
    def default_presets() -> Dict[str, ClickerConfig]:
        return {
            "Default (10 CPS LMB)": ClickerConfig(
                action_type="mouse",
                mouse_button_code=ecodes.BTN_LEFT,
                interval_ms=100.0,
                hold_duration_ms=15.0,
                jitter_enabled=False,
                hotkey_code=ecodes.KEY_F6,
                trigger_mode="toggle",
                window_lock_enabled=False,
            ),
            "Fast Clicker (25 CPS + Jitter)": ClickerConfig(
                action_type="mouse",
                mouse_button_code=ecodes.BTN_LEFT,
                interval_ms=40.0,
                hold_duration_ms=12.0,
                jitter_enabled=True,
                jitter_ms=8.0,
                hotkey_code=ecodes.KEY_F6,
                trigger_mode="toggle",
                window_lock_enabled=False,
            ),
            "Cell to Singularity / Idle Game (Locked)": ClickerConfig(
                action_type="mouse",
                mouse_button_code=ecodes.BTN_LEFT,
                interval_ms=35.0,
                hold_duration_ms=12.0,
                jitter_enabled=True,
                jitter_ms=6.0,
                hotkey_code=ecodes.KEY_F6,
                trigger_mode="toggle",
                window_lock_enabled=True,
                window_whitelist="steam_app_977400, Cell to Singularity",
                window_lock_mode="pause",
            ),
            "Hold-to-Spam (MB4 Rapid LMB)": ClickerConfig(
                action_type="mouse",
                mouse_button_code=ecodes.BTN_LEFT,
                interval_ms=50.0,
                hold_duration_ms=14.0,
                jitter_enabled=True,
                jitter_ms=6.0,
                hotkey_code=ecodes.BTN_SIDE,
                trigger_mode="hold",
                stop_on_real_click=False,
                window_lock_enabled=False,
            ),
            "Game Key Presser (Spacebar)": ClickerConfig(
                action_type="keyboard",
                keyboard_key_code=ecodes.KEY_SPACE,
                interval_ms=100.0,
                hold_duration_ms=20.0,
                jitter_enabled=True,
                jitter_ms=12.0,
                hotkey_code=ecodes.KEY_F6,
                trigger_mode="toggle",
                window_lock_enabled=False,
            ),
        }

    def load(self) -> None:
        if not self.path.exists():
            self.presets = self.default_presets()
            self.active_name = next(iter(self.presets.keys()))
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "presets" in raw and isinstance(raw["presets"], dict):
                loaded: Dict[str, ClickerConfig] = {}
                for name, cfg_dict in raw["presets"].items():
                    if isinstance(cfg_dict, dict):
                        loaded[str(name)] = ClickerConfig.from_dict(cfg_dict)
                if loaded:
                    # Provide built-in defaults for any newly added default presets
                    defaults = self.default_presets()
                    for def_name, def_cfg in defaults.items():
                        if def_name not in loaded:
                            loaded[def_name] = def_cfg
                    self.presets = loaded
                    active = str(raw.get("active_preset", ""))
                    self.active_name = active if active in self.presets else next(iter(self.presets.keys()))
                    return
            # Backwards compatibility for flat single-config JSON
            if isinstance(raw, dict):
                self.presets = self.default_presets()
                self.presets["Default (10 CPS LMB)"] = ClickerConfig.from_dict(raw)
                self.active_name = "Default (10 CPS LMB)"
                return
        except Exception:
            pass
        self.presets = self.default_presets()
        self.active_name = next(iter(self.presets.keys()))

    def save(self) -> None:
        payload = {
            "active_preset": self.active_name,
            "presets": {name: asdict(cfg) for name, cfg in self.presets.items()},
        }
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            pass

    @property
    def active_config(self) -> ClickerConfig:
        return self.presets[self.active_name]


# ---------------------------------------------------------------------------
# Real-Time Active Window Tracker (KWin Scripting + xprop Fallback)
# ---------------------------------------------------------------------------

class ActiveWindowTracker(QObject):
    """
    Monitors the active/focused desktop window in real time on Linux.
    Uses native KWin 6 scripting on KDE Wayland (zero latency, zero polling)
    with an automatic `xprop -root -spy _NET_ACTIVE_WINDOW` fallback for other
    desktop environments (GNOME, X11, Sway).
    """

    window_changed = pyqtSignal(str, str, int)  # (caption, resource_class, pid)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self.active_caption: str = ""
        self.active_class: str = ""
        self.active_pid: int = 0
        self.last_external_caption: str = ""
        self.last_external_class: str = ""

        self._script_id: int = -1
        self._tmp_path: str = ""
        self._fallback_stop_event = threading.Event()
        self._fallback_thread: Optional[threading.Thread] = None

        if not self._setup_kwin():
            self._setup_fallback()

    def _setup_kwin(self) -> bool:
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return False
            scripting = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus)
            if not scripting.isValid():
                return False

            service_name = f"org.wayclick.Tracker_{os.getpid()}"
            bus.registerService(service_name)
            bus.registerObject("/Tracker", self, QDBusConnection.RegisterOption.ExportAllSlots)

            script_content = f"""
            function notify() {{
                var win = workspace.activeWindow;
                var caption = win ? (win.caption || '') : '';
                var cls = win ? (win.resourceClass || '') : '';
                var pid = win ? (win.pid || 0) : 0;
                callDBus('{service_name}', '/Tracker', 'local.ActiveWindowTracker', 'onWindowActivated', caption, cls, pid);
            }}
            notify();
            workspace.windowActivated.connect(notify);
            """
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(script_content)
                self._tmp_path = f.name

            plugin_name = f"wayclick_win_{os.getpid()}"
            reply = scripting.call("loadScript", self._tmp_path, plugin_name)
            if reply.arguments():
                self._script_id = int(reply.arguments()[0])
                script_obj = QDBusInterface(
                    "org.kde.KWin",
                    f"/Scripting/Script{self._script_id}",
                    "org.kde.kwin.Script",
                    bus,
                )
                script_obj.call("run")
                return True
        except Exception:
            pass
        return False

    def _setup_fallback(self) -> None:
        self._fallback_thread = threading.Thread(
            target=self._fallback_loop, daemon=True, name="WayClick-WindowFallback"
        )
        self._fallback_thread.start()

    def _fallback_loop(self) -> None:
        try:
            proc = subprocess.Popen(
                ["xprop", "-root", "-spy", "_NET_ACTIVE_WINDOW"],
                stdout=subprocess.PIPE,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            while not self._fallback_stop_event.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                if "window id #" in line:
                    wid = line.strip().split()[-1]
                    if wid and wid != "0x0":
                        self._query_xprop_window(wid)
            proc.terminate()
        except Exception:
            pass

    def _query_xprop_window(self, wid: str) -> None:
        try:
            out = subprocess.check_output(
                ["xprop", "-id", wid, "WM_CLASS", "_NET_WM_NAME"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
            classes: List[str] = []
            name = ""
            for line in out.splitlines():
                if "WM_CLASS" in line and "=" in line:
                    parts = [p.strip(' ",') for p in line.split("=", 1)[1].split(",")]
                    classes = [p for p in parts if p]
                elif "_NET_WM_NAME" in line and "=" in line:
                    name = line.split("=", 1)[1].strip(' ",')
            res_class = " / ".join(classes) if classes else ""
            self.onWindowActivated(name, res_class, 0)
        except Exception:
            pass

    @pyqtSlot(str, str, int)
    def onWindowActivated(self, caption: str, resource_class: str, pid: int) -> None:
        with self._lock:
            self.active_caption = caption
            self.active_class = resource_class
            self.active_pid = pid
            low_cls = resource_class.lower()
            if "wayclick" not in low_cls and "autoclicker" not in low_cls and (resource_class or caption):
                self.last_external_caption = caption
                self.last_external_class = resource_class
        self.window_changed.emit(caption, resource_class, pid)

    def is_whitelisted(self, whitelist_str: str) -> bool:
        """Return True if the current active window matches any comma-separated pattern in the whitelist."""
        if not whitelist_str or not whitelist_str.strip():
            return True
        patterns = [p.strip().lower() for p in whitelist_str.split(",") if p.strip()]
        if not patterns:
            return True

        with self._lock:
            cap = self.active_caption.lower()
            cls = self.active_class.lower()

        return any(p in cap or p in cls for p in patterns)

    def cleanup(self) -> None:
        self._fallback_stop_event.set()
        if self._script_id >= 0:
            try:
                bus = QDBusConnection.sessionBus()
                scripting = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus)
                plugin_name = f"wayclick_win_{os.getpid()}"
                scripting.call("unloadScript", plugin_name)
            except Exception:
                pass
            self._script_id = -1
        if self._tmp_path and os.path.exists(self._tmp_path):
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Kernel UInput Virtual Injector
# ---------------------------------------------------------------------------

class VirtualInputDevice:
    """
    Wraps `/dev/uinput` to emit hardware-level mouse clicks and key presses.
    Note: `libinput` requires `EV_REL` (`REL_X`, `REL_Y`) to be declared alongside
    `BTN_LEFT` so that the kernel & Wayland compositor classify the device as a valid pointer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ui: Optional[UInput] = None
        self._open()

    def _open(self) -> None:
        mouse_btns = [
            ecodes.BTN_LEFT,
            ecodes.BTN_RIGHT,
            ecodes.BTN_MIDDLE,
            ecodes.BTN_SIDE,
            ecodes.BTN_EXTRA,
        ]
        kbd_keys = [
            k for k in ecodes.keys.keys()
            if isinstance(k, int) and 1 <= k <= 248
        ]
        capabilities = {
            ecodes.EV_KEY: sorted(set(mouse_btns + kbd_keys)),
            ecodes.EV_REL: [
                ecodes.REL_X,
                ecodes.REL_Y,
                ecodes.REL_WHEEL,
                ecodes.REL_HWHEEL,
            ],
        }
        self._ui = UInput(capabilities, name=VIRTUAL_DEVICE_NAME)

    def send_key(self, code: int, value: int) -> None:
        """Emit key/button press (value=1) or release (value=0) and synchronize."""
        with self._lock:
            if self._ui is not None:
                self._ui.write(ecodes.EV_KEY, code, value)
                self._ui.syn()

    def close(self) -> None:
        with self._lock:
            if self._ui is not None:
                try:
                    self._ui.close()
                except Exception:
                    pass
                self._ui = None


# ---------------------------------------------------------------------------
# Background Auto-Click & Key-Press Worker
# ---------------------------------------------------------------------------

class ClickEngine(QObject):
    """High-precision background worker for executing clicks/keypresses."""

    state_changed = pyqtSignal(bool)          # True = active, False = stopped
    stats_updated = pyqtSignal(int, float)    # (total_clicks, instantaneous_cps)
    status_message = pyqtSignal(str)
    countdown_started = pyqtSignal(float)     # Countdown duration in seconds
    countdown_finished = pyqtSignal()
    pause_state_changed = pyqtSignal(bool)    # True = paused by window lock, False = active

    def __init__(
        self,
        vinput: VirtualInputDevice,
        config: ClickerConfig,
        window_tracker: Optional[ActiveWindowTracker] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._vinput = vinput
        self._config = config
        self._window_tracker = window_tracker
        self._running = False
        self._is_paused = False
        self._started_at = 0.0
        self._stopped_at = 0.0
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._session_clicks = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def stopped_at(self) -> float:
        return self._stopped_at

    def update_config(self, config: ClickerConfig) -> None:
        self._config = config

    def update_window_tracker(self, tracker: ActiveWindowTracker) -> None:
        self._window_tracker = tracker

    def toggle(self, from_gui: bool = False) -> None:
        if self._running:
            self.stop()
        else:
            self.start(from_gui=from_gui)

    def start(self, from_gui: bool = False) -> None:
        if self._running:
            return
        self._running = True
        self._is_paused = False
        self._started_at = time.monotonic()
        self._stop_event.clear()
        self.state_changed.emit(True)
        self._worker_thread = threading.Thread(
            target=self._run_loop,
            args=(from_gui,),
            daemon=True,
            name="WayClick-Worker",
        )
        self._worker_thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._is_paused:
            self._is_paused = False
            self.pause_state_changed.emit(False)
        self._stopped_at = time.monotonic()
        self.state_changed.emit(False)

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep for `seconds` with sub-millisecond precision at tail, returning True if stopped."""
        if seconds <= 0:
            return self._stop_event.is_set()
        deadline = time.perf_counter() + seconds
        remaining = deadline - time.perf_counter()
        if remaining > 0.002:
            if self._stop_event.wait(timeout=remaining - 0.0015):
                return True
        while time.perf_counter() < deadline:
            if self._stop_event.is_set():
                return True
            time.sleep(0.0002)
        return self._stop_event.is_set()

    def _perform_single_press(self, target_code: int, hold_sec: float) -> None:
        """Press target key/button, hold for `hold_sec`, and guaranteed-release."""
        try:
            self._vinput.send_key(target_code, 1)
            if hold_sec > 0:
                deadline = time.perf_counter() + hold_sec
                while time.perf_counter() < deadline and not self._stop_event.is_set():
                    time.sleep(min(0.001, max(0.0001, deadline - time.perf_counter())))
        finally:
            self._vinput.send_key(target_code, 0)

    def _run_loop(self, from_gui: bool) -> None:
        cfg = self._config

        # Optional start countdown when clicking GUI button directly
        if from_gui and cfg.gui_start_delay_sec > 0:
            delay = cfg.gui_start_delay_sec
            self.status_message.emit(f"Starting in {delay:.1f}s — move cursor to target...")
            self.countdown_started.emit(delay)
            interrupted = self._sleep_interruptible(delay)
            self.countdown_finished.emit()
            if interrupted:
                self._running = False
                self._stopped_at = time.monotonic()
                self.state_changed.emit(False)
                return
            time.sleep(0.015)

        self._session_clicks = 0
        start_time = time.perf_counter()
        window_timestamps: List[float] = []

        while not self._stop_event.is_set():
            cfg = self._config

            # --- Target Window Lock & Whitelist Gate ---
            if self._window_tracker is not None and cfg.window_lock_enabled and cfg.window_whitelist.strip():
                if not self._window_tracker.is_whitelisted(cfg.window_whitelist):
                    if cfg.window_lock_mode == "stop":
                        self.status_message.emit("Stopped: Target window lost focus (Window Lock active).")
                        break
                    else:  # "pause" mode
                        if not self._is_paused:
                            self._is_paused = True
                            self.pause_state_changed.emit(True)
                            self.status_message.emit("❚❚ Paused: Waiting for whitelisted window to gain focus...")
                        if self._sleep_interruptible(0.05):
                            break
                        continue
                else:
                    if self._is_paused:
                        self._is_paused = False
                        self.pause_state_changed.emit(False)
                        self.status_message.emit("Resumed: Target window focused.")

            target_code = (
                cfg.mouse_button_code
                if cfg.action_type == "mouse"
                else cfg.keyboard_key_code
            )

            base_hold_ms = max(1.0, cfg.hold_duration_ms)
            if cfg.jitter_enabled and cfg.jitter_ms > 0:
                hold_jitter = random.uniform(
                    -min(5.0, cfg.jitter_ms * 0.25),
                    min(5.0, cfg.jitter_ms * 0.25),
                )
                hold_ms = max(1.0, base_hold_ms + hold_jitter)
            else:
                hold_ms = base_hold_ms

            effective_hold_sec = min(hold_ms, max(1.0, cfg.interval_ms * 0.75)) / 1000.0

            # Execute click / key press
            self._perform_single_press(target_code, effective_hold_sec)
            self._session_clicks += 1

            # Optional double-click
            if cfg.double_click and not self._stop_event.is_set():
                if not self._sleep_interruptible(0.035):
                    self._perform_single_press(target_code, effective_hold_sec)
                    self._session_clicks += 1

            now = time.perf_counter()
            window_timestamps.append(now)
            cutoff = now - 1.0
            while window_timestamps and window_timestamps[0] < cutoff:
                window_timestamps.pop(0)

            inst_cps = float(len(window_timestamps))
            self.stats_updated.emit(self._session_clicks, inst_cps)

            # Check stop conditions
            if cfg.limit_mode == "count" and self._session_clicks >= cfg.max_clicks:
                self.status_message.emit(f"Completed target limit ({cfg.max_clicks} actions).")
                break
            if cfg.limit_mode == "duration" and (now - start_time) >= cfg.max_duration_sec:
                self.status_message.emit(f"Completed target duration ({cfg.max_duration_sec:.1f}s).")
                break

            # Sleep interval until next cycle
            base_interval_ms = max(1.0, cfg.interval_ms)
            if cfg.jitter_enabled and cfg.jitter_ms > 0:
                delta = random.uniform(-cfg.jitter_ms, cfg.jitter_ms)
                total_cycle_ms = max(2.0, base_interval_ms + delta)
            else:
                total_cycle_ms = base_interval_ms

            rest_sec = max(0.0005, (total_cycle_ms / 1000.0) - effective_hold_sec)
            if self._sleep_interruptible(rest_sec):
                break

        self._running = False
        if self._is_paused:
            self._is_paused = False
            self.pause_state_changed.emit(False)
        self._stopped_at = time.monotonic()
        self.state_changed.emit(False)


# ---------------------------------------------------------------------------
# Global Evdev Hardware Hotkey Listener
# ---------------------------------------------------------------------------

class EvdevHotkeyMonitor(QObject):
    """
    Listens directly to physical `/dev/input/event*` devices so global hotkeys,
    ESC emergency stop, and Real Physical Click stops work inside any Wayland
    window or fullscreen Proton game. Excludes our own UInput virtual device.
    """

    hotkey_pressed = pyqtSignal()
    hotkey_released = pyqtSignal()
    emergency_stop = pyqtSignal()          # Always-active ESC panic key
    real_click_detected = pyqtSignal(int)  # Physical mouse button code (e.g. BTN_LEFT)
    key_captured = pyqtSignal(int, str)    # (code, target_purpose: "hotkey" | "action_key")

    def __init__(self, config: ClickerConfig) -> None:
        super().__init__()
        self._config = config
        self._stop_event = threading.Event()
        self._capture_mode: Optional[str] = None
        self.last_physical_lmb_ts: float = 0.0
        self._thread = threading.Thread(
            target=self._listen_loop,
            daemon=True,
            name="WayClick-EvdevMonitor",
        )
        self._thread.start()

    def update_config(self, config: ClickerConfig) -> None:
        self._config = config

    def start_capture(self, purpose: str) -> None:
        self._capture_mode = purpose

    def cancel_capture(self) -> None:
        self._capture_mode = None

    def stop(self) -> None:
        self._stop_event.set()

    def _scan_devices(self, current: Dict[str, InputDevice]) -> Dict[str, InputDevice]:
        updated: Dict[str, InputDevice] = {}
        for path in evdev.list_devices():
            if path in current:
                updated[path] = current[path]
                continue
            try:
                dev = InputDevice(path)
                if dev.name == VIRTUAL_DEVICE_NAME:
                    dev.close()
                    continue
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps:
                    updated[path] = dev
                else:
                    dev.close()
            except Exception:
                pass
        return updated

    def _listen_loop(self) -> None:
        devices: Dict[str, InputDevice] = {}
        last_scan = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()
            if now - last_scan > 2.5 or not devices:
                devices = self._scan_devices(devices)
                last_scan = now

            if not devices:
                time.sleep(0.5)
                continue

            fd_map = {dev.fd: (path, dev) for path, dev in list(devices.items())}
            try:
                rlist, _, _ = select.select(list(fd_map.keys()), [], [], 0.25)
            except Exception:
                devices.clear()
                continue

            for fd in rlist:
                path, dev = fd_map[fd]
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        if event.code == ecodes.BTN_LEFT and event.value == 1:
                            self.last_physical_lmb_ts = time.monotonic()

                        # Always-active ESC emergency stop
                        if event.code == ecodes.KEY_ESC and event.value == 1 and self._capture_mode is None:
                            self.emergency_stop.emit()
                            continue

                        if self._capture_mode is not None and event.value == 1:
                            purpose = self._capture_mode
                            if purpose == "hotkey" and event.code == ecodes.BTN_LEFT:
                                continue
                            if purpose == "action_key" and event.code in (
                                ecodes.BTN_LEFT,
                                ecodes.BTN_RIGHT,
                                ecodes.BTN_MIDDLE,
                            ):
                                continue
                            self._capture_mode = None
                            self.key_captured.emit(event.code, purpose)
                            continue

                        if event.code == self._config.hotkey_code:
                            if event.value == 1:
                                self.hotkey_pressed.emit()
                            elif event.value == 0:
                                self.hotkey_released.emit()
                            continue

                        if (
                            self._config.stop_on_real_click
                            and event.value == 1
                            and event.code in (ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE)
                        ):
                            self.real_click_detected.emit(event.code)
                except OSError:
                    try:
                        dev.close()
                    except Exception:
                        pass
                    devices.pop(path, None)


# ---------------------------------------------------------------------------
# Zero-Drift Per-Screen Cursor Countdown Overlay
# ---------------------------------------------------------------------------

class ScreenCountdownCanvas(QWidget):
    """
    Stationary fullscreen translucent canvas for a single monitor during the
    start countdown. Maps 1:1 with the physical cursor all the way to the extreme
    screen edges on fractional HiDPI (1.25x) multi-monitor setups with zero lag or drift.
    """

    RING_DIAMETER = 88

    def __init__(self, screen: QScreen, controller: "CursorCountdownController") -> None:
        super().__init__(None)
        self._screen = screen
        self._controller = controller

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setGeometry(screen.geometry())

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        self.update()
        super().mouseMoveEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        remaining = self._controller.remaining_sec
        duration = self._controller.duration_sec
        if remaining <= 0.0 or duration <= 0.0:
            return

        global_pos = QCursor.pos()
        if not self._screen.geometry().contains(global_pos):
            return

        local_pos = self.mapFromGlobal(global_pos)
        cx = float(local_pos.x())
        cy = float(local_pos.y())
        radius = self.RING_DIAMETER / 2.0

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        fraction = max(0.0, min(1.0, remaining / max(0.01, duration)))
        ring_rect = QRectF(cx - radius, cy - radius, self.RING_DIAMETER, self.RING_DIAMETER)

        # 1. High-contrast dark outer ring track
        track_pen = QPen(QColor(18, 20, 26, 195), 7.0)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(ring_rect)

        # 2. Shrinking countdown arc (Blue -> Ready Green)
        arc_color = QColor("#8ab4f8") if fraction > 0.35 else QColor("#56d364")
        arc_pen = QPen(arc_color, 5.0)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        start_angle = 90 * 16
        span_angle = int(-360 * 16 * fraction)
        painter.drawArc(ring_rect, start_angle, span_angle)

        # 3. Subtle center reticle around exact cursor tip
        reticle_pen = QPen(QColor(255, 255, 255, 130), 1.2)
        painter.setPen(reticle_pen)
        painter.drawEllipse(QRectF(cx - 4.0, cy - 4.0, 8.0, 8.0))

        # 4. Remaining seconds pill badge below the ring
        badge_rect = QRectF(cx - 24.0, cy + radius - 10.0, 48.0, 20.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 20, 26, 225))
        painter.drawRoundedRect(badge_rect, 6.0, 6.0)

        font = QFont("Inter", 9, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(arc_color)
        painter.drawText(
            badge_rect,
            Qt.AlignmentFlag.AlignCenter,
            f"{remaining:.1f}s",
        )


class CursorCountdownController(QObject):
    """Manages per-screen zero-drift countdown overlays across all monitors."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.duration_sec: float = 2.0
        self.remaining_sec: float = 0.0
        self._start_ts: float = 0.0
        self._canvases: List[ScreenCountdownCanvas] = []

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 FPS arc animation
        self._timer.timeout.connect(self._on_tick)

    @pyqtSlot(float)
    def start_countdown(self, duration_sec: float) -> None:
        if duration_sec <= 0:
            return
        self.stop_countdown()
        self.duration_sec = duration_sec
        self.remaining_sec = duration_sec
        self._start_ts = time.perf_counter()

        for screen in QApplication.screens():
            canvas = ScreenCountdownCanvas(screen, self)
            canvas.show()
            canvas.raise_()
            self._canvases.append(canvas)

        self._timer.start()

    @pyqtSlot()
    def stop_countdown(self) -> None:
        self._timer.stop()
        self.remaining_sec = 0.0
        for canvas in self._canvases:
            canvas.hide()
            canvas.deleteLater()
        self._canvases.clear()

    def _on_tick(self) -> None:
        elapsed = time.perf_counter() - self._start_ts
        self.remaining_sec = max(0.0, self.duration_sec - elapsed)
        if self.remaining_sec <= 0.0:
            self.stop_countdown()
            return
        for canvas in self._canvases:
            canvas.update()


# ---------------------------------------------------------------------------
# PyQt6 Main Window GUI
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.store = PresetStore()
        self.config = self.store.active_config
        self.vinput = VirtualInputDevice()
        self.window_tracker = ActiveWindowTracker(self)
        self.engine = ClickEngine(self.vinput, self.config, self.window_tracker, self)
        self.monitor = EvdevHotkeyMonitor(self.config)
        self.countdown_overlay = CursorCountdownController(self)
        self._syncing_speed_inputs = False
        self._loading_preset_ui = False
        self._last_real_click_stop_ts = 0.0
        self._capture_countdown_remaining = 0
        self._capture_timer: Optional[QTimer] = None

        self.setWindowTitle("WayClick — Wayland Auto Clicker & Key Presser")
        self.setWindowIcon(build_app_icon(active=False))
        self.setMinimumWidth(560)

        self._apply_dark_theme()
        self._build_ui()
        self._setup_tray_icon()
        self._refresh_preset_combo()
        self._load_config_into_ui()
        self._connect_signals()
        self._apply_always_on_top(self.config.always_on_top)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #181a20;
                color: #e6e9f0;
                font-family: "Inter", "Noto Sans", sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #2b2f3a;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px 10px 10px 10px;
                font-weight: bold;
                color: #8ab4f8;
                background-color: #1f222a;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {
                background-color: #12141a;
                border: 1px solid #353a47;
                border-radius: 5px;
                padding: 5px 8px;
                color: #ffffff;
                min-height: 22px;
            }
            QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QLineEdit:focus {
                border: 1px solid #8ab4f8;
            }
            QPushButton {
                background-color: #2b303c;
                border: 1px solid #3d4455;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
                color: #e6e9f0;
            }
            QPushButton:hover {
                background-color: #363c4b;
                border-color: #525b70;
            }
            QPushButton:pressed {
                background-color: #222630;
            }
            QCheckBox, QRadioButton {
                spacing: 7px;
            }
            QStatusBar {
                background-color: #12141a;
                color: #9aa0ac;
                border-top: 1px solid #2b2f3a;
            }
            """
        )

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(14, 14, 14, 10)
        root_layout.setSpacing(10)

        # 0. Top Status & Telemetry Banner
        self.status_banner = QFrame()
        self.status_banner.setStyleSheet(
            "QFrame { background-color: #1f2430; border: 1px solid #32394a; border-radius: 8px; padding: 8px; }"
        )
        banner_layout = QHBoxLayout(self.status_banner)

        status_col = QVBoxLayout()
        self.lbl_state = QLabel("● IDLE (STANDBY)")
        self.lbl_state.setStyleSheet("font-size: 16px; font-weight: bold; color: #9aa0ac; border: none;")
        self.lbl_hotkey_hint = QLabel("Press F6 anywhere to start/stop")
        self.lbl_hotkey_hint.setStyleSheet("font-size: 12px; color: #8ab4f8; border: none;")
        status_col.addWidget(self.lbl_state)
        status_col.addWidget(self.lbl_hotkey_hint)

        stats_col = QVBoxLayout()
        stats_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_clicks = QLabel("Session Actions: 0")
        self.lbl_clicks.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_clicks.setStyleSheet("font-size: 13px; font-weight: bold; color: #e6e9f0; border: none;")
        self.lbl_live_cps = QLabel("Live Rate: 0.0 / sec")
        self.lbl_live_cps.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_live_cps.setStyleSheet("font-size: 12px; color: #9aa0ac; border: none;")
        stats_col.addWidget(self.lbl_clicks)
        stats_col.addWidget(self.lbl_live_cps)

        banner_layout.addLayout(status_col, stretch=3)
        banner_layout.addLayout(stats_col, stretch=2)
        root_layout.addWidget(self.status_banner)

        # 1. App / Game Presets Group
        grp_presets = QGroupBox("App / Game Presets")
        preset_layout = QHBoxLayout(grp_presets)
        preset_layout.setSpacing(8)

        preset_layout.addWidget(QLabel("Preset:"))
        self.cmb_presets = QComboBox()
        self.cmb_presets.setToolTip("Select a saved configuration profile for a specific game or app.")
        preset_layout.addWidget(self.cmb_presets, stretch=1)

        self.btn_new_preset = QPushButton("+ New / Save As...")
        self.btn_new_preset.setToolTip("Save current settings as a new named Game/App preset.")
        preset_layout.addWidget(self.btn_new_preset)

        self.btn_rename_preset = QPushButton("Rename")
        self.btn_rename_preset.setToolTip("Rename the currently active preset.")
        preset_layout.addWidget(self.btn_rename_preset)

        self.btn_delete_preset = QPushButton("Delete")
        self.btn_delete_preset.setToolTip("Delete the currently selected preset.")
        preset_layout.addWidget(self.btn_delete_preset)

        root_layout.addWidget(grp_presets)

        # 2. Target Action Group (Mouse vs Keyboard)
        grp_action = QGroupBox("1. Target Input (Mouse Click or Keyboard Key)")
        action_layout = QVBoxLayout(grp_action)

        mode_row = QHBoxLayout()
        self.rb_mouse = QRadioButton("Mouse Click")
        self.rb_keyboard = QRadioButton("Keyboard Key Auto-Presser")
        mode_row.addWidget(self.rb_mouse)
        mode_row.addWidget(self.rb_keyboard)
        mode_row.addStretch()
        action_layout.addLayout(mode_row)

        # Mouse controls row
        self.mouse_row_widget = QWidget()
        mouse_row = QHBoxLayout(self.mouse_row_widget)
        mouse_row.setContentsMargins(0, 4, 0, 0)
        mouse_row.addWidget(QLabel("Mouse Button:"))
        self.cmb_mouse_btn = QComboBox()
        for label, code in MOUSE_BUTTONS:
            self.cmb_mouse_btn.addItem(label, code)
        mouse_row.addWidget(self.cmb_mouse_btn, stretch=1)
        self.chk_double_click = QCheckBox("Double-Click per cycle")
        mouse_row.addWidget(self.chk_double_click)
        action_layout.addWidget(self.mouse_row_widget)

        # Keyboard controls row
        self.kbd_row_widget = QWidget()
        kbd_row = QHBoxLayout(self.kbd_row_widget)
        kbd_row.setContentsMargins(0, 4, 0, 0)
        kbd_row.addWidget(QLabel("Target Key:"))
        self.cmb_kbd_key = QComboBox()
        for label, code in COMMON_KEYBOARD_KEYS:
            self.cmb_kbd_key.addItem(label, code)
        kbd_row.addWidget(self.cmb_kbd_key, stretch=1)
        self.btn_bind_action_key = QPushButton("Record Any Key...")
        kbd_row.addWidget(self.btn_bind_action_key)
        action_layout.addWidget(self.kbd_row_widget)

        root_layout.addWidget(grp_action)

        # 3. Click Speed & Timing Jitter Group
        grp_speed = QGroupBox("2. Speed, Interval & Humanizer Jitter")
        speed_layout = QGridLayout(grp_speed)
        speed_layout.setVerticalSpacing(8)
        speed_layout.setHorizontalSpacing(12)

        speed_layout.addWidget(QLabel("Rate (CPS / sec):"), 0, 0)
        self.spin_cps = QDoubleSpinBox()
        self.spin_cps.setRange(0.05, 500.0)
        self.spin_cps.setDecimals(2)
        self.spin_cps.setSingleStep(1.0)
        self.spin_cps.setSuffix(" CPS")
        speed_layout.addWidget(self.spin_cps, 0, 1)

        speed_layout.addWidget(QLabel("Interval (ms):"), 0, 2)
        self.spin_interval_ms = QDoubleSpinBox()
        self.spin_interval_ms.setRange(2.0, 60000.0)
        self.spin_interval_ms.setDecimals(1)
        self.spin_interval_ms.setSingleStep(10.0)
        self.spin_interval_ms.setSuffix(" ms")
        speed_layout.addWidget(self.spin_interval_ms, 0, 3)

        speed_layout.addWidget(QLabel("Press Hold Time:"), 1, 0)
        self.spin_hold_ms = QDoubleSpinBox()
        self.spin_hold_ms.setRange(1.0, 5000.0)
        self.spin_hold_ms.setDecimals(1)
        self.spin_hold_ms.setSingleStep(5.0)
        self.spin_hold_ms.setSuffix(" ms")
        self.spin_hold_ms.setToolTip(
            "How long the button/key stays held down during each click.\n"
            "15 ms (~1 frame at 60 FPS) ensures games reliably register every press."
        )
        speed_layout.addWidget(self.spin_hold_ms, 1, 1)

        self.chk_jitter = QCheckBox("Random Jitter (± ms):")
        self.chk_jitter.setToolTip("Adds random human-like timing variation to each click cycle.")
        speed_layout.addWidget(self.chk_jitter, 1, 2)

        self.spin_jitter_ms = QDoubleSpinBox()
        self.spin_jitter_ms.setRange(0.5, 5000.0)
        self.spin_jitter_ms.setDecimals(1)
        self.spin_jitter_ms.setSingleStep(5.0)
        self.spin_jitter_ms.setSuffix(" ms")
        speed_layout.addWidget(self.spin_jitter_ms, 1, 3)

        self.lbl_effective_timing = QLabel("")
        self.lbl_effective_timing.setStyleSheet("color: #9aa0ac; font-size: 11px;")
        speed_layout.addWidget(self.lbl_effective_timing, 2, 0, 1, 4)

        root_layout.addWidget(grp_speed)

        # 4. App / Game Window Whitelist & Target Lock Group
        grp_whitelist = QGroupBox("3. Target Window Whitelist & App Lock")
        wl_layout = QVBoxLayout(grp_whitelist)
        wl_layout.setSpacing(8)

        self.chk_window_lock = QCheckBox("Lock auto-clicks to Whitelisted App / Window (Prevent clicking other apps)")
        self.chk_window_lock.setToolTip(
            "When enabled, WayClick monitors the active window and will ONLY click if the focused window\n"
            "matches the whitelist. If you switch windows, alt-tab, or try to close the app, clicks\n"
            "automatically pause or stop."
        )
        wl_layout.addWidget(self.chk_window_lock)

        self.wl_container = QWidget()
        wl_sub_layout = QGridLayout(self.wl_container)
        wl_sub_layout.setContentsMargins(0, 2, 0, 0)
        wl_sub_layout.setVerticalSpacing(8)
        wl_sub_layout.setHorizontalSpacing(10)

        wl_sub_layout.addWidget(QLabel("Target App/Title:"), 0, 0)
        self.txt_whitelist = QLineEdit()
        self.txt_whitelist.setPlaceholderText("e.g. steam_app_977400, Cell to Singularity, Stardew Valley")
        self.txt_whitelist.setToolTip(
            "Comma-separated list of window titles or application class names (case-insensitive substring match).\n"
            "Example: 'steam_app_977400' or 'Cell to Singularity'."
        )
        wl_sub_layout.addWidget(self.txt_whitelist, 0, 1)

        self.btn_use_detected = QPushButton("⚡ Use Detected App")
        self.btn_use_detected.setToolTip("Instantly populate whitelist with the currently detected active game or app.")
        wl_sub_layout.addWidget(self.btn_use_detected, 0, 2)

        self.btn_capture_window = QPushButton("🎯 Capture in 3s...")
        self.btn_capture_window.setToolTip(
            "Start a 3-second countdown so you can switch to your target game window to capture its title & class."
        )
        wl_sub_layout.addWidget(self.btn_capture_window, 0, 3)

        wl_sub_layout.addWidget(QLabel("When Unfocused:"), 1, 0)
        self.cmb_lock_mode = QComboBox()
        self.cmb_lock_mode.addItem("❚❚ Pause clicks (auto-resume when target re-focused)", "pause")
        self.cmb_lock_mode.addItem("■ Stop clicker completely", "stop")
        self.cmb_lock_mode.setToolTip(
            "Choose whether to temporarily suspend clicks while you are on another window,\n"
            "or stop the auto-clicker completely when the target loses focus."
        )
        wl_sub_layout.addWidget(self.cmb_lock_mode, 1, 1)

        self.lbl_active_window = QLabel("Active Window: None")
        self.lbl_active_window.setStyleSheet(
            "color: #9aa0ac; font-size: 11px; background-color: #12141a; border: 1px solid #2b2f3a; "
            "border-radius: 4px; padding: 4px 8px;"
        )
        self.lbl_active_window.setToolTip("Real-time active window detected on your desktop.")
        wl_sub_layout.addWidget(self.lbl_active_window, 1, 2, 1, 2)

        wl_layout.addWidget(self.wl_container)
        root_layout.addWidget(grp_whitelist)

        # 5. Global Hotkey & Activation Mode Group
        grp_trigger = QGroupBox("4. Global Hotkey & Activation Mode")
        trigger_layout = QGridLayout(grp_trigger)
        trigger_layout.setVerticalSpacing(8)
        trigger_layout.setHorizontalSpacing(12)

        trigger_layout.addWidget(QLabel("Global Hotkey:"), 0, 0)
        self.lbl_hotkey_value = QLabel()
        self.lbl_hotkey_value.setStyleSheet(
            "background-color: #12141a; border: 1px solid #353a47; border-radius: 5px; "
            "padding: 5px 10px; font-weight: bold; color: #f6c177;"
        )
        trigger_layout.addWidget(self.lbl_hotkey_value, 0, 1, 1, 2)

        self.btn_bind_hotkey = QPushButton("Bind Key / Mouse Btn...")
        trigger_layout.addWidget(self.btn_bind_hotkey, 0, 3)

        trigger_layout.addWidget(QLabel("Activation Mode:"), 1, 0)
        self.cmb_trigger_mode = QComboBox()
        self.cmb_trigger_mode.addItem("Toggle (Press once to Start, again to Stop)", "toggle")
        self.cmb_trigger_mode.addItem("Hold (Rapid-fire only while hotkey is held down)", "hold")
        trigger_layout.addWidget(self.cmb_trigger_mode, 1, 1, 1, 3)

        root_layout.addWidget(grp_trigger)

        # 6. Stop Conditions & Options Group
        grp_limits = QGroupBox("5. Stop Conditions & Window Options")
        limits_layout = QGridLayout(grp_limits)
        limits_layout.setVerticalSpacing(8)
        limits_layout.setHorizontalSpacing(12)

        limits_layout.addWidget(QLabel("Stop Condition:"), 0, 0)
        self.cmb_limit_mode = QComboBox()
        self.cmb_limit_mode.addItem("Repeat Until Stopped", "infinite")
        self.cmb_limit_mode.addItem("Stop After N Actions", "count")
        self.cmb_limit_mode.addItem("Stop After Duration (sec)", "duration")
        limits_layout.addWidget(self.cmb_limit_mode, 0, 1)

        self.spin_max_clicks = QSpinBox()
        self.spin_max_clicks.setRange(1, 10_000_000)
        self.spin_max_clicks.setSingleStep(100)
        self.spin_max_clicks.setSuffix(" actions")
        limits_layout.addWidget(self.spin_max_clicks, 0, 2, 1, 2)

        self.spin_max_duration = QDoubleSpinBox()
        self.spin_max_duration.setRange(0.5, 86400.0)
        self.spin_max_duration.setDecimals(1)
        self.spin_max_duration.setSingleStep(10.0)
        self.spin_max_duration.setSuffix(" sec")
        limits_layout.addWidget(self.spin_max_duration, 0, 2, 1, 2)

        self.chk_stop_on_real_click = QCheckBox(
            "Stop immediately on a Real Physical Mouse Click (LMB / RMB / MMB)"
        )
        self.chk_stop_on_real_click.setToolTip(
            "Because WayClick reads physical /dev/input/event* hardware nodes directly\n"
            "and ignores its own virtual uinput device, it can tell a real physical mouse click\n"
            "apart from synthetic auto-clicks with 100% accuracy."
        )
        limits_layout.addWidget(self.chk_stop_on_real_click, 1, 0, 1, 4)

        self.chk_always_on_top = QCheckBox("Keep window Always on Top")
        limits_layout.addWidget(self.chk_always_on_top, 2, 0, 1, 2)

        limits_layout.addWidget(QLabel("GUI Start Button Delay:"), 2, 2)
        self.spin_gui_delay = QDoubleSpinBox()
        self.spin_gui_delay.setRange(0.0, 30.0)
        self.spin_gui_delay.setDecimals(1)
        self.spin_gui_delay.setSingleStep(0.5)
        self.spin_gui_delay.setSuffix(" s")
        self.spin_gui_delay.setToolTip(
            "Countdown delay (with cursor circle ring) when clicking the GUI Start button\n"
            "so you have time to move your cursor onto the game/target window."
        )
        limits_layout.addWidget(self.spin_gui_delay, 2, 3)

        root_layout.addWidget(grp_limits)

        # 7. Bottom Action Button
        self.btn_start_stop = QPushButton("▶  START AUTO CLICKER")
        self.btn_start_stop.setMinimumHeight(44)
        self._style_start_button(False)
        root_layout.addWidget(self.btn_start_stop)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready — Kernel uinput/evdev active (Wayland & Proton compatible)")

    def _style_start_button(self, active: bool) -> None:
        if active:
            self.btn_start_stop.setText("■  STOP AUTO CLICKER")
            self.btn_start_stop.setStyleSheet(
                """
                QPushButton {
                    background-color: #b93848;
                    border: 1px solid #e05263;
                    border-radius: 7px;
                    font-size: 14px;
                    font-weight: bold;
                    color: #ffffff;
                }
                QPushButton:hover {
                    background-color: #d14354;
                }
                """
            )
        else:
            self.btn_start_stop.setText("▶  START AUTO CLICKER")
            self.btn_start_stop.setStyleSheet(
                """
                QPushButton {
                    background-color: #2b7a4b;
                    border: 1px solid #3ea366;
                    border-radius: 7px;
                    font-size: 14px;
                    font-weight: bold;
                    color: #ffffff;
                }
                QPushButton:hover {
                    background-color: #348f59;
                }
                """
            )

    def _setup_tray_icon(self) -> None:
        self.tray_icon = QSystemTrayIcon(build_app_icon(active=False), self)
        self.tray_icon.setToolTip("WayClick — IDLE (Standby)")

        self.tray_menu = QMenu(self)
        self.tray_status_action = QAction("● Status: IDLE (Standby)", self)
        self.tray_status_action.setEnabled(False)
        self.tray_menu.addAction(self.tray_status_action)
        self.tray_menu.addSeparator()

        self.tray_toggle_action = QAction("▶ Start Auto Clicker", self)
        self.tray_toggle_action.triggered.connect(lambda: self.engine.toggle(from_gui=False))
        self.tray_menu.addAction(self.tray_toggle_action)

        self.tray_presets_menu = QMenu("Switch Game / App Preset", self)
        self.tray_menu.addMenu(self.tray_presets_menu)
        self.tray_menu.addSeparator()

        self.tray_show_action = QAction("Show / Hide Window", self)
        self.tray_show_action.triggered.connect(self._toggle_window_visibility)
        self.tray_menu.addAction(self.tray_show_action)

        self.tray_quit_action = QAction("Quit WayClick", self)
        self.tray_quit_action.triggered.connect(self.close)
        self.tray_menu.addAction(self.tray_quit_action)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _toggle_window_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visibility()

    def _refresh_tray_presets_menu(self) -> None:
        self.tray_presets_menu.clear()
        group = QActionGroup(self.tray_presets_menu)
        group.setExclusive(True)
        for name in self.store.presets.keys():
            act = QAction(name, self.tray_presets_menu)
            act.setCheckable(True)
            act.setChecked(name == self.store.active_name)
            act.triggered.connect(lambda checked, n=name: self.cmb_presets.setCurrentText(n))
            group.addAction(act)
            self.tray_presets_menu.addAction(act)

    def _refresh_preset_combo(self) -> None:
        self._loading_preset_ui = True
        self.cmb_presets.clear()
        for name in self.store.presets.keys():
            self.cmb_presets.addItem(name)
        idx = self.cmb_presets.findText(self.store.active_name)
        if idx >= 0:
            self.cmb_presets.setCurrentIndex(idx)
        self.btn_delete_preset.setEnabled(len(self.store.presets) > 1)
        self._refresh_tray_presets_menu()
        self._loading_preset_ui = False

    def _load_config_into_ui(self) -> None:
        self._loading_preset_ui = True
        cfg = self.config
        if cfg.action_type == "keyboard":
            self.rb_keyboard.setChecked(True)
        else:
            self.rb_mouse.setChecked(True)

        idx = self.cmb_mouse_btn.findData(cfg.mouse_button_code)
        if idx >= 0:
            self.cmb_mouse_btn.setCurrentIndex(idx)
        self.chk_double_click.setChecked(cfg.double_click)

        self._ensure_kbd_combo_has_code(cfg.keyboard_key_code)

        self._syncing_speed_inputs = True
        self.spin_interval_ms.setValue(max(2.0, cfg.interval_ms))
        self.spin_cps.setValue(round(1000.0 / max(2.0, cfg.interval_ms), 2))
        self._syncing_speed_inputs = False

        self.spin_hold_ms.setValue(cfg.hold_duration_ms)
        self.chk_jitter.setChecked(cfg.jitter_enabled)
        self.spin_jitter_ms.setValue(cfg.jitter_ms)
        self.spin_jitter_ms.setEnabled(cfg.jitter_enabled)

        # Window lock & whitelist
        self.chk_window_lock.setChecked(cfg.window_lock_enabled)
        self.txt_whitelist.setText(cfg.window_whitelist)
        w_idx = self.cmb_lock_mode.findData(cfg.window_lock_mode)
        if w_idx >= 0:
            self.cmb_lock_mode.setCurrentIndex(w_idx)
        self.wl_container.setEnabled(cfg.window_lock_enabled)

        self._refresh_hotkey_labels()
        t_idx = self.cmb_trigger_mode.findData(cfg.trigger_mode)
        if t_idx >= 0:
            self.cmb_trigger_mode.setCurrentIndex(t_idx)

        l_idx = self.cmb_limit_mode.findData(cfg.limit_mode)
        if l_idx >= 0:
            self.cmb_limit_mode.setCurrentIndex(l_idx)
        self.spin_max_clicks.setValue(cfg.max_clicks)
        self.spin_max_duration.setValue(cfg.max_duration_sec)
        self.chk_stop_on_real_click.setChecked(cfg.stop_on_real_click)
        self.chk_always_on_top.setChecked(cfg.always_on_top)
        self.spin_gui_delay.setValue(cfg.gui_start_delay_sec)

        self._update_action_mode_visibility()
        self._update_limit_visibility()
        self._update_effective_timing_label()
        self._loading_preset_ui = False

    def _ensure_kbd_combo_has_code(self, code: int) -> None:
        idx = self.cmb_kbd_key.findData(code)
        if idx < 0:
            self.cmb_kbd_key.addItem(format_ecode_name(code), code)
            idx = self.cmb_kbd_key.count() - 1
        self.cmb_kbd_key.setCurrentIndex(idx)

    def _refresh_hotkey_labels(self) -> None:
        hk_name = format_ecode_name(self.config.hotkey_code)
        self.lbl_hotkey_value.setText(hk_name)
        mode_desc = (
            "Hold down" if self.config.trigger_mode == "hold" else "Press"
        )
        self.lbl_hotkey_hint.setText(
            f"{mode_desc} [{hk_name}] to trigger  •  [ESC] or Real Click to emergency-stop"
        )

    def _connect_signals(self) -> None:
        self.cmb_presets.currentTextChanged.connect(self._on_preset_selected)
        self.btn_new_preset.clicked.connect(self._on_new_preset_clicked)
        self.btn_rename_preset.clicked.connect(self._on_rename_preset_clicked)
        self.btn_delete_preset.clicked.connect(self._on_delete_preset_clicked)

        self.rb_mouse.toggled.connect(self._on_ui_changed)
        self.rb_keyboard.toggled.connect(self._on_ui_changed)
        self.cmb_mouse_btn.currentIndexChanged.connect(self._on_ui_changed)
        self.chk_double_click.toggled.connect(self._on_ui_changed)
        self.cmb_kbd_key.currentIndexChanged.connect(self._on_ui_changed)

        self.spin_cps.valueChanged.connect(self._on_cps_changed)
        self.spin_interval_ms.valueChanged.connect(self._on_interval_changed)
        self.spin_hold_ms.valueChanged.connect(self._on_ui_changed)
        self.chk_jitter.toggled.connect(self._on_ui_changed)
        self.spin_jitter_ms.valueChanged.connect(self._on_ui_changed)

        # Window lock & whitelist signals
        self.chk_window_lock.toggled.connect(self._on_ui_changed)
        self.txt_whitelist.textChanged.connect(self._on_ui_changed)
        self.cmb_lock_mode.currentIndexChanged.connect(self._on_ui_changed)
        self.btn_use_detected.clicked.connect(self._on_use_detected_clicked)
        self.btn_capture_window.clicked.connect(self._on_capture_window_clicked)

        self.cmb_trigger_mode.currentIndexChanged.connect(self._on_ui_changed)
        self.cmb_limit_mode.currentIndexChanged.connect(self._on_ui_changed)
        self.spin_max_clicks.valueChanged.connect(self._on_ui_changed)
        self.spin_max_duration.valueChanged.connect(self._on_ui_changed)
        self.chk_stop_on_real_click.toggled.connect(self._on_ui_changed)
        self.spin_gui_delay.valueChanged.connect(self._on_ui_changed)
        self.chk_always_on_top.toggled.connect(self._on_always_on_top_toggled)

        self.btn_bind_hotkey.clicked.connect(self._on_bind_hotkey_clicked)
        self.btn_bind_action_key.clicked.connect(self._on_bind_action_key_clicked)
        self.btn_start_stop.clicked.connect(self._on_gui_start_stop_clicked)

        # Engine signals (emitted from Python worker thread -> QueuedConnection to GUI thread)
        qconn = Qt.ConnectionType.QueuedConnection
        self.engine.state_changed.connect(self._on_engine_state_changed, qconn)
        self.engine.stats_updated.connect(self._on_stats_updated, qconn)
        self.engine.status_message.connect(self.status_bar.showMessage, qconn)
        self.engine.countdown_started.connect(self.countdown_overlay.start_countdown, qconn)
        self.engine.countdown_finished.connect(self.countdown_overlay.stop_countdown, qconn)
        self.engine.pause_state_changed.connect(self._on_engine_pause_state_changed, qconn)

        # Window tracker signal
        self.window_tracker.window_changed.connect(self._on_window_changed, qconn)

        # Hardware monitor signals (emitted from Python evdev thread -> QueuedConnection)
        self.monitor.hotkey_pressed.connect(self._on_hardware_hotkey_pressed, qconn)
        self.monitor.hotkey_released.connect(self._on_hardware_hotkey_released, qconn)
        self.monitor.emergency_stop.connect(self._on_emergency_stop, qconn)
        self.monitor.real_click_detected.connect(self._on_real_click_detected, qconn)
        self.monitor.key_captured.connect(self._on_key_captured, qconn)

    # -----------------------------------------------------------------------
    # Window Whitelist & Detection Handlers
    # -----------------------------------------------------------------------

    def _on_window_changed(self, caption: str, resource_class: str, pid: int) -> None:
        display_text = f"Active: {resource_class or 'Unknown'}"
        if caption:
            short_cap = (caption[:26] + "..") if len(caption) > 28 else caption
            display_text += f" ({short_cap})"
        self.lbl_active_window.setText(display_text)

    def _on_use_detected_clicked(self) -> None:
        cls = self.window_tracker.last_external_class or self.window_tracker.active_class
        cap = self.window_tracker.last_external_caption or self.window_tracker.active_caption
        target = cls or cap
        if target:
            current = self.txt_whitelist.text().strip()
            if not current:
                self.txt_whitelist.setText(target)
            elif target not in current:
                self.txt_whitelist.setText(f"{current}, {target}")
            self.chk_window_lock.setChecked(True)
            self._on_ui_changed()
            self.status_bar.showMessage(f"Set target whitelist to: {target}")
        else:
            self.status_bar.showMessage("No game/app detected yet. Switch to your game first!")

    def _on_capture_window_clicked(self) -> None:
        self._capture_countdown_remaining = 3
        self.btn_capture_window.setEnabled(False)
        self.btn_capture_window.setText("Capturing in 3s...")
        self.status_bar.showMessage("Switch to your target game/app window in 3 seconds...")

        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(1000)
        self._capture_timer.timeout.connect(self._on_capture_tick)
        self._capture_timer.start()

    def _on_capture_tick(self) -> None:
        self._capture_countdown_remaining -= 1
        if self._capture_countdown_remaining > 0:
            self.btn_capture_window.setText(f"Capturing in {self._capture_countdown_remaining}s...")
            self.status_bar.showMessage(
                f"Switch to your target game/app window in {self._capture_countdown_remaining} seconds..."
            )
        else:
            if self._capture_timer:
                self._capture_timer.stop()
            self.btn_capture_window.setEnabled(True)
            self.btn_capture_window.setText("🎯 Capture in 3s...")

            cls = self.window_tracker.active_class or self.window_tracker.last_external_class
            cap = self.window_tracker.active_caption or self.window_tracker.last_external_caption
            target = cls or cap
            if target:
                self.txt_whitelist.setText(target)
                self.chk_window_lock.setChecked(True)
                self._on_ui_changed()
                self.status_bar.showMessage(f"Captured target window: {cls} ({cap})!")
            else:
                self.status_bar.showMessage("Could not detect active window.")

    # -----------------------------------------------------------------------
    # Preset Management Handlers
    # -----------------------------------------------------------------------

    def _on_preset_selected(self, name: str) -> None:
        if self._loading_preset_ui or not name or name not in self.store.presets:
            return
        self.engine.stop()
        self.store.active_name = name
        self.config = self.store.active_config
        self._load_config_into_ui()
        self.engine.update_config(self.config)
        self.monitor.update_config(self.config)
        self._apply_always_on_top(self.config.always_on_top)
        self._refresh_tray_presets_menu()
        self.store.save()
        self.status_bar.showMessage(f"Loaded preset: {name}")

    def _on_new_preset_clicked(self) -> None:
        self.engine.stop()
        name, ok = QInputDialog.getText(
            self,
            "Save Game / App Preset",
            "Enter a name for this preset (e.g. Stardew Valley, Minecraft, ETS2):",
        )
        name = name.strip()
        if not ok or not name:
            return
        self.store.presets[name] = ClickerConfig.from_dict(asdict(self.config))
        self.store.active_name = name
        self.config = self.store.active_config
        self.store.save()
        self._refresh_preset_combo()
        self.status_bar.showMessage(f"Saved preset: {name}")

    def _on_rename_preset_clicked(self) -> None:
        self.engine.stop()
        old_name = self.store.active_name
        new_name, ok = QInputDialog.getText(
            self,
            "Rename Preset",
            "New name for preset:",
            text=old_name,
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return
        rebuilt: Dict[str, ClickerConfig] = {}
        for k, v in self.store.presets.items():
            if k == old_name:
                rebuilt[new_name] = v
            else:
                rebuilt[k] = v
        self.store.presets = rebuilt
        self.store.active_name = new_name
        self.config = self.store.active_config
        self.store.save()
        self._refresh_preset_combo()
        self.status_bar.showMessage(f"Renamed preset to: {new_name}")

    def _on_delete_preset_clicked(self) -> None:
        if len(self.store.presets) <= 1:
            return
        self.engine.stop()
        target = self.store.active_name
        reply = QMessageBox.question(
            self,
            "Delete Preset",
            f"Delete preset '{target}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.store.presets.pop(target, None)
        self.store.active_name = next(iter(self.store.presets.keys()))
        self.config = self.store.active_config
        self.store.save()
        self._refresh_preset_combo()
        self._load_config_into_ui()
        self.engine.update_config(self.config)
        self.monitor.update_config(self.config)
        self.status_bar.showMessage(f"Deleted preset: {target}")

    # -----------------------------------------------------------------------
    # UI & Engine Handlers
    # -----------------------------------------------------------------------

    def _on_cps_changed(self, cps: float) -> None:
        if self._syncing_speed_inputs or self._loading_preset_ui:
            return
        self._syncing_speed_inputs = True
        interval = max(2.0, 1000.0 / max(0.05, cps))
        self.spin_interval_ms.setValue(round(interval, 1))
        self._syncing_speed_inputs = False
        self._on_ui_changed()

    def _on_interval_changed(self, interval_ms: float) -> None:
        if self._syncing_speed_inputs or self._loading_preset_ui:
            return
        self._syncing_speed_inputs = True
        cps = 1000.0 / max(2.0, interval_ms)
        self.spin_cps.setValue(round(cps, 2))
        self._syncing_speed_inputs = False
        self._on_ui_changed()

    def _update_action_mode_visibility(self) -> None:
        is_mouse = self.rb_mouse.isChecked()
        self.mouse_row_widget.setVisible(is_mouse)
        self.kbd_row_widget.setVisible(not is_mouse)

    def _update_limit_visibility(self) -> None:
        mode = self.cmb_limit_mode.currentData()
        self.spin_max_clicks.setVisible(mode == "count")
        self.spin_max_duration.setVisible(mode == "duration")

    def _update_effective_timing_label(self) -> None:
        base_ms = self.spin_interval_ms.value()
        cps = 1000.0 / max(2.0, base_ms)
        if self.chk_jitter.isChecked():
            j = self.spin_jitter_ms.value()
            low_ms = max(2.0, base_ms - j)
            high_ms = base_ms + j
            low_cps = 1000.0 / high_ms
            high_cps = 1000.0 / low_ms
            self.lbl_effective_timing.setText(
                f"Effective randomized cycle: {low_ms:.1f}–{high_ms:.1f} ms "
                f"({low_cps:.1f}–{high_cps:.1f} actions/sec)"
            )
        else:
            self.lbl_effective_timing.setText(
                f"Fixed cycle: {base_ms:.1f} ms ({cps:.2f} actions/sec), "
                f"Hold duration: {self.spin_hold_ms.value():.1f} ms"
            )

    def _on_ui_changed(self) -> None:
        if self._loading_preset_ui:
            return
        self._update_action_mode_visibility()
        self._update_limit_visibility()
        self.spin_jitter_ms.setEnabled(self.chk_jitter.isChecked())
        self.wl_container.setEnabled(self.chk_window_lock.isChecked())
        self._update_effective_timing_label()

        self.config.action_type = "mouse" if self.rb_mouse.isChecked() else "keyboard"
        self.config.mouse_button_code = int(self.cmb_mouse_btn.currentData())
        self.config.double_click = self.chk_double_click.isChecked()
        self.config.keyboard_key_code = int(self.cmb_kbd_key.currentData())

        self.config.interval_ms = float(self.spin_interval_ms.value())
        self.config.hold_duration_ms = float(self.spin_hold_ms.value())
        self.config.jitter_enabled = self.chk_jitter.isChecked()
        self.config.jitter_ms = float(self.spin_jitter_ms.value())

        # Window lock & whitelist settings
        self.config.window_lock_enabled = self.chk_window_lock.isChecked()
        self.config.window_whitelist = self.txt_whitelist.text().strip()
        self.config.window_lock_mode = str(self.cmb_lock_mode.currentData())

        self.config.trigger_mode = str(self.cmb_trigger_mode.currentData())
        self.config.limit_mode = str(self.cmb_limit_mode.currentData())
        self.config.max_clicks = int(self.spin_max_clicks.value())
        self.config.max_duration_sec = float(self.spin_max_duration.value())
        self.config.stop_on_real_click = self.chk_stop_on_real_click.isChecked()
        self.config.gui_start_delay_sec = float(self.spin_gui_delay.value())

        self._refresh_hotkey_labels()
        self.engine.update_config(self.config)
        self.monitor.update_config(self.config)
        self.store.save()

    def _on_always_on_top_toggled(self, checked: bool) -> None:
        if self._loading_preset_ui:
            return
        self.config.always_on_top = checked
        self.store.save()
        self._apply_always_on_top(checked)

    def _apply_always_on_top(self, enabled: bool) -> None:
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _on_bind_hotkey_clicked(self) -> None:
        self.engine.stop()
        self.btn_bind_hotkey.setText("Press any key/btn...")
        self.lbl_hotkey_value.setText("Listening for input...")
        self.status_bar.showMessage("Press any keyboard key or mouse side button to bind as Global Hotkey...")
        self.monitor.start_capture("hotkey")

    def _on_bind_action_key_clicked(self) -> None:
        self.engine.stop()
        self.btn_bind_action_key.setText("Press any key...")
        self.status_bar.showMessage("Press any keyboard key to set as the auto-pressed target key...")
        self.monitor.start_capture("action_key")

    def _on_key_captured(self, code: int, purpose: str) -> None:
        if purpose == "hotkey":
            self.config.hotkey_code = code
            self.btn_bind_hotkey.setText("Bind Key / Mouse Btn...")
            self._refresh_hotkey_labels()
            self.monitor.update_config(self.config)
            self.store.save()
            self.status_bar.showMessage(f"Bound Global Hotkey to {format_ecode_name(code)}")
        elif purpose == "action_key":
            self.config.keyboard_key_code = code
            self.btn_bind_action_key.setText("Record Any Key...")
            self._ensure_kbd_combo_has_code(code)
            self.engine.update_config(self.config)
            self.store.save()
            self.status_bar.showMessage(f"Bound Target Auto-Press Key to {format_ecode_name(code)}")

    def _on_gui_start_stop_clicked(self) -> None:
        now = time.monotonic()
        if (now - self.monitor.last_physical_lmb_ts) > 0.35:
            return
        if not self.engine.is_running and (
            (now - self._last_real_click_stop_ts) < 0.50
            or (now - self.engine.stopped_at) < 0.50
        ):
            return
        self.engine.toggle(from_gui=True)

    def _on_emergency_stop(self) -> None:
        if self.engine.is_running:
            self.engine.stop()
            self.status_bar.showMessage("Emergency Stop triggered via [ESC] key.")

    def _on_real_click_detected(self, code: int) -> None:
        if not self.engine.is_running:
            return
        if (time.monotonic() - self.engine.started_at) < 0.25:
            return
        self._last_real_click_stop_ts = time.monotonic()
        self.engine.stop()
        self.status_bar.showMessage(
            f"Stopped automatically: detected real physical click ({format_ecode_name(code)})"
        )

    def _on_hardware_hotkey_pressed(self) -> None:
        if self.config.trigger_mode == "toggle":
            self.engine.toggle(from_gui=False)
        else:
            self.engine.start(from_gui=False)

    def _on_hardware_hotkey_released(self) -> None:
        if self.config.trigger_mode == "hold":
            self.engine.stop()

    def _update_banner_status(self) -> None:
        if self.engine.is_running:
            if self.engine.is_paused:
                self.lbl_state.setText("❚❚ PAUSED (TARGET UNFOCUSED)")
                self.lbl_state.setStyleSheet("font-size: 16px; font-weight: bold; color: #f6c177; border: none;")
                self.status_banner.setStyleSheet(
                    "QFrame { background-color: #2b2519; border: 1px solid #f6c177; border-radius: 8px; padding: 8px; }"
                )
                self.tray_icon.setIcon(build_app_icon(active=True, paused=True))
                self.tray_icon.setToolTip(f"WayClick — PAUSED [{self.store.active_name}]")
                self.tray_status_action.setText(f"❚❚ Status: PAUSED ({self.store.active_name})")
            else:
                locked_tag = " (LOCKED TO TARGET)" if self.config.window_lock_enabled else ""
                self.lbl_state.setText(f"● ACTIVE — RUNNING{locked_tag}")
                self.lbl_state.setStyleSheet("font-size: 16px; font-weight: bold; color: #56d364; border: none;")
                self.status_banner.setStyleSheet(
                    "QFrame { background-color: #192b22; border: 1px solid #2ea043; border-radius: 8px; padding: 8px; }"
                )
                self.tray_icon.setIcon(build_app_icon(active=True, paused=False))
                self.tray_icon.setToolTip(f"WayClick — ACTIVE [{self.store.active_name}]")
                self.tray_status_action.setText(f"● Status: ACTIVE ({self.store.active_name})")
                self.tray_toggle_action.setText("■ Stop Auto Clicker")
        else:
            self.countdown_overlay.stop_countdown()
            self.lbl_state.setText("● IDLE (STANDBY)")
            self.lbl_state.setStyleSheet("font-size: 16px; font-weight: bold; color: #9aa0ac; border: none;")
            self.status_banner.setStyleSheet(
                "QFrame { background-color: #1f2430; border: 1px solid #32394a; border-radius: 8px; padding: 8px; }"
            )
            self.lbl_live_cps.setText("Live Rate: 0.0 / sec")
            self.tray_icon.setIcon(build_app_icon(active=False))
            self.tray_status_action.setText(f"● Status: IDLE ({self.store.active_name})")
            self.tray_toggle_action.setText("▶ Start Auto Clicker")
            self.tray_icon.setToolTip(f"WayClick — IDLE [{self.store.active_name}]")

    def _on_engine_state_changed(self, active: bool) -> None:
        self._style_start_button(active)
        self._update_banner_status()
        if active:
            target_desc = (
                format_ecode_name(self.config.mouse_button_code)
                if self.config.action_type == "mouse"
                else format_ecode_name(self.config.keyboard_key_code)
            )
            lock_desc = f" (Locked: {self.config.window_whitelist})" if self.config.window_lock_enabled else ""
            self.status_bar.showMessage(f"Active [{self.store.active_name}]: injecting {target_desc}{lock_desc}")

    def _on_engine_pause_state_changed(self, paused: bool) -> None:
        self._update_banner_status()

    def _on_stats_updated(self, total_clicks: int, inst_cps: float) -> None:
        self.lbl_clicks.setText(f"Session Actions: {total_clicks:,}")
        self.lbl_live_cps.setText(f"Live Rate: {inst_cps:.1f} / sec")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.tray_icon.hide()
        self.countdown_overlay.stop_countdown()
        self.window_tracker.cleanup()
        self.monitor.stop()
        self.engine.stop()
        time.sleep(0.05)
        self.vinput.close()
        self.store.save()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WayClick")
    app.setDesktopFileName("wayclick-autoclicker")
    app.setWindowIcon(build_app_icon(active=False))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
