#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鼠标录制回放器 (Mouse Recorder & Player)
========================================
F9  — 开始录制鼠标操作（移动、点击、滚轮）
F10 — 停止录制并弹出命名保存对话框

回放方式:
  1. AutoHotkey 脚本（优先，需安装 AHK）
  2. 内置 Python 引擎（ctypes SendInput，无需 AHK）

生成的 .ahk 脚本可独立运行，Esc 可中断回放。
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import subprocess
import shutil
import ctypes
import logging
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog, colorchooser

# ── 云端模块（可选） ─────────────────────────────────────────────
try:
    from cloud_config import CloudConfig
    from cloud_service import CloudService

    CLOUD_OK = True
except ImportError:
    CLOUD_OK = False

# ── 像素监测模块 ────────────────────────────────────────────────
try:
    from pixel_monitor import PixelMonitor, PixelRule

    PIXEL_OK = True
except ImportError:
    PIXEL_OK = False

# ── 状态机模块 ──────────────────────────────────────────────────
if PIXEL_OK:
    try:
        from state_machine import (
            StateMachineEngine, SMConfig, SMState, SMCondition, SMAction,
            SMTransition, SMCloudTrigger, SMCloudAction, JumpRule,
            load_all_jump_rules, save_jump_rule, delete_jump_rule,
        )
        SM_OK = True
    except ImportError:
        SM_OK = False
else:
    SM_OK = False

# ── 统一配置模块 ─────────────────────────────────────────────────
try:
    from config import AppConfig, TaskBranchConfig, PrintSettings
    CONFIG_OK = True
except ImportError:
    CONFIG_OK = False

# ── 平台检测 ───────────────────────────────────────────────────
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"

# ── Windows DPI 感知 ───────────────────────────────────────────
if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# ── 依赖检查 ───────────────────────────────────────────────────
try:
    from pynput import mouse, keyboard

    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False

# ── 常量 ───────────────────────────────────────────────────────
RECORDINGS_DIR = Path(__file__).parent / "recordings"
RULES_DIR = Path(__file__).parent / "sm_rules"
SAMPLE_INTERVAL = 0.02       # 50 fps 采样率
MOVE_THRESHOLD = 2           # 像素移动阈值
DEFAULT_START_DELAY = 3      # 回放前倒计时（秒）

# ── 跨平台字体/主题 ───────────────────────────────────────────
if IS_LINUX:
    FONT_BODY = ("DejaVu Sans", 10)
    FONT_SMALL = ("DejaVu Sans", 8)
    FONT_BOLD = ("DejaVu Sans", 10, "bold")
    FONT_TITLE = ("DejaVu Sans", 12, "bold")
    FONT_HERO = ("DejaVu Sans", 16, "bold")
    FONT_MONO = ("DejaVu Sans Mono", 9)
    FONT_MONO_SM = ("DejaVu Sans Mono", 8)
    FONT_ITALIC = ("DejaVu Sans", 9, "italic")
    FONT_STATUS = ("DejaVu Sans", 10)
    FONT_TINY = ("DejaVu Sans", 7)
else:
    FONT_BODY = ("Microsoft YaHei UI", 10)
    FONT_SMALL = ("Microsoft YaHei UI", 8)
    FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
    FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
    FONT_HERO = ("Microsoft YaHei UI", 16, "bold")
    FONT_MONO = ("Consolas", 9)
    FONT_MONO_SM = ("Consolas", 8)
    FONT_ITALIC = ("Microsoft YaHei UI", 9, "italic")
    FONT_STATUS = ("Microsoft YaHei UI", 10)
    FONT_TINY = ("Microsoft YaHei UI", 7)

# 颜色主题 — 蓝灰色系，干净专业
C_BG = "#F5F6FA"            # 页面背景
C_SURFACE = "#FFFFFF"        # 卡片/面板背景
C_PRIMARY = "#1976D2"        # 主色（按钮、强调）
C_PRIMARY_DARK = "#1565C0"   # 深主色
C_ACCENT = "#FF6F00"         # 强调色（录制中、警告）
C_SUCCESS = "#2E7D32"        # 成功/运行中
C_DANGER = "#C62828"         # 危险/停止
C_TEXT = "#212121"            # 主文字
C_TEXT_SEC = "#616161"       # 次要文字
C_BORDER = "#E0E0E0"         # 边框
C_LOG_BG = "#263238"         # 日志背景
C_LOG_FG = "#ECEFF1"         # 日志文字


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def find_autohotkey():
    """查找系统上的 AutoHotkey 可执行文件（仅 Windows）。"""
    if not IS_WINDOWS:
        return None
    candidates = [
        r"C:\Program Files\AutoHotkey\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\AutoHotkey.exe",
        r"C:\Program Files (x86)\AutoHotkey\AutoHotkey.exe",
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe",
        r"C:\Program Files\AutoHotkey\v2\AutoHotkey.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return shutil.which("AutoHotkey") or shutil.which("AutoHotkey64")


def generate_ahk_script(data):
    """将录制数据转换为 AutoHotkey v1 脚本。"""
    events = data["events"]
    speed = data.get("speed", 1.0)
    delay = data.get("start_delay", DEFAULT_START_DELAY)
    name = data.get("name", "Unknown")

    lines = [
        "#NoEnv",
        "#SingleInstance, Force",
        "SendMode, Input",
        "SetWorkingDir, %A_ScriptDir%",
        "CoordMode, Mouse, Screen",
        "",
        f"; ═══ 录制: {name} ═══",
        f"; 日期:   {data.get('created', '')}",
        f"; 时长:   {data.get('duration_ms', 0)} ms",
        f"; 事件数: {len(events)}",
        f"; 速度:   {speed}x",
        "",
        "Esc::ExitApp  ; 按 Esc 中断回放",
        "",
        f"Sleep, {int(delay * 1000)}  ; 回放前倒计时",
        "",
    ]

    prev_ms = 0
    for ev in events:
        dt = ev["time_ms"] - prev_ms
        prev_ms = ev["time_ms"]

        if speed != 1.0:
            dt = max(1, int(dt / speed))

        if dt > 0:
            lines.append(f"Sleep, {dt}")

        t = ev["type"]
        if t == "move":
            lines.append(f"MouseMove, {ev['x']}, {ev['y']}, 0")
        elif t == "press":
            btn = {"left": "Left", "right": "Right", "middle": "Middle"}.get(
                ev["button"], "Left"
            )
            lines.append(f"Click, Down {btn}")
        elif t == "release":
            btn = {"left": "Left", "right": "Right", "middle": "Middle"}.get(
                ev["button"], "Left"
            )
            lines.append(f"Click, Up {btn}")
        elif t == "scroll":
            dy = ev.get("dy", 0)
            direction = "WheelUp" if dy > 0 else "WheelDown"
            for _ in range(abs(dy)):
                lines.append(f"MouseClick, {direction}, , , 1, 0")
        elif t == "key_press":
            key = ev.get("key", "")
            lines.append(f"Send, {{{key} down}}")
        elif t == "key_release":
            key = ev.get("key", "")
            lines.append(f"Send, {{{key} up}}")

    lines.append("ExitApp")
    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════════════════════════════
# 录制引擎
# ══════════════════════════════════════════════════════════════════

class RecordingSession:
    """管理一次录制会话的鼠标+键盘事件采集。"""

    def __init__(self):
        self.events: list = []
        self._start_time: float | None = None
        self.recording: bool = False
        self.record_keyboard: bool = False
        self._lock = threading.Lock()
        self._last_x: int | None = None
        self._last_y: int | None = None
        self._last_sample: float = 0.0
        self._mouse_listener: mouse.Listener | None = None
        self._keyboard_listener: keyboard.Listener | None = None

    def start(self):
        """启动鼠标+键盘监听。"""
        self.events.clear()
        self._start_time = time.time()
        self.recording = True
        self._last_x = self._last_y = None
        self._last_sample = 0.0
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._mouse_listener.start()
        if self.record_keyboard and PYNPUT_OK:
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self._keyboard_listener.start()

    def stop(self):
        """停止所有监听。"""
        self.recording = False
        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None
        if self._keyboard_listener:
            self._keyboard_listener.stop()
            self._keyboard_listener = None

    def _elapsed_ms(self) -> int:
        return int((time.time() - self._start_time) * 1000) if self._start_time else 0

    def _on_move(self, x, y):
        if not self.recording:
            return True
        now = time.time()
        # 采样节流：间隔时间不够 且 移动距离不够 → 跳过
        if self._last_x is not None:
            dist = ((x - self._last_x) ** 2 + (y - self._last_y) ** 2) ** 0.5
            if dist < MOVE_THRESHOLD and (now - self._last_sample) < SAMPLE_INTERVAL:
                return True
        self._last_sample = now
        self._last_x, self._last_y = x, y
        with self._lock:
            self.events.append(
                {"type": "move", "x": int(x), "y": int(y), "time_ms": self._elapsed_ms()}
            )
        return True

    def _on_click(self, x, y, button, pressed):
        if not self.recording:
            return True
        btn_name = str(button).split(".")[-1].lower()
        with self._lock:
            self.events.append(
                {
                    "type": "press" if pressed else "release",
                    "button": btn_name,
                    "x": int(x),
                    "y": int(y),
                    "time_ms": self._elapsed_ms(),
                }
            )
        return True

    def _on_scroll(self, x, y, dx, dy):
        if not self.recording:
            return True
        with self._lock:
            self.events.append(
                {
                    "type": "scroll",
                    "x": int(x),
                    "y": int(y),
                    "dx": int(dx),
                    "dy": int(dy),
                    "time_ms": self._elapsed_ms(),
                }
            )
        return True

    def _on_key_press(self, key):
        if not self.recording:
            return True
        key_name = self._key_to_name(key)
        if key_name is None:
            return True
        with self._lock:
            self.events.append({
                "type": "key_press",
                "key": key_name,
                "time_ms": self._elapsed_ms(),
            })
        return True

    def _on_key_release(self, key):
        if not self.recording:
            return True
        key_name = self._key_to_name(key)
        if key_name is None:
            return True
        with self._lock:
            self.events.append({
                "type": "key_release",
                "key": key_name,
                "time_ms": self._elapsed_ms(),
            })
        return True

    @staticmethod
    def _key_to_name(key) -> str | None:
        """pynput key → AHK 兼容的键名。"""
        try:
            # 普通字符键
            if hasattr(key, 'char') and key.char is not None:
                c = key.char
                # 特殊字符映射
                special = {
                    '\t': 'Tab', '\n': 'Enter', '\r': 'Enter',
                    ' ': 'Space', '!': '{!}', '#': '{#}', '+': '{+}',
                    '^': '{^}', '%': '{%}', '~': '{~}', '{': '{{}',
                    '}': '{}}',
                }
                return special.get(c, c)
            # 特殊键
            name = str(key).split('.')[-1]
            name_map = {
                'alt_l': 'LAlt', 'alt_r': 'RAlt', 'alt_gr': 'RAlt',
                'ctrl_l': 'LCtrl', 'ctrl_r': 'RCtrl',
                'shift_l': 'LShift', 'shift_r': 'RShift',
                'cmd_l': 'LWin', 'cmd_r': 'RWin',
                'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
                'enter': 'Enter', 'return': 'Enter',
                'space': 'Space', 'tab': 'Tab', 'escape': 'Escape',
                'esc': 'Escape', 'backspace': 'Backspace', 'bs': 'Backspace',
                'delete': 'Delete', 'del': 'Delete', 'insert': 'Insert',
                'ins': 'Insert', 'home': 'Home', 'end': 'End',
                'page_up': 'PgUp', 'page_down': 'PgDn',
                'caps_lock': 'CapsLock', 'num_lock': 'NumLock',
                'scroll_lock': 'ScrollLock', 'print_screen': 'PrintScreen',
                'pause': 'Pause', 'menu': 'AppsKey',
                'f1': 'F1', 'f2': 'F2', 'f3': 'F3', 'f4': 'F4',
                'f5': 'F5', 'f6': 'F6', 'f7': 'F7', 'f8': 'F8',
                'f9': 'F9', 'f10': 'F10', 'f11': 'F11', 'f12': 'F12',
            }
            return name_map.get(name, name)
        except Exception:
            return None

    def get_data(self, name: str = "Unnamed") -> dict:
        """取出录制数据（线程安全）。"""
        with self._lock:
            events_copy = list(self.events)
        return {
            "name": name,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": self._elapsed_ms(),
            "event_count": len(events_copy),
            "events": events_copy,
        }

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self.events)


# ══════════════════════════════════════════════════════════════════
# 内置回放引擎（SendInput，无需 AutoHotkey）
# ══════════════════════════════════════════════════════════════════

# Win32 SendInput 常量和结构体
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

# 键盘输入常量
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

# 键名 → 虚拟键码 (VK) 映射
_KEY_TO_VK: dict[str, int] = {
    'Backspace': 0x08, 'Tab': 0x09, 'Enter': 0x0D,
    'Shift': 0x10, 'LShift': 0xA0, 'RShift': 0xA1,
    'Ctrl': 0x11, 'LCtrl': 0xA2, 'RCtrl': 0xA3,
    'Alt': 0x12, 'LAlt': 0xA4, 'RAlt': 0xA5,
    'Pause': 0x13, 'CapsLock': 0x14, 'Escape': 0x1B,
    'Space': 0x20, 'PageUp': 0x21, 'PgUp': 0x21, 'PageDown': 0x22, 'PgDn': 0x22,
    'End': 0x23, 'Home': 0x24, 'Left': 0x25, 'Up': 0x26, 'Right': 0x27, 'Down': 0x28,
    'PrintScreen': 0x2C, 'Insert': 0x2D, 'Ins': 0x2D, 'Delete': 0x2E, 'Del': 0x2E,
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
    '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
    'A': 0x41, 'B': 0x42, 'C': 0x43, 'D': 0x44, 'E': 0x45,
    'F': 0x46, 'G': 0x47, 'H': 0x48, 'I': 0x49, 'J': 0x4A,
    'K': 0x4B, 'L': 0x4C, 'M': 0x4D, 'N': 0x4E, 'O': 0x4F,
    'P': 0x50, 'Q': 0x51, 'R': 0x52, 'S': 0x53, 'T': 0x54,
    'U': 0x55, 'V': 0x56, 'W': 0x57, 'X': 0x58, 'Y': 0x59, 'Z': 0x5A,
    'LWin': 0x5B, 'RWin': 0x5C, 'AppsKey': 0x5D,
    'NumPad0': 0x60, 'NumPad1': 0x61, 'NumPad2': 0x62, 'NumPad3': 0x63,
    'NumPad4': 0x64, 'NumPad5': 0x65, 'NumPad6': 0x66, 'NumPad7': 0x67,
    'NumPad8': 0x68, 'NumPad9': 0x69,
    'Multiply': 0x6A, 'Add': 0x6B, 'Subtract': 0x6D, 'Decimal': 0x6E, 'Divide': 0x6F,
    'F1': 0x70, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73,
    'F5': 0x74, 'F6': 0x75, 'F7': 0x76, 'F8': 0x77,
    'F9': 0x78, 'F10': 0x79, 'F11': 0x7A, 'F12': 0x7B,
    'NumLock': 0x90, 'ScrollLock': 0x91,
    ';': 0xBA, '=': 0xBB, ',': 0xBC, '-': 0xBD, '.': 0xBE, '/': 0xBF,
    '`': 0xC0, '[': 0xDB, '\\\\': 0xDC, ']': 0xDD, "'": 0xDE,
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUT_UNION),
    ]


def _send_mouse_input(flags: int, x: int = 0, y: int = 0, data: int = 0):
    """向系统发送单次鼠标输入事件。"""
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dx = x
    inp.u.mi.dy = y
    inp.u.mi.mouseData = data
    inp.u.mi.dwFlags = flags
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _send_key_input(vk: int, keyup: bool = False):
    """向系统发送单次键盘输入事件。"""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = 0
    inp.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = ctypes.POINTER(ctypes.c_ulong)()
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def playback_via_sendinput(data: dict, speed: float = 1.0, delay: float = DEFAULT_START_DELAY):
    """使用 SendInput 回放录制数据（内置引擎）。"""
    events = data["events"]
    if not events:
        return

    user32 = ctypes.windll.user32
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)

    # 回放前倒计时
    time.sleep(delay)

    BTN_FLAGS = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }

    prev_ms = 0
    for ev in events:
        dt = (ev["time_ms"] - prev_ms) / 1000.0  # 转换为秒
        prev_ms = ev["time_ms"]

        if speed != 1.0:
            dt = max(0.001, dt / speed)

        if dt > 0:
            time.sleep(dt)

        t = ev["type"]

        if t == "move":
            # 绝对坐标：映射到 [0, 65535]
            abs_x = int(ev["x"] * 65535 / max(1, screen_w))
            abs_y = int(ev["y"] * 65535 / max(1, screen_h))
            _send_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, abs_x, abs_y)

        elif t in ("press", "release"):
            btn = ev.get("button", "left")
            flags = BTN_FLAGS.get(btn, BTN_FLAGS["left"])
            flag = flags[0] if t == "press" else flags[1]
            _send_mouse_input(flag)

        elif t == "scroll":
            dy = ev.get("dy", 0)
            amount = int(dy * WHEEL_DELTA)
            _send_mouse_input(MOUSEEVENTF_WHEEL, data=amount)

        elif t in ("key_press", "key_release"):
            key = ev.get("key", "")
            vk = _KEY_TO_VK.get(key.upper(), 0)
            if vk == 0 and len(key) == 1:
                # 单字符 → 用其 ASCII 大写作为 VK
                vk = ord(key.upper())
            if vk:
                _send_key_input(vk, keyup=(t == "key_release"))


# ══════════════════════════════════════════════════════════════════
# Linux 回放引擎（xdotool）
# ══════════════════════════════════════════════════════════════════

def _find_xdotool() -> str | None:
    """查找 xdotool 可执行文件。"""
    return shutil.which("xdotool")


# 键名 → X11 keysym 名称映射
_KEY_TO_XKEYSYM: dict[str, str] = {
    'Backspace': 'BackSpace', 'Tab': 'Tab', 'Enter': 'Return',
    'Shift': 'Shift_L', 'LShift': 'Shift_L', 'RShift': 'Shift_R',
    'Ctrl': 'Control_L', 'LCtrl': 'Control_L', 'RCtrl': 'Control_R',
    'Alt': 'Alt_L', 'LAlt': 'Alt_L', 'RAlt': 'Alt_R',
    'Pause': 'Pause', 'CapsLock': 'Caps_Lock', 'Escape': 'Escape',
    'Space': 'space', 'PageUp': 'Page_Up', 'PgUp': 'Page_Up',
    'PageDown': 'Page_Down', 'PgDn': 'Page_Down',
    'End': 'End', 'Home': 'Home',
    'Left': 'Left', 'Up': 'Up', 'Right': 'Right', 'Down': 'Down',
    'PrintScreen': 'Print', 'Insert': 'Insert', 'Ins': 'Insert',
    'Delete': 'Delete', 'Del': 'Delete',
    'LWin': 'Super_L', 'RWin': 'Super_R', 'Super_L': 'Super_L', 'Super_R': 'Super_R',
    'NumLock': 'Num_Lock', 'ScrollLock': 'Scroll_Lock',
    'F1': 'F1', 'F2': 'F2', 'F3': 'F3', 'F4': 'F4',
    'F5': 'F5', 'F6': 'F6', 'F7': 'F7', 'F8': 'F8',
    'F9': 'F9', 'F10': 'F10', 'F11': 'F11', 'F12': 'F12',
    'AppsKey': 'Menu',
}


def _key_to_xdotool(key: str) -> str:
    """pynput 键名 → xdotool key 名称。"""
    if key in _KEY_TO_XKEYSYM:
        return _KEY_TO_XKEYSYM[key]
    # 单字符（字母、数字、符号）
    if len(key) == 1:
        return key
    return key


def playback_via_xdotool(data: dict, speed: float = 1.0, delay: float = DEFAULT_START_DELAY):
    """使用 xdotool 回放录制数据（Linux）。"""
    events = data["events"]
    if not events:
        return

    time.sleep(delay)

    prev_ms = 0
    btn_map = {"left": 1, "middle": 2, "right": 3}

    for ev in events:
        dt = (ev["time_ms"] - prev_ms) / 1000.0
        prev_ms = ev["time_ms"]

        if speed != 1.0:
            dt = max(0.001, dt / speed)

        if dt > 0:
            time.sleep(dt)

        t = ev["type"]

        if t == "move":
            subprocess.run(
                ["xdotool", "mousemove", str(ev["x"]), str(ev["y"])],
                capture_output=True,
            )

        elif t in ("press", "release"):
            btn = ev.get("button", "left")
            bnum = btn_map.get(btn, 1)
            action = "mousedown" if t == "press" else "mouseup"
            subprocess.run(
                ["xdotool", action, str(bnum)],
                capture_output=True,
            )

        elif t == "scroll":
            dy = ev.get("dy", 0)
            direction = 4 if dy > 0 else 5  # 4=up, 5=down
            count = abs(dy)
            for _ in range(count):
                subprocess.run(
                    ["xdotool", "click", str(direction)],
                    capture_output=True,
                )

        elif t in ("key_press", "key_release"):
            key = ev.get("key", "")
            xkey = _key_to_xdotool(key)
            if t == "key_press":
                subprocess.run(
                    ["xdotool", "keydown", xkey],
                    capture_output=True,
                )
            else:
                subprocess.run(
                    ["xdotool", "keyup", xkey],
                    capture_output=True,
                )


# ══════════════════════════════════════════════════════════════════
# 平台回放调度器
# ══════════════════════════════════════════════════════════════════

def play_back(data: dict, speed: float = 1.0, delay: float = DEFAULT_START_DELAY):
    """根据当前平台选择合适的回放引擎。"""
    if IS_LINUX:
        playback_via_xdotool(data, speed, delay)
    else:
        playback_via_sendinput(data, speed, delay)


# ══════════════════════════════════════════════════════════════════
# GUI 主应用
# ══════════════════════════════════════════════════════════════════

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("鼠标录制回放器 + 云端命令")
        self.root.geometry("820x680")
        self.root.minsize(640, 520)
        self.root.resizable(True, True)
        self.root.configure(bg=C_BG)

        # ── ttk 样式 ──
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=C_BG)
        style.configure("TLabelframe", background=C_BG)
        style.configure("TLabelframe.Label", background=C_BG, font=FONT_BOLD, foreground=C_PRIMARY_DARK)
        style.configure("TLabel", background=C_BG, font=FONT_BODY)
        style.configure("TButton", font=FONT_BODY, padding=(10, 4))
        style.configure("Accent.TButton", font=FONT_BOLD, padding=(12, 4))
        style.configure("Small.TButton", font=FONT_SMALL, padding=(6, 2))
        style.configure("Danger.TButton", font=FONT_BOLD, padding=(10, 4))
        style.map("TButton", background=[("active", "#E3F2FD")])
        style.configure("Treeview", font=FONT_BODY, rowheight=26)
        style.configure("Treeview.Heading", font=FONT_BOLD)
        style.configure("TNotebook", background=C_BG)
        style.configure("TNotebook.Tab", font=FONT_BOLD, padding=(14, 6))

        # 确保录制目录和规则目录存在
        RECORDINGS_DIR.mkdir(exist_ok=True)
        RULES_DIR.mkdir(exist_ok=True)
        from state_machine import JUMP_RULES_DIR
        JUMP_RULES_DIR.mkdir(exist_ok=True)

        # 状态
        self.session = RecordingSession()
        self._hotkey_listener: keyboard.GlobalHotKeys | None = None
        self._ahk_path: str | None = find_autohotkey()
        self._playback_thread: threading.Thread | None = None

        # ── 云端服务 ──
        self._cloud_cfg: CloudConfig | None = None
        self._cloud_svc: CloudService | None = None
        if CLOUD_OK:
            self._cloud_cfg = CloudConfig.load()
        else:
            self._cloud_cfg = None

        # ── 像素监测 ──
        self._pixel_monitor: PixelMonitor | None = None
        self._pixel_vars: dict[str, tk.StringVar] = {}  # 当前颜色显示绑定

        # ── 状态机 ──
        self._sm_instances: dict[str, StateMachineEngine] = {}  # 多实例: {name: engine}
        self._sm_active: str = ""  # 当前编辑的状态机名称
        self._sm_vars: dict[str, tk.StringVar] = {}
        self._sm_editing_state: str = ""  # 当前正在编辑的状态名（切换前先保存）
        self._sm_building: bool = False   # 防止 ComboBox 事件在程序化更新时触发

        # 构建界面
        self._build_ui()
        self._start_hotkeys()
        self._refresh_list()

        # 窗口关闭回调
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def _sm_engine(self) -> StateMachineEngine | None:
        """返回当前活跃的状态机引擎（兼容旧代码）。"""
        return self._sm_instances.get(self._sm_active)

    @_sm_engine.setter
    def _sm_engine(self, eng: StateMachineEngine | None):
        if eng is not None:
            self._sm_instances[self._sm_active or eng.config.name] = eng

    # ── UI 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        available = style.theme_names()
        if "clam" in available:
            style.theme_use("clam")

        # ── 顶层 Notebook 标签页 ──
        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 标签页 1: 录制回放
        self._tab_record = ttk.Frame(self._notebook, padding="12")
        self._notebook.add(self._tab_record, text="录制回放")
        self._build_record_tab(self._tab_record)

        # 标签页 2: 云端配置
        if CLOUD_OK:
            self._tab_cloud = ttk.Frame(self._notebook, padding="12")
            self._notebook.add(self._tab_cloud, text="云端配置")
            self._build_cloud_tab(self._tab_cloud)

        # 标签页 3: 分支配置
        if CONFIG_OK:
            self._tab_branch = ttk.Frame(self._notebook, padding="12")
            self._notebook.add(self._tab_branch, text="分支配置")
            self._build_branch_tab(self._tab_branch)

        # 标签页 4: 像素监测
        if PIXEL_OK:
            self._tab_pixel = ttk.Frame(self._notebook, padding="12")
            self._notebook.add(self._tab_pixel, text="像素监测")
            self._build_pixel_tab(self._tab_pixel)

        # 标签页 5: 使用说明
        self._tab_help = ttk.Frame(self._notebook, padding="12")
        self._notebook.add(self._tab_help, text="使用说明")
        self._build_help_tab(self._tab_help)

        # 标签页 6: 状态机
        if SM_OK:
            self._tab_sm = ttk.Frame(self._notebook, padding="12")
            self._notebook.add(self._tab_sm, text="状态机")
            self._build_sm_tab(self._tab_sm)

        # ── 底部状态栏（跨标签页） ──
        bottom_frame = tk.Frame(self.root, bg=C_PRIMARY_DARK, height=28)
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)
        bottom_frame.pack_propagate(False)

        if IS_LINUX:
            xdotool_path = _find_xdotool()
            if xdotool_path:
                engine_text = f"xdotool ✔ ({xdotool_path})"
            else:
                engine_text = "请安装 xdotool: sudo apt install xdotool"
        elif self._ahk_path:
            engine_text = f"AHK ✔ ({self._ahk_path})"
        else:
            engine_text = "引擎: SendInput (内置)"

        shortcuts_text = "F9:录制 F10:停止 F6/F7:取坐标 Esc:中断"
        self._engine_label = tk.Label(
            bottom_frame, text=engine_text, font=FONT_SMALL,
            fg="#BBDEFB", bg=C_PRIMARY_DARK, anchor=tk.W
        )
        self._engine_label.pack(side=tk.LEFT, padx=(12, 0), pady=4)
        tk.Label(
            bottom_frame, text=shortcuts_text,
            font=FONT_SMALL, fg="#90CAF9", bg=C_PRIMARY_DARK, anchor=tk.E
        ).pack(side=tk.RIGHT, padx=(0, 12), pady=4)

    # ── 录制标签页 ─────────────────────────────────────────

    def _build_record_tab(self, parent: ttk.Frame):
        # 状态区
        status_frame = tk.Frame(parent, bg=C_SURFACE, highlightbackground=C_BORDER,
                                highlightthickness=1, padx=10, pady=8)
        status_frame.pack(fill=tk.X, pady=(0, 8))

        self._status_var = tk.StringVar(value="就绪 — 按 F9 开始录制，F10 停止并保存")
        tk.Label(
            status_frame, textvariable=self._status_var, font=FONT_STATUS,
            fg=C_TEXT, bg=C_SURFACE, anchor=tk.W
        ).pack(anchor=tk.W)

        self._info_var = tk.StringVar(value="")
        tk.Label(
            status_frame, textvariable=self._info_var, font=FONT_SMALL,
            fg=C_TEXT_SEC, bg=C_SURFACE, anchor=tk.W
        ).pack(anchor=tk.W)

        # 录制列表
        list_frame = ttk.LabelFrame(parent, text="录制列表", padding="6")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        columns = ("name", "date", "duration", "events")
        self._tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self._tree.heading("name", text="名称")
        self._tree.heading("date", text="创建时间")
        self._tree.heading("duration", text="时长")
        self._tree.heading("events", text="事件数")

        self._tree.column("name", width=150, minwidth=80)
        self._tree.column("date", width=160, minwidth=100)
        self._tree.column("duration", width=70, minwidth=50, anchor=tk.CENTER)
        self._tree.column("events", width=70, minwidth=50, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.bind("<Double-1>", lambda _e: self._play_selected())

        # 按钮行
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, pady=(0, 6))

        self._btn_record = ttk.Button(
            btn_frame, text="⏺ 开始录制 (F9)", command=self._start_recording
        )
        self._btn_record.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_stop = ttk.Button(
            btn_frame, text="⏹ 停止录制 (F10)", command=self._stop_recording, state=tk.DISABLED
        )
        self._btn_stop.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self._kb_record_var = tk.BooleanVar(value=False)
        self._kb_record_cb = ttk.Checkbutton(
            btn_frame, text="录制键盘", variable=self._kb_record_var,
            command=self._on_kb_record_toggle
        )
        self._kb_record_cb.pack(side=tk.LEFT, padx=6)

        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self._btn_play = ttk.Button(btn_frame, text="▶ 播放选中", command=self._play_selected)
        self._btn_play.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_delete = ttk.Button(btn_frame, text="✕ 删除选中", command=self._delete_selected)
        self._btn_delete.pack(side=tk.LEFT, padx=(0, 6))

        self._btn_refresh = ttk.Button(btn_frame, text="↻ 刷新列表", command=self._refresh_list)
        self._btn_refresh.pack(side=tk.LEFT)

        # 速度选择
        speed_frame = ttk.LabelFrame(parent, text="回放速度", padding="4")
        speed_frame.pack(fill=tk.X, pady=(0, 6))

        self._speed_var = tk.DoubleVar(value=1.0)
        speed_options = [
            ("0.25x", 0.25),
            ("0.5x", 0.5),
            ("1x (原始)", 1.0),
            ("2x", 2.0),
            ("3x", 3.0),
            ("5x", 5.0),
        ]
        for label, val in speed_options:
            ttk.Radiobutton(
                speed_frame, text=label, variable=self._speed_var, value=val
            ).pack(side=tk.LEFT, padx=4, pady=2)

    # ── 云端配置标签页 ─────────────────────────────────────

    def _build_cloud_tab(self, parent: ttk.Frame):
        cfg = self._cloud_cfg

        # ── 服务器设置 ──
        server_frame = ttk.LabelFrame(parent, text="云端服务器", padding="8")
        server_frame.pack(fill=tk.X, pady=(0, 8))

        # 启用开关
        self._cloud_enabled_var = tk.BooleanVar(value=cfg.enabled)
        ttk.Checkbutton(
            server_frame,
            text="启用云端命令服务",
            variable=self._cloud_enabled_var,
            command=self._on_cloud_toggle,
        ).pack(anchor=tk.W, pady=(0, 6))

        # 服务器地址
        row1 = ttk.Frame(server_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="服务器地址", width=12).pack(side=tk.LEFT)
        self._cloud_url_var = tk.StringVar(value=cfg.server_url)
        self._cloud_url_entry = ttk.Entry(row1, textvariable=self._cloud_url_var, width=50)
        self._cloud_url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # API 密钥
        row2 = ttk.Frame(server_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="API 密钥", width=12).pack(side=tk.LEFT)
        self._cloud_key_var = tk.StringVar(value=cfg.api_key)
        self._cloud_key_entry = ttk.Entry(
            row2, textvariable=self._cloud_key_var, width=50, show="*"
        )
        self._cloud_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 轮询间隔
        row3 = ttk.Frame(server_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="轮询间隔(秒)", width=12).pack(side=tk.LEFT)
        self._cloud_interval_var = tk.IntVar(value=cfg.poll_interval_seconds)
        ttk.Spinbox(
            row3, textvariable=self._cloud_interval_var, from_=1, to=3600, width=8
        ).pack(side=tk.LEFT)

        # ── 目录设置 ──
        dir_frame = ttk.LabelFrame(parent, text="本地目录", padding="8")
        dir_frame.pack(fill=tk.X, pady=(0, 8))

        # 历史目录
        row4 = ttk.Frame(dir_frame)
        row4.pack(fill=tk.X, pady=2)
        ttk.Label(row4, text="历史目录", width=12).pack(side=tk.LEFT)
        self._cloud_history_var = tk.StringVar(value=cfg.history_dir)
        hist_entry = ttk.Entry(row4, textvariable=self._cloud_history_var, width=42)
        hist_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(
            row4, text="浏览", width=6,
            command=lambda: self._browse_dir(self._cloud_history_var)
        ).pack(side=tk.LEFT)

        # 执行目录
        row5 = ttk.Frame(dir_frame)
        row5.pack(fill=tk.X, pady=2)
        ttk.Label(row5, text="执行目录", width=12).pack(side=tk.LEFT)
        self._cloud_exec_var = tk.StringVar(value=cfg.exec_dir)
        exec_entry = ttk.Entry(row5, textvariable=self._cloud_exec_var, width=42)
        exec_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(
            row5, text="浏览", width=6,
            command=lambda: self._browse_dir(self._cloud_exec_var)
        ).pack(side=tk.LEFT)

        # ── 按钮 ──
        ctrl_frame = ttk.Frame(parent)
        ctrl_frame.pack(fill=tk.X, pady=(0, 8))

        self._cloud_save_btn = ttk.Button(
            ctrl_frame, text="保存配置", command=self._save_cloud_config
        )
        self._cloud_save_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._cloud_start_btn = ttk.Button(
            ctrl_frame, text="启动云服务", command=self._toggle_cloud_service
        )
        self._cloud_start_btn.pack(side=tk.LEFT, padx=(0, 6))

        # ── 云状态 ──
        cloud_status_frame = ttk.LabelFrame(parent, text="云服务状态", padding="6")
        cloud_status_frame.pack(fill=tk.BOTH, expand=True)

        self._cloud_status_text = tk.Text(
            cloud_status_frame, height=6, wrap=tk.WORD,
            font=FONT_MONO, state=tk.DISABLED,
            bg=C_LOG_BG, fg=C_LOG_FG, insertbackground=C_LOG_FG, relief=tk.FLAT
        )
        cloud_scroll = ttk.Scrollbar(cloud_status_frame, orient=tk.VERTICAL,
                                     command=self._cloud_status_text.yview)
        self._cloud_status_text.configure(yscrollcommand=cloud_scroll.set)
        self._cloud_status_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cloud_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._cloud_log("就绪 — 请在服务器设置中配置云端地址后启动服务")

        # 初始禁用状态
        self._on_cloud_toggle()

    # ── 云端操作 ────────────────────────────────────────────

    def _browse_dir(self, var: tk.StringVar):
        path = filedialog.askdirectory(parent=self.root, title="选择目录")
        if path:
            var.set(path)

    def _on_cloud_toggle(self):
        """启用/禁用云端控件状态。"""
        enabled = self._cloud_enabled_var.get()
        state = tk.NORMAL if enabled else tk.DISABLED
        self._cloud_url_entry.configure(state=state)
        self._cloud_key_entry.configure(state=state)
        if not self._cloud_svc or not self._cloud_svc.running:
            self._cloud_start_btn.configure(state=state)

    def _save_cloud_config(self):
        """保存云端配置到文件。"""
        self._cloud_cfg = CloudConfig(
            enabled=self._cloud_enabled_var.get(),
            server_url=self._cloud_url_var.get().strip(),
            api_key=self._cloud_key_var.get().strip(),
            poll_interval_seconds=self._cloud_interval_var.get(),
            history_dir=self._cloud_history_var.get().strip(),
            exec_dir=self._cloud_exec_var.get().strip(),
        )
        err = self._cloud_cfg.validate()
        if err and self._cloud_cfg.enabled:
            messagebox.showwarning("配置验证", err)
            return

        try:
            self._cloud_cfg.save()
            self._cloud_log("配置已保存")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _toggle_cloud_service(self):
        """启动或停止云服务。"""
        if self._cloud_svc and self._cloud_svc.running:
            self._stop_cloud_service()
        else:
            self._start_cloud_service()

    def _start_cloud_service(self):
        """启动云端后台服务。"""
        if not self._cloud_cfg:
            return
        err = self._cloud_cfg.validate()
        if err:
            messagebox.showwarning("配置无效", err)
            return

        self._cloud_svc = CloudService(self._cloud_cfg)
        self._cloud_svc.set_status_callback(
            lambda msg: self.root.after(0, lambda: self._cloud_log(msg))
        )
        self._cloud_svc.start()
        self._cloud_start_btn.configure(text="停止云服务")
        self._cloud_save_btn.configure(state=tk.DISABLED)
        self._cloud_log("云服务启动中...")

    def _stop_cloud_service(self):
        """停止云端后台服务。"""
        if self._cloud_svc:
            self._cloud_svc.stop()
            self._cloud_svc = None
        self._cloud_start_btn.configure(text="启动云服务")
        self._cloud_save_btn.configure(state=tk.NORMAL)
        self._cloud_log("云服务已停止")

    def _cloud_log(self, msg: str):
        """向云端状态文本框追加日志。"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._cloud_status_text.configure(state=tk.NORMAL)
        self._cloud_status_text.insert(tk.END, f"[{timestamp}] {msg}\n")
        self._cloud_status_text.see(tk.END)
        self._cloud_status_text.configure(state=tk.DISABLED)

    # ── 分支配置标签页 ─────────────────────────────────────

    def _build_branch_tab(self, parent: ttk.Frame):
        """构建打印任务 A/B 分支配置界面。"""
        # 顶部说明
        desc = ttk.Label(
            parent, text="配置打印任务 A/B 分支各自的录制文件、完成检测区域和超时参数。\n"
                         "云端发送 print_task_a 时走分支 A，print_task_b 时走分支 B。\n"
                         "录制名留空则自动回退到云端配置页的通用设置。",
            font=FONT_SMALL, foreground=C_TEXT_SEC
        )
        desc.pack(anchor=tk.W, pady=(0, 8))

        # ── 分支 A ──
        frame_a = ttk.LabelFrame(parent, text="任务分支 A", padding="8")
        frame_a.pack(fill=tk.X, pady=(0, 6))
        self._branch_a_vars = self._build_branch_form(frame_a, "A")

        # ── 分支 B ──
        frame_b = ttk.LabelFrame(parent, text="任务分支 B", padding="8")
        frame_b.pack(fill=tk.X, pady=(0, 6))
        self._branch_b_vars = self._build_branch_form(frame_b, "B")

        # ── 按钮行 ──
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_frame, text="保存配置", command=self._branch_save_config).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="重新加载", command=self._branch_load_config).pack(side=tk.LEFT)
        self._branch_status_var = tk.StringVar()
        ttk.Label(btn_frame, textvariable=self._branch_status_var, font=FONT_SMALL,
                  foreground=C_SUCCESS).pack(side=tk.LEFT, padx=12)

        # 初始加载
        self._branch_load_config()

    def _build_branch_form(self, parent: ttk.Frame, branch: str) -> dict[str, tk.StringVar]:
        """构建单个分支的表单，返回变量字典。"""
        vars_dict = {}
        prefix = f"branch_{branch.lower()}_"

        # ── 第1行：录制文件 ──
        r1 = ttk.Frame(parent)
        r1.pack(fill=tk.X, pady=2)

        ttk.Label(r1, text="上传任务录制", width=12).pack(side=tk.LEFT)
        v1 = tk.StringVar()
        combo1 = ttk.Combobox(r1, textvariable=v1, width=22)
        combo1.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(r1, text="↻", width=3,
                   command=lambda: self._branch_refresh_recordings([combo1])).pack(side=tk.LEFT, padx=2)
        vars_dict[f"{prefix}upload"] = v1

        r2 = ttk.Frame(parent)
        r2.pack(fill=tk.X, pady=2)

        ttk.Label(r2, text="开始打印录制", width=12).pack(side=tk.LEFT)
        v2 = tk.StringVar()
        combo2 = ttk.Combobox(r2, textvariable=v2, width=22)
        combo2.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(r2, text="↻", width=3,
                   command=lambda: self._branch_refresh_recordings([combo2])).pack(side=tk.LEFT, padx=2)
        vars_dict[f"{prefix}start"] = v2

        # ── 第2行：完成检测 ──
        r3 = ttk.Frame(parent)
        r3.pack(fill=tk.X, pady=2)

        ttk.Label(r3, text="完成检测区域", width=12).pack(side=tk.LEFT)
        v3 = tk.StringVar()
        ttk.Entry(r3, textvariable=v3, width=24).pack(side=tk.LEFT, fill=tk.X, expand=True)
        vars_dict[f"{prefix}region"] = v3

        r4 = ttk.Frame(parent)
        r4.pack(fill=tk.X, pady=2)

        ttk.Label(r4, text="完成颜色", width=12).pack(side=tk.LEFT)
        v4 = tk.StringVar(value="#00FF00")
        ttk.Entry(r4, textvariable=v4, width=10).pack(side=tk.LEFT)
        vars_dict[f"{prefix}color"] = v4
        # 色块预览
        swatch = tk.Canvas(r4, width=18, height=18, highlightthickness=1, highlightbackground="#999")
        swatch.pack(side=tk.LEFT, padx=4)
        v4.trace_add("write", lambda *a, c=swatch, v=v4: self._branch_update_swatch(c, v.get()))
        vars_dict[f"{prefix}_swatch"] = swatch
        ttk.Button(r4, text="选色", width=4,
                   command=lambda v=v4, s=swatch: self._branch_pick_color(v, s)).pack(side=tk.LEFT, padx=2)
        self._branch_update_swatch(swatch, v4.get())

        # ── 第3行：超时参数 ──
        r5 = ttk.Frame(parent)
        r5.pack(fill=tk.X, pady=2)

        ttk.Label(r5, text="超时(分钟)", width=12).pack(side=tk.LEFT)
        v5 = tk.StringVar(value="120")
        ttk.Spinbox(r5, textvariable=v5, from_=1, to=1440, width=6).pack(side=tk.LEFT)
        vars_dict[f"{prefix}timeout"] = v5

        ttk.Label(r5, text="额外等待(分钟)", width=14).pack(side=tk.LEFT, padx=(16, 0))
        v6 = tk.StringVar(value="10")
        ttk.Spinbox(r5, textvariable=v6, from_=1, to=120, width=6).pack(side=tk.LEFT)
        vars_dict[f"{prefix}extra"] = v6

        # 存 combobox 引用用于刷新
        vars_dict[f"{prefix}_combos"] = [combo1, combo2]

        return vars_dict

    # ── 分支配置操作 ─────────────────────────────────────────

    def _branch_update_swatch(self, canvas: tk.Canvas, color: str):
        try:
            if color.startswith("#") and len(color) == 7:
                canvas.configure(bg=color)
            else:
                canvas.configure(bg="#CCCCCC")
        except Exception:
            canvas.configure(bg="#CCCCCC")

    def _branch_pick_color(self, var: tk.StringVar, swatch: tk.Canvas):
        color = colorchooser.askcolor(initialcolor=var.get(), title="选择完成颜色")
        if color and color[1]:
            var.set(color[1])
            self._branch_update_swatch(swatch, color[1])

    @staticmethod
    def _branch_refresh_recordings(combos: list[ttk.Combobox]):
        """刷新 Combobox 下拉列表中的录制名称。"""
        try:
            names = [f.stem for f in RECORDINGS_DIR.glob("*.json")]
        except Exception:
            names = []
        for c in combos:
            c["values"] = names

    def _branch_load_config(self):
        """从 config.json 加载分支配置到 UI。"""
        try:
            cfg = AppConfig.load()
        except Exception as e:
            self._branch_status_var.set(f"加载失败: {e}")
            return

        self._branch_fill_vars(self._branch_a_vars, cfg.print.task_branch_a)
        self._branch_fill_vars(self._branch_b_vars, cfg.print.task_branch_b)

        # 刷新所有下拉列表
        all_combos = []
        for vd in (self._branch_a_vars, self._branch_b_vars):
            for _, val in vd.items():
                if isinstance(val, list) and val and isinstance(val[0], ttk.Combobox):
                    pass
            key = list(vd.keys())[0]  # dummy
        self._branch_refresh_recordings_for_all()

        self._branch_status_var.set("已加载配置")

    def _branch_refresh_recordings_for_all(self):
        """刷新分支 A 和 B 所有录制下拉列表。"""
        names = []
        try:
            if RECORDINGS_DIR.exists():
                names = [f.stem for f in sorted(RECORDINGS_DIR.glob("*.json"))]
        except Exception:
            pass
        for vd in (self._branch_a_vars, self._branch_b_vars):
            for key, val in vd.items():
                if key.endswith("_combos") and isinstance(val, list):
                    for combo in val:
                        combo["values"] = names

    @staticmethod
    def _branch_fill_vars(vars_dict: dict, branch_cfg):
        """将 TaskBranchConfig 填充到对应的 StringVar。"""
        prefix = None
        for k in vars_dict:
            if k.endswith("_combos") or k.endswith("_swatch"):
                continue
            parts = k.rsplit("_", 1)
            if len(parts) == 2 and parts[1] in ("upload", "start", "region", "color", "timeout", "extra"):
                prefix = k[:k.rfind("_")]
                break

        if not prefix:
            return

        mapping = {
            f"{prefix}_upload": branch_cfg.upload_task_recording,
            f"{prefix}_start": branch_cfg.start_print_recording,
            f"{prefix}_region": branch_cfg.complete_region,
            f"{prefix}_color": branch_cfg.complete_color,
            f"{prefix}_timeout": str(branch_cfg.default_timeout_minutes),
            f"{prefix}_extra": str(branch_cfg.extra_timeout_minutes),
        }

        for var_name, value in mapping.items():
            if var_name in vars_dict:
                vars_dict[var_name].set(value)

    def _branch_save_config(self):
        """保存分支配置到 config.json。"""
        try:
            cfg = AppConfig.load()
        except Exception as e:
            self._branch_status_var.set(f"加载配置失败: {e}")
            return

        self._branch_collect_vars(self._branch_a_vars, cfg.print.task_branch_a)
        self._branch_collect_vars(self._branch_b_vars, cfg.print.task_branch_b)

        try:
            cfg.save()
            self._branch_status_var.set("配置已保存")
        except Exception as e:
            self._branch_status_var.set(f"保存失败: {e}")

    def _branch_collect_vars(self, vars_dict: dict, branch_cfg):
        """从 StringVar 收集值写入 TaskBranchConfig 对象。"""
        prefix = None
        for k in vars_dict:
            if k.endswith("_combos") or k.endswith("_swatch"):
                continue
            parts = k.rsplit("_", 1)
            if len(parts) == 2 and parts[1] in ("upload", "start", "region", "color", "timeout", "extra"):
                prefix = k[:k.rfind("_")]
                break
        if not prefix:
            return

        def _get(k): return vars_dict.get(f"{prefix}_{k}", tk.StringVar()).get().strip()

        branch_cfg.upload_task_recording = _get("upload")
        branch_cfg.start_print_recording = _get("start")
        branch_cfg.complete_region = _get("region")
        branch_cfg.complete_color = _get("color")

        try:
            branch_cfg.default_timeout_minutes = int(_get("timeout"))
        except ValueError:
            branch_cfg.default_timeout_minutes = 120

        try:
            branch_cfg.extra_timeout_minutes = int(_get("extra"))
        except ValueError:
            branch_cfg.extra_timeout_minutes = 10

    # ── 区块监测标签页 ─────────────────────────────────────

    def _build_pixel_tab(self, parent: ttk.Frame):
        # ── 添加监测区域 ──
        add_frame = ttk.LabelFrame(parent, text="监测区域 — F6取P1  F7取P2  F8取颜色", padding="8")
        add_frame.pack(fill=tk.X, pady=(0, 6))

        # 名称 + 两点坐标（紧凑布局）
        row_name = ttk.Frame(add_frame)
        row_name.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(row_name, text="名称").pack(side=tk.LEFT)
        self._px_name_var = tk.StringVar(value="")
        ttk.Entry(row_name, textvariable=self._px_name_var, width=14).pack(side=tk.LEFT, padx=6)

        ttk.Label(row_name, text="P1").pack(side=tk.LEFT)
        self._px_x1_var = tk.IntVar(value=0)
        ttk.Spinbox(row_name, textvariable=self._px_x1_var, from_=0, to=9999, width=5).pack(side=tk.LEFT)
        self._px_y1_var = tk.IntVar(value=0)
        ttk.Spinbox(row_name, textvariable=self._px_y1_var, from_=0, to=9999, width=5).pack(side=tk.LEFT)
        self._px_p1_label = ttk.Label(row_name, text="", foreground="gray")
        self._px_p1_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(row_name, text="取P1", width=3, command=self._pixel_capture_p1).pack(side=tk.LEFT)

        ttk.Label(row_name, text="P2").pack(side=tk.LEFT, padx=(8, 0))
        self._px_x2_var = tk.IntVar(value=0)
        ttk.Spinbox(row_name, textvariable=self._px_x2_var, from_=0, to=9999, width=5).pack(side=tk.LEFT)
        self._px_y2_var = tk.IntVar(value=0)
        ttk.Spinbox(row_name, textvariable=self._px_y2_var, from_=0, to=9999, width=5).pack(side=tk.LEFT)
        self._px_p2_label = ttk.Label(row_name, text="", foreground="gray")
        self._px_p2_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(row_name, text="取P2", width=3, command=self._pixel_capture_p2).pack(side=tk.LEFT)

        ttk.Button(row_name, text="添加区域", command=self._pixel_add_region).pack(side=tk.RIGHT)

        # ── 鼠标实时坐标 ──
        mouse_row = ttk.Frame(add_frame)
        mouse_row.pack(fill=tk.X, pady=(3, 0))
        self._px_mouse_label = ttk.Label(mouse_row, text="鼠标: (等待...)", font=("TkFixedFont", 10))
        self._px_mouse_label.pack(side=tk.LEFT)
        ttk.Button(mouse_row, text="→P1", width=3, command=lambda: self._px_mouse_to("P1")).pack(side=tk.LEFT, padx=2)
        ttk.Button(mouse_row, text="→P2", width=3, command=lambda: self._px_mouse_to("P2")).pack(side=tk.LEFT)
        self._px_mouse_tracking = False

        # ── 监测设置 ──
        setting_frame = ttk.LabelFrame(parent, text="监测设置", padding="6")
        setting_frame.pack(fill=tk.X, pady=(0, 6))
        sf = ttk.Frame(setting_frame)
        sf.pack(fill=tk.X)
        ttk.Label(sf, text="采样间隔(ms)").pack(side=tk.LEFT)
        self._px_interval_var = tk.IntVar(value=2000)
        ttk.Spinbox(sf, textvariable=self._px_interval_var, from_=500, to=60000, increment=500, width=6).pack(side=tk.LEFT, padx=4)
        self._px_start_btn = ttk.Button(sf, text="开始监测", command=self._toggle_pixel_monitor)
        self._px_start_btn.pack(side=tk.LEFT, padx=(16, 0))

        # ── 区域列表 (左) + 规则编辑 (右) ──
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # 左: 区域列表
        list_frame = ttk.LabelFrame(paned, text="区域", padding="6")
        paned.add(list_frame, weight=1)

        columns = ("label", "rect", "status", "changes")
        self._px_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse", height=4)
        self._px_tree.heading("label", text="名称")
        self._px_tree.heading("rect", text="大小")
        self._px_tree.heading("status", text="当前颜色")
        self._px_tree.heading("changes", text="变化")
        self._px_tree.column("label", width=70, minwidth=50)
        self._px_tree.column("rect", width=65, minwidth=50, anchor=tk.CENTER)
        self._px_tree.column("status", width=80, minwidth=60, anchor=tk.CENTER)
        self._px_tree.column("changes", width=45, minwidth=40, anchor=tk.CENTER)
        self._px_tree.pack(fill=tk.BOTH, expand=True)
        self._px_tree.bind("<<TreeviewSelect>>", self._on_region_selected)
        self._px_tree.bind("<Button-3>", self._pixel_delete_region)

        # 右: 规则编辑
        rule_frame = ttk.LabelFrame(paned, text="触发规则", padding="6")
        paned.add(rule_frame, weight=1)

        self._px_rule_label_var = tk.StringVar(value="← 选中左侧区域后编辑规则")
        ttk.Label(rule_frame, textvariable=self._px_rule_label_var, font=FONT_BOLD).pack(anchor=tk.W, pady=(0, 4))

        rule_columns = ("rcolor", "rtol", "rrec")
        self._px_rule_tree = ttk.Treeview(rule_frame, columns=rule_columns, show="headings", selectmode="browse", height=4)
        self._px_rule_tree.heading("rcolor", text="颜色")
        self._px_rule_tree.heading("rtol", text="容差")
        self._px_rule_tree.heading("rrec", text="回放录制")
        self._px_rule_tree.column("rcolor", width=55, anchor=tk.CENTER)
        self._px_rule_tree.column("rtol", width=40, anchor=tk.CENTER)
        self._px_rule_tree.column("rrec", width=130)
        self._px_rule_tree.pack(fill=tk.BOTH, expand=True)
        self._px_rule_tree.bind("<Double-1>", self._pixel_edit_rule_populate)

        # 规则表单
        rform = ttk.Frame(rule_frame)
        rform.pack(fill=tk.X, pady=(4, 0))

        ttk.Label(rform, text="颜色").pack(side=tk.LEFT)
        self._px_rule_color_var = tk.StringVar(value="#00FF00")
        self._px_rule_color_entry = ttk.Entry(rform, textvariable=self._px_rule_color_var, width=8)
        self._px_rule_color_entry.pack(side=tk.LEFT, padx=2)
        self._px_rule_color_var.trace_add("write", self._on_color_var_changed)

        # 色块预览
        self._px_color_swatch = tk.Canvas(rform, width=18, height=18, highlightthickness=1, highlightbackground="#999")
        self._px_color_swatch.pack(side=tk.LEFT, padx=2)
        ttk.Button(rform, text="选色", width=4, command=self._pixel_pick_color).pack(side=tk.LEFT)
        ttk.Button(rform, text="吸色", width=4, command=self._pixel_eyedropper).pack(side=tk.LEFT, padx=1)

        ttk.Label(rform, text="容差").pack(side=tk.LEFT, padx=(4, 0))
        self._px_rule_tol_var = tk.IntVar(value=20)
        ttk.Spinbox(rform, textvariable=self._px_rule_tol_var, from_=0, to=200, width=4).pack(side=tk.LEFT, padx=2)

        ttk.Label(rform, text="录制").pack(side=tk.LEFT, padx=(4, 0))
        self._px_rule_recording_var = tk.StringVar(value="")
        self._px_rule_combo = ttk.Combobox(rform, textvariable=self._px_rule_recording_var, width=16, state="readonly")
        self._px_rule_combo.pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        ttk.Button(rform, text="↻", width=3, command=self._pixel_refresh_recordings).pack(side=tk.LEFT)
        self._pixel_refresh_recordings()

        # 当前编辑规则索引（-1 = 新建模式）
        self._px_editing_rule_idx: int = -1

        rbtn = ttk.Frame(rule_frame)
        rbtn.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(rbtn, text="添加规则", command=self._pixel_add_rule).pack(side=tk.LEFT, padx=(0, 4))
        self._px_rule_upd_btn = ttk.Button(rbtn, text="更新规则", command=self._pixel_update_rule, state=tk.DISABLED)
        self._px_rule_upd_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._px_rule_del_btn = ttk.Button(rbtn, text="删除规则", command=self._pixel_delete_rule, state=tk.DISABLED)
        self._px_rule_del_btn.pack(side=tk.LEFT)

        self._update_color_swatch()

        # ── 变化日志 ──
        log_frame = ttk.LabelFrame(parent, text="事件日志", padding="6")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self._px_log_text = tk.Text(log_frame, height=4, wrap=tk.WORD, font=FONT_MONO, state=tk.DISABLED,
                                       bg=C_LOG_BG, fg=C_LOG_FG, insertbackground=C_LOG_FG, relief=tk.FLAT)
        px_log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self._px_log_text.yview)
        self._px_log_text.configure(yscrollcommand=px_log_scroll.set)
        self._px_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        px_log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._pixel_load_config()
        self._px_log("就绪 — 鼠标坐标实时显示 | F6/→P1 F7/→P2 取坐标 | F8取颜色")
        self._start_mouse_tracking()

    # ── 区块监测操作 ─────────────────────────────────────────

    def _update_color_swatch(self):
        """更新颜色预览色块。"""
        try:
            c = self._px_rule_color_var.get().strip()
            if c.startswith("#") and len(c) == 7:
                self._px_color_swatch.configure(bg=c)
            else:
                self._px_color_swatch.configure(bg="#CCCCCC")
        except Exception:
            pass

    def _on_color_var_changed(self, *_args):
        self._update_color_swatch()

    def _pixel_load_config(self):
        try:
            mon = PixelMonitor.load_config()
        except Exception:
            return
        self._px_interval_var.set(int(mon.interval * 1000))
        self._pixel_monitor = mon
        self._px_tree.delete(*self._px_tree.get_children())
        seen: set[str] = set()
        for r in mon.regions:
            iid = r.label
            if iid in seen:
                iid = f"{r.label}_{len(seen)}"
                r.label = iid
            seen.add(iid)
            status = "—"
            self._px_tree.insert("", tk.END, iid=iid,
                                 values=(r.label, f"{r.width}x{r.height}", status, str(r.change_count)))
        if mon.regions:
            self._px_log(f"已加载 {len(mon.regions)} 个监测区域")

    def _pixel_save_config(self):
        if self._pixel_monitor:
            self._pixel_monitor.interval = self._px_interval_var.get() / 1000.0
            try:
                self._pixel_monitor.save_config()
            except Exception:
                pass

    def _on_region_selected(self, event):
        sel = self._px_tree.selection()
        self._px_rule_tree.delete(*self._px_rule_tree.get_children())
        if not sel:
            self._px_rule_label_var.set("← 选中左侧区域后编辑规则")
            self._px_rule_del_btn.configure(state=tk.DISABLED)
            return
        name = sel[0]
        self._px_rule_label_var.set(f"区域: {name}")
        if self._pixel_monitor:
            r = self._pixel_monitor.get_region(name)
            if r:
                for rule in r.rules:
                    iid = self._px_rule_tree.insert("", tk.END,
                                              values=(rule.color, str(rule.tolerance), rule.recording))
                    self._px_tag_rule_row(iid, rule.color)
        self._px_rule_del_btn.configure(state=tk.NORMAL)

    def _px_tag_rule_row(self, iid: str, color: str):
        """为规则行添加颜色标签 — 前景色 = 目标颜色，直观辨识。"""
        tag = f"c_{color.lstrip('#')}"
        self._px_rule_tree.tag_configure(tag, foreground=color)
        self._px_rule_tree.item(iid, tags=(tag,))

    # ── 使用说明标签页 ─────────────────────────────────────

    def _build_help_tab(self, parent: ttk.Frame):
        text = tk.Text(parent, wrap=tk.WORD, font=FONT_BODY,
                       padx=16, pady=12, state=tk.DISABLED,
                       relief=tk.FLAT, borderwidth=0)
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 样式配置
        text.tag_configure("h1", font=FONT_HERO, spacing1=16, spacing3=8)
        text.tag_configure("h2", font=FONT_TITLE, spacing1=12, spacing3=4)
        text.tag_configure("body", font=FONT_BODY, spacing3=2,
                           lmargin1=8, lmargin2=8)
        text.tag_configure("tip", font=FONT_ITALIC, foreground="#555555",
                           lmargin1=16, lmargin2=16)
        text.tag_configure("key", font=(FONT_MONO[0], FONT_MONO[1], "bold"), foreground="#0066CC",
                           background="#EEF3F8")
        text.tag_configure("path", font=FONT_MONO, foreground="#666600")
        text.tag_configure("sep", spacing1=10, spacing3=10)

        GUIDE = """\
项目功能总览

本工具是 3D 打印机自动化四合一工具，包含录制回放、云端通信、像素监测、状态机四大核心模块，并包含自动化控制器（main.py）实现无人值守。

══════════════════════════════════════════════════════

一、录制回放 — 录制鼠标与键盘操作并自动回放

本模块可录制鼠标的移动、点击、滚轮操作，也可同时录制键盘按键，保存后随时回放。

操作步骤：
  1. 如需录制键盘，勾选按钮栏的「录制键盘」复选框
  2. 按 F9 或点击「开始录制」按钮 → 开始记录鼠标（和键盘）操作
  3. 操作完成后按 F10 或点击「停止录制」 → 弹出对话框命名并保存
  4. 在录制列表中双击某个录制 → 自动回放
  5. 按 Esc 可随时中断回放

回放引擎：
  • Windows: AutoHotkey 引擎（优先）或 SendInput API
  • Linux:   xdotool 引擎（Ubuntu 自带）
  • 支持 0.25x ~ 5x 多档调速

文件存储：所有录制保存在 recordings 目录下（.json + .ahk 两个文件）。

══════════════════════════════════════════════════════

二、像素监测 — 屏幕区域颜色监测 + 自动触发录制

本模块监测屏幕任意矩形区域，当画面变化且检测到指定颜色时，自动回放录制。

新功能：鼠标实时坐标显示
  • 打开像素监测页即显示当前鼠标坐标，每 100ms 刷新
  • 点击「→P1」「→P2」直接将当前坐标填入对应输入框
  • 也可手动输入坐标，无需依赖快捷键

快捷键：
  F6 或点「取P1」— 捕获鼠标位置作为 P1（矩形第一角）
  F7 或点「取P2」— 捕获鼠标位置作为 P2（矩形第二角）
  F8 — 捕获鼠标所在像素颜色，填入规则颜色框

操作步骤：
  1. 移动鼠标到监测区域第一角 → 按 F6 或点「→P1」
  2. 移动鼠标到对角位置 → 按 F7 或点「→P2」
  3. 输入名称 → 点击「添加区域」
  4. 选中左侧区域 → 编辑触发规则（颜色/容差/录制名）→ 「添加规则」
  5. 双击规则行 → 值自动填入编辑表单 → 修改后点「更新规则」保存
  6. 点击「开始监测」→ 引擎开始按间隔扫描

吸色工具：
  点击「吸色」→ 出现十字准星 → 按住左键拖拽实时预览 → 松开确认 → Esc 取消

══════════════════════════════════════════════════════

三、状态机 — PLC 风格像素驱动自动化（支持多实例并发）

通过像素颜色条件驱动状态转移，每个状态进入/退出时自动回放录制。

多实例支持：
  • 可创建多个状态机实例，每个独立配置、独立运行
  • 顶部下拉选择器切换编辑不同的状态机
  • 「新建」创建新实例，「删除」移除当前实例
  • 「加载...」可多选导入多个状态机 JSON 文件
  • 「▶ 全部运行」一键启动所有状态机，各实例并发执行
  • 每个实例运行在独立线程，互不干扰

核心概念：
  状态 (State) → 一个工步，代表自动化流程的一个阶段
  条件 (Condition) → 转移触发信号（像素颜色匹配）
  动作 (Action) → 进入/退出时执行的操作（回放录制）
  转移规则 (Transition Rule) → 多条件 AND 逻辑满足后跳转到目标状态

编辑技巧：
  • 双击条件行 → 值自动填入编辑表单 → 点击"更新"保存修改
  • 点击"保存" → 当前状态机写入 state_machine.json

典型场景：
  state_1: 检测登录按钮 → 回放"输入密码"
  state_2: 检测主界面标志 → 回放"点击报表"
  state_3: 检测报表完成 → 回放"导出数据"
  设置循环=true → 持续值守

══════════════════════════════════════════════════════

四、云端通信 — 双向消息 + 文件管理

云端服务支持：
  • 发送设备状态（空闲/打印中/故障）
  • 接收打印任务（A/B 分支）
  • 接收取消指令
  • 心跳包（30s 间隔，含任务ID/进度/运行时长）
  • 文件下载与自动分发

配置：编辑 config.json 填写服务器地址和 API 密钥即可。

══════════════════════════════════════════════════════

五、自动化控制器 (main.py)

headless 模式运行，无需 GUI：

  python3 main.py

上电自启动流程：
  初始化 → 启动 BambuLab Studio → 像素确认启动成功 → 发送空闲状态
  → 主循环等待云端消息 → 收到打印任务 → 校验/启动打印 → 后台计时
  → 打印完成 → 通知 MCU → 通知云端 → 返回空闲

BambuLab Studio 位置：
  项目已内置 AppImage: bambu stdio/BambuStudio_ubuntu-22.04-*.AppImage
  无需额外下载，config.json 中已配置好路径。

云端升级（无需手动操作）：
  控制器支持四种远程升级类型：
    • update_software   — 更新 Python 脚本
    • update_config     — 更新配置文件
    • update_appimage   — 更新 BambuLab Studio
    • update_recording  — 更新录制文件
  升级前自动备份到 backup_YYYYMMDD_HHMMSS/ 目录。
  状态机配置可通过 GUI「状态机」页导出/导入，支持跨设备迁移。

详细说明参见 GUIDE.md。

══════════════════════════════════════════════════════

六、环境要求与前置准备

支持系统：
  • Windows 10/11 — 全功能
  • Ubuntu 22.04+ — 全功能（推荐 Xorg 会话）

Ubuntu 22.04 安装步骤：
  1. sudo apt update && sudo apt upgrade -y
  2. sudo apt install python3 python3-pip python3-tk python3-pil xdotool -y
  3. pip install pynput pillow
  4. python3 mouse_recorder.py

Windows 安装步骤：
  1. 安装 Python 3.8+
  2. pip install pynput pillow
  3. (可选) 安装 AutoHotkey v1
  4. python mouse_recorder.py

各模块依赖：
  录制回放    pynput（必需）, xdotool（Linux必需）, AHK（Windows可选）
  像素监测    Pillow（Linux必需）
  云端通信    无额外依赖
  状态机      同像素监测
  控制器      pyserial（MCU通信可选）

══════════════════════════════════════════════════════

配置文件

  config.json          — 统一配置（云端/Bambu/打印/MCU）
  pixel_config.json    — 像素监测区域和规则
  state_machine.json   — 状态机配置
  cloud_config.json    — 云端配置（向后兼容）
  recordings/          — 鼠标录制文件
  cloud_history/       — 云端下载历史
  cloud_exec/          — 云端当前执行文件
"""

        text.configure(state=tk.NORMAL)
        text.insert("1.0", GUIDE)

        # 应用样式标签
        # 标题
        for title in ["项目功能总览"]:
            idx = text.search(title, "1.0", tk.END)
            if idx:
                text.tag_add("h1", idx, f"{idx}+{len(title)}c")

        # 一级标题
        for h2 in ["一、录制回放", "二、云端配置", "三、像素监测", "四、联合使用场景",
                    "五、环境要求与前置准备", "六、状态机", "配置文件"]:
            idx = text.search(h2, "1.0", tk.END)
            if idx:
                text.tag_add("h2", idx, f"{idx}+{len(h2)}c")

        # 快捷键高亮
        for key in ["F6", "F7", "F8", "F9", "F10", "Esc"]:
            start = "1.0"
            while True:
                idx = text.search(key, start, tk.END)
                if not idx:
                    break
                text.tag_add("key", idx, f"{idx}+{len(key)}c")
                start = f"{idx}+{len(key)}c"

        # 路径高亮
        for path in ["pixel_config.json", "cloud_config.json", "state_machine.json",
                     "recordings/", "cloud_history/", "cloud_exec/",
                     ".json", ".ahk", "install_service.sh"]:
            start = "1.0"
            while True:
                idx = text.search(path, start, tk.END)
                if not idx:
                    break
                text.tag_add("path", idx, f"{idx}+{len(path)}c")
                start = f"{idx}+{len(path)}c"

        text.configure(state=tk.DISABLED)

    # ── 状态机多实例管理 ────────────────────────────────────

    def _sm_refresh_selector(self):
        """刷新状态机选择下拉框。"""
        names = list(self._sm_instances.keys())
        self._sm_selector_combo["values"] = names
        if names and not self._sm_active:
            self._sm_selector_var.set(names[0])
            self._on_sm_selector_change()

    def _sm_new_instance(self):
        """新建状态机实例。"""
        from tkinter import simpledialog
        name = simpledialog.askstring("新建状态机", "输入状态机名称:",
                                      parent=self.root)
        if not name:
            return
        if name in self._sm_instances:
            messagebox.showinfo("提示", f"状态机「{name}」已存在")
            return
        eng = StateMachineEngine(SMConfig())
        eng.config.name = name
        eng.set_replay_callback(self._replay_by_name)
        eng.set_event_callback(lambda ev: self.root.after(0, self._on_sm_event, ev))
        if self._pixel_monitor:
            eng.set_pixel_monitor(self._pixel_monitor)
        self._sm_instances[name] = eng
        self._sm_refresh_selector()
        self._sm_selector_var.set(name)
        self._on_sm_selector_change()
        self._sm_log(f"已创建状态机: {name}")

    def _sm_del_instance(self):
        """删除当前状态机实例。"""
        name = self._sm_active
        if not name:
            return
        if not messagebox.askyesno("确认删除", f"确定删除状态机「{name}」？"):
            return
        eng = self._sm_instances.pop(name, None)
        if eng and eng.running:
            eng.stop()
        # 删除关联规则文件
        import shutil
        for f in RULES_DIR.glob(f"{name}_*.json"):
            f.unlink(missing_ok=True)
        old_name = name
        self._sm_editing_state = ""
        if self._sm_instances:
            self._sm_active = list(self._sm_instances.keys())[0]
        else:
            self._sm_active = ""
        self._sm_refresh_selector()
        if self._sm_active:
            self._sm_selector_var.set(self._sm_active)
            self._on_sm_selector_change()
        else:
            self._sm_clear_editor()
        self._sm_log(f"已删除状态机: {old_name}")

    def _sm_clear_editor(self):
        """清空编辑区域。"""
        self._sm_name_var.set("未命名状态机")
        self._sm_tree.delete(*self._sm_tree.get_children())
        self._sm_cond_tree.delete(*self._sm_cond_tree.get_children())
        self._sm_enter_tree.delete(*self._sm_enter_tree.get_children())
        self._sm_exit_tree.delete(*self._sm_exit_tree.get_children())
        self._sm_editing_state = ""
        self._sm_edit_label.set("编辑状态: —")

    def _on_sm_selector_change(self, evt=None):
        """切换活跃状态机。"""
        new_name = self._sm_selector_var.get()
        if not new_name or new_name not in self._sm_instances:
            return
        # 保存旧状态机的当前编辑
        if self._sm_editing_state and self._sm_active and self._sm_active != new_name:
            self._sm_save_rule_for_state(self._sm_editing_state)
            self._sm_flush_state_by_name(self._sm_editing_state)
        self._sm_active = new_name
        eng = self._sm_instances[new_name]
        self._sm_apply_config(eng.config)
        self._sm_update_status()

    def _sm_toggle_all(self):
        """启动/停止所有状态机。"""
        any_running = any(e.running for e in self._sm_instances.values())
        if any_running:
            # 停止所有
            for name, eng in self._sm_instances.items():
                if eng.running:
                    eng.stop()
            self._sm_start_btn.configure(text="▶ 全部运行")
            self._sm_log("所有状态机已停止")
        else:
            # 启动所有
            if not self._sm_instances:
                self._sm_log("没有可运行的状态机")
                return
            # 保存当前编辑
            if self._sm_editing_state:
                self._sm_save_rule_for_state(self._sm_editing_state)
                self._sm_flush_state_by_name(self._sm_editing_state)
            for name, eng in self._sm_instances.items():
                # 同步规则文件到引擎
                self._sm_sync_engine_from_gui_for(name)
                if self._pixel_monitor:
                    eng.set_pixel_monitor(self._pixel_monitor)
                eng.set_jump_rules(load_all_jump_rules())
                eng.start()
            self._sm_start_btn.configure(text="■ 全部停止")
            self._sm_log(f"已启动 {len(self._sm_instances)} 个状态机")
            self._sm_update_status()

    # ── 状态机标签页 ────────────────────────────────────────

    def _build_sm_tab(self, parent: ttk.Frame):
        """构建状态机标签页：二级导航（编辑器 | 规则池）。"""
        # 二级导航 notebook
        self._sm_notebook = ttk.Notebook(parent)
        self._sm_notebook.pack(fill=tk.BOTH, expand=True)

        tab_editor = ttk.Frame(self._sm_notebook)
        self._sm_notebook.add(tab_editor, text="状态机编辑")
        self._build_sm_editor_tab(tab_editor)

        tab_rules = ttk.Frame(self._sm_notebook)
        self._sm_notebook.add(tab_rules, text="跳转规则管理")
        self._build_jump_rules_tab(tab_rules)

        # 切换标签页时刷新
        self._sm_notebook.bind("<<NotebookTabChanged>>", self._on_sm_subtab_change)

        # 尝试加载已有配置
        self._sm_load_config()

    def _on_sm_subtab_change(self, evt=None):
        """二级导航切换时刷新对应标签页。"""
        tab_idx = self._sm_notebook.index(self._sm_notebook.select())
        if tab_idx == 1:  # 跳转规则管理
            self._jr_refresh_list()
            if hasattr(self, '_jr_cond_region_combo'):
                self._jr_refresh_combos()
        elif tab_idx == 0:  # 状态机编辑
            if hasattr(self, '_sm_jump_rules_list'):
                self._sm_refresh_jump_rules_list()

    def _build_sm_editor_tab(self, parent: ttk.Frame):
        """状态机编辑器（原 _build_sm_tab 内容）。"""
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 6))

        # 状态机选择器
        ttk.Label(top, text="状态机").pack(side=tk.LEFT)
        self._sm_selector_var = tk.StringVar(value="")
        self._sm_selector_combo = ttk.Combobox(top, textvariable=self._sm_selector_var,
                                                width=16, state="readonly")
        self._sm_selector_combo.pack(side=tk.LEFT, padx=4)
        self._sm_selector_combo.bind("<<ComboboxSelected>>", self._on_sm_selector_change)
        ttk.Button(top, text="新建", width=3, command=self._sm_new_instance).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Button(top, text="删除", width=3, command=self._sm_del_instance).pack(side=tk.LEFT)

        ttk.Label(top, text="名称").pack(side=tk.LEFT)
        self._sm_name_var = tk.StringVar(value="未命名状态机")
        ttk.Entry(top, textvariable=self._sm_name_var, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text="间隔ms").pack(side=tk.LEFT, padx=(8, 0))
        self._sm_interval_var = tk.IntVar(value=500)
        ttk.Spinbox(top, textvariable=self._sm_interval_var, from_=100, to=10000, increment=100, width=5).pack(side=tk.LEFT, padx=4)
        self._sm_loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="循环", variable=self._sm_loop_var).pack(side=tk.LEFT, padx=8)

        self._sm_start_btn = ttk.Button(top, text="▶ 全部运行", command=self._sm_toggle_all)
        self._sm_start_btn.pack(side=tk.RIGHT, padx=(2, 0))
        ttk.Button(top, text="保存", command=self._sm_save).pack(side=tk.RIGHT, padx=(8, 2))
        ttk.Button(top, text="加载...", command=self._sm_load).pack(side=tk.RIGHT)
        ttk.Button(top, text="导出...", command=self._sm_export).pack(side=tk.RIGHT, padx=(8, 2))

        # ── 状态列表（左）+ 编辑（右）──
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # 左：状态列表
        st_frame = ttk.LabelFrame(paned, text="状态列表", padding="6")
        paned.add(st_frame, weight=1)
        st_cols = ("sname", "sconds", "sacts")
        self._sm_tree = ttk.Treeview(st_frame, columns=st_cols, show="headings", selectmode="browse", height=8)
        self._sm_tree.heading("sname", text="状态名")
        self._sm_tree.heading("sconds", text="条件")
        self._sm_tree.heading("sacts", text="动作")
        self._sm_tree.column("sname", width=80)
        self._sm_tree.column("sconds", width=40, anchor=tk.CENTER)
        self._sm_tree.column("sacts", width=40, anchor=tk.CENTER)
        self._sm_tree.pack(fill=tk.BOTH, expand=True)
        self._sm_tree.bind("<<TreeviewSelect>>", self._on_sm_state_select)

        sbtn = ttk.Frame(st_frame)
        sbtn.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(sbtn, text="新增状态", command=self._sm_add_state).pack(side=tk.LEFT)
        ttk.Button(sbtn, text="删除", command=self._sm_delete_state).pack(side=tk.LEFT, padx=4)
        ttk.Button(sbtn, text="▲", width=3, command=lambda: self._sm_move_state(-1)).pack(side=tk.RIGHT)
        ttk.Button(sbtn, text="▼", width=3, command=lambda: self._sm_move_state(1)).pack(side=tk.RIGHT, padx=2)

        # 设置初始状态
        ttk.Label(sbtn, text="初始:").pack(side=tk.LEFT, padx=(8, 2))
        self._sm_initial_var = tk.StringVar(value="")
        self._sm_initial_combo = ttk.Combobox(sbtn, textvariable=self._sm_initial_var, width=12, state="readonly")
        self._sm_initial_combo.pack(side=tk.LEFT)

        # 右：状态编辑
        ed_frame = ttk.LabelFrame(paned, text="状态编辑", padding="6")
        paned.add(ed_frame, weight=2)

        self._sm_edit_label = tk.StringVar(value="← 选中左侧状态后编辑")
        ttk.Label(ed_frame, textvariable=self._sm_edit_label, font=FONT_BOLD).pack(anchor=tk.W)

        # 状态名
        nr = ttk.Frame(ed_frame)
        nr.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(nr, text="状态名").pack(side=tk.LEFT)
        self._sm_state_name_var = tk.StringVar(value="")
        ttk.Entry(nr, textvariable=self._sm_state_name_var, width=16).pack(side=tk.LEFT, padx=4)
        ttk.Button(nr, text="应用名称", command=self._sm_apply_state_name).pack(side=tk.LEFT)

        # ── 转移规则（多分支）──
        cframe = ttk.LabelFrame(ed_frame, text="转移规则（按顺序检查，首个满足即跳转）", padding="4")
        cframe.pack(fill=tk.BOTH, expand=True, pady=(4, 2))

        # 规则选择栏
        trans_bar = ttk.Frame(cframe)
        trans_bar.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(trans_bar, text="规则").pack(side=tk.LEFT)
        self._sm_trans_var = tk.StringVar(value="规则1")
        self._sm_trans_combo = ttk.Combobox(trans_bar, textvariable=self._sm_trans_var, width=10, state="readonly")
        self._sm_trans_combo.pack(side=tk.LEFT, padx=2)
        self._sm_trans_combo.bind("<<ComboboxSelected>>", self._on_sm_trans_select)
        ttk.Button(trans_bar, text="＋规则", width=5, command=self._sm_add_transition).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(trans_bar, text="－规则", width=5, command=self._sm_del_transition).pack(side=tk.LEFT)
        ttk.Label(trans_bar, text="目标").pack(side=tk.LEFT, padx=(8, 0))
        self._sm_trans_target_var = tk.StringVar(value="")
        self._sm_trans_target_combo = ttk.Combobox(trans_bar, textvariable=self._sm_trans_target_var, width=10, state="readonly")
        self._sm_trans_target_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(trans_bar, text="💾 保存规则", width=9, command=self._sm_save_rule).pack(side=tk.LEFT, padx=(8, 0))

        # 条件列表
        ccols = ("cregion", "ccolor", "ctol", "cmode")
        self._sm_cond_tree = ttk.Treeview(cframe, columns=ccols, show="headings", selectmode="browse", height=3)
        self._sm_cond_tree.heading("cregion", text="监测区域")
        self._sm_cond_tree.heading("ccolor", text="颜色")
        self._sm_cond_tree.heading("ctol", text="容差")
        self._sm_cond_tree.heading("cmode", text="触发方式")
        self._sm_cond_tree.column("cregion", width=70)
        self._sm_cond_tree.column("ccolor", width=55, anchor=tk.CENTER)
        self._sm_cond_tree.column("ctol", width=35, anchor=tk.CENTER)
        self._sm_cond_tree.column("cmode", width=60, anchor=tk.CENTER)
        self._sm_cond_tree.pack(fill=tk.BOTH, expand=True)
        self._sm_cond_tree.bind("<Double-1>", self._sm_edit_condition_populate)
        # 当前编辑的条件索引（-1 = 新建模式）
        self._sm_editing_cond_idx: int = -1

        cbtn = ttk.Frame(cframe)
        cbtn.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(cbtn, text="区域").pack(side=tk.LEFT)
        self._sm_cond_region_var = tk.StringVar(value="")
        self._sm_cond_region_combo = ttk.Combobox(cbtn, textvariable=self._sm_cond_region_var, width=8, state="readonly")
        self._sm_cond_region_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(cbtn, text="颜色").pack(side=tk.LEFT, padx=(2, 0))
        self._sm_cond_color_var = tk.StringVar(value="#00FF00")
        ttk.Entry(cbtn, textvariable=self._sm_cond_color_var, width=7).pack(side=tk.LEFT, padx=2)
        self._sm_cond_swatch = tk.Canvas(cbtn, width=16, height=16, highlightthickness=1, highlightbackground="#999")
        self._sm_cond_swatch.pack(side=tk.LEFT)
        ttk.Button(cbtn, text="选色", width=4, command=self._sm_pick_color).pack(side=tk.LEFT, padx=1)
        ttk.Button(cbtn, text="吸色", width=4, command=self._sm_eyedropper).pack(side=tk.LEFT)
        ttk.Label(cbtn, text="容差").pack(side=tk.LEFT, padx=(2, 0))
        self._sm_cond_tol_var = tk.IntVar(value=20)
        ttk.Spinbox(cbtn, textvariable=self._sm_cond_tol_var, from_=0, to=200, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(cbtn, text="方式").pack(side=tk.LEFT, padx=(4, 0))
        self._sm_cond_mode_var = tk.StringVar(value="match")
        self._sm_cond_mode_combo = ttk.Combobox(cbtn, textvariable=self._sm_cond_mode_var,
                                                 values=["match", "changed"], width=7, state="readonly")
        self._sm_cond_mode_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(cbtn, text="+", width=3, command=self._sm_add_condition).pack(side=tk.LEFT, padx=(4, 2))
        self._sm_cond_upd_btn = ttk.Button(cbtn, text="更新", width=3, command=self._sm_update_condition, state=tk.DISABLED)
        self._sm_cond_upd_btn.pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(cbtn, text="-", width=3, command=self._sm_del_condition).pack(side=tk.LEFT)

        # ── 规则池引用（多选列表）──
        rpool_frame = ttk.LabelFrame(ed_frame, text="规则池引用（选中即生效，多选）", padding="4")
        rpool_frame.pack(fill=tk.X, pady=(4, 0))

        rpool_inner = ttk.Frame(rpool_frame)
        rpool_inner.pack(fill=tk.X)
        self._sm_jump_rules_list = tk.Listbox(rpool_inner, selectmode=tk.MULTIPLE, height=4,
                                               exportselection=False, font=FONT_SMALL)
        self._sm_jump_rules_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rpool_scroll = ttk.Scrollbar(rpool_inner, orient=tk.VERTICAL, command=self._sm_jump_rules_list.yview)
        self._sm_jump_rules_list.configure(yscrollcommand=rpool_scroll.set)
        rpool_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(rpool_frame, text="🔄 刷新规则池", command=self._sm_refresh_jump_rules_list).pack(anchor=tk.E, pady=(2, 0))

        # ── 云端触发 (cloud triggers) ──
        ct_frame = ttk.LabelFrame(ed_frame, text="云端触发 (收到消息时自动跳转)", padding="4")
        ct_frame.pack(fill=tk.X, pady=(4, 0))

        ct_bar = ttk.Frame(ct_frame)
        ct_bar.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(ct_bar, text="消息类型").pack(side=tk.LEFT)
        self._sm_ct_msg_var = tk.StringVar(value="print_task_a")
        self._sm_ct_msg_combo = ttk.Combobox(ct_bar, textvariable=self._sm_ct_msg_var,
                                              values=["print_task_a", "print_task_b", "print_task", "cancel", "restart", "ping"],
                                              width=14, state="readonly")
        self._sm_ct_msg_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(ct_bar, text="目标状态").pack(side=tk.LEFT, padx=(8, 0))
        self._sm_ct_target_var = tk.StringVar(value="")
        self._sm_ct_target_combo = ttk.Combobox(ct_bar, textvariable=self._sm_ct_target_var,
                                                 width=12, state="readonly")
        self._sm_ct_target_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(ct_bar, text="+", width=3, command=self._sm_add_cloud_trigger).pack(side=tk.LEFT, padx=(4, 2))
        self._sm_ct_upd_btn = ttk.Button(ct_bar, text="更新", width=3, command=self._sm_update_cloud_trigger,
                                          state=tk.DISABLED)
        self._sm_ct_upd_btn.pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(ct_bar, text="-", width=3, command=self._sm_del_cloud_trigger).pack(side=tk.LEFT)

        ct_cols = ("ct_msg", "ct_target")
        self._sm_ct_tree = ttk.Treeview(ct_frame, columns=ct_cols, show="headings", selectmode="browse", height=3)
        self._sm_ct_tree.heading("ct_msg", text="消息类型")
        self._sm_ct_tree.heading("ct_target", text="目标状态")
        self._sm_ct_tree.column("ct_msg", width=100)
        self._sm_ct_tree.column("ct_target", width=100)
        self._sm_ct_tree.pack(fill=tk.X)
        self._sm_ct_tree.bind("<<TreeviewSelect>>", self._on_sm_ct_select)
        self._sm_editing_ct_idx: int = -1

        # ── 云端操作 (cloud actions on enter/exit) ──
        ca_frame = ttk.LabelFrame(ed_frame, text="云端操作 (进入/退出状态时发送)", padding="4")
        ca_frame.pack(fill=tk.X, pady=(4, 0))

        # 进入时
        ca_enter_bar = ttk.Frame(ca_frame)
        ca_enter_bar.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(ca_enter_bar, text="进入时").pack(side=tk.LEFT)
        ttk.Label(ca_enter_bar, text="状态").pack(side=tk.LEFT, padx=(4, 0))
        self._sm_cae_status_var = tk.StringVar(value="idle")
        self._sm_cae_combo = ttk.Combobox(ca_enter_bar, textvariable=self._sm_cae_status_var,
                                           values=["idle", "printing", "faulted", "offline", "accepted", "completed",
                                                   "cancelled"], width=10, state="readonly")
        self._sm_cae_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(ca_enter_bar, text="消息").pack(side=tk.LEFT, padx=(4, 0))
        self._sm_cae_msg_var = tk.StringVar(value="")
        ttk.Entry(ca_enter_bar, textvariable=self._sm_cae_msg_var, width=16).pack(side=tk.LEFT, padx=2)
        ttk.Button(ca_enter_bar, text="+进入", width=5, command=self._sm_add_cloud_action_enter).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(ca_enter_bar, text="-", width=3, command=self._sm_del_cloud_action_enter).pack(side=tk.LEFT)

        cae_cols = ("cae_status", "cae_msg")
        self._sm_cae_tree = ttk.Treeview(ca_frame, columns=cae_cols, show="headings", selectmode="browse", height=2)
        self._sm_cae_tree.heading("cae_status", text="状态")
        self._sm_cae_tree.heading("cae_msg", text="消息")
        self._sm_cae_tree.column("cae_status", width=80, anchor=tk.CENTER)
        self._sm_cae_tree.column("cae_msg", width=180)
        self._sm_cae_tree.pack(fill=tk.X, pady=(0, 4))

        # 退出时
        ca_exit_bar = ttk.Frame(ca_frame)
        ca_exit_bar.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(ca_exit_bar, text="退出时").pack(side=tk.LEFT)
        ttk.Label(ca_exit_bar, text="状态").pack(side=tk.LEFT, padx=(4, 0))
        self._sm_cax_status_var = tk.StringVar(value="idle")
        self._sm_cax_combo = ttk.Combobox(ca_exit_bar, textvariable=self._sm_cax_status_var,
                                           values=["idle", "printing", "faulted", "offline", "accepted", "completed",
                                                   "cancelled"], width=10, state="readonly")
        self._sm_cax_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(ca_exit_bar, text="消息").pack(side=tk.LEFT, padx=(4, 0))
        self._sm_cax_msg_var = tk.StringVar(value="")
        ttk.Entry(ca_exit_bar, textvariable=self._sm_cax_msg_var, width=16).pack(side=tk.LEFT, padx=2)
        ttk.Button(ca_exit_bar, text="+退出", width=5, command=self._sm_add_cloud_action_exit).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(ca_exit_bar, text="-", width=3, command=self._sm_del_cloud_action_exit).pack(side=tk.LEFT)

        cax_cols = ("cax_status", "cax_msg")
        self._sm_cax_tree = ttk.Treeview(ca_frame, columns=cax_cols, show="headings", selectmode="browse", height=2)
        self._sm_cax_tree.heading("cax_status", text="状态")
        self._sm_cax_tree.heading("cax_msg", text="消息")
        self._sm_cax_tree.column("cax_status", width=80, anchor=tk.CENTER)
        self._sm_cax_tree.column("cax_msg", width=180)
        self._sm_cax_tree.pack(fill=tk.X)

        # 动作列表
        aframe = ttk.Frame(ed_frame)
        aframe.pack(fill=tk.BOTH, expand=True, pady=(2, 2))

        # 进入动作
        a1 = ttk.LabelFrame(aframe, text="进入动作 (on enter)", padding="4")
        a1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        acts1_cols = ("arec", "adelay")
        self._sm_enter_tree = ttk.Treeview(a1, columns=acts1_cols, show="headings", selectmode="browse", height=3)
        self._sm_enter_tree.heading("arec", text="录制")
        self._sm_enter_tree.heading("adelay", text="延时")
        self._sm_enter_tree.column("arec", width=100)
        self._sm_enter_tree.column("adelay", width=50, anchor=tk.CENTER)
        self._sm_enter_tree.pack(fill=tk.BOTH, expand=True)
        eb = ttk.Frame(a1)
        eb.pack(fill=tk.X, pady=(2, 0))
        self._sm_enter_rec_var = tk.StringVar(value="")
        self._sm_enter_combo = ttk.Combobox(eb, textvariable=self._sm_enter_rec_var, width=12, state="readonly")
        self._sm_enter_combo.pack(side=tk.LEFT)
        ttk.Button(eb, text="+", width=3, command=self._sm_add_enter_action).pack(side=tk.LEFT, padx=2)
        ttk.Button(eb, text="-", width=3, command=self._sm_del_enter_action).pack(side=tk.LEFT)

        # 退出动作
        a2 = ttk.LabelFrame(aframe, text="退出动作 (on exit)", padding="4")
        a2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        self._sm_exit_tree = ttk.Treeview(a2, columns=acts1_cols, show="headings", selectmode="browse", height=3)
        self._sm_exit_tree.heading("arec", text="录制")
        self._sm_exit_tree.heading("adelay", text="延时")
        self._sm_exit_tree.column("arec", width=100)
        self._sm_exit_tree.column("adelay", width=50, anchor=tk.CENTER)
        self._sm_exit_tree.pack(fill=tk.BOTH, expand=True)
        xb = ttk.Frame(a2)
        xb.pack(fill=tk.X, pady=(2, 0))
        self._sm_exit_rec_var = tk.StringVar(value="")
        self._sm_exit_combo = ttk.Combobox(xb, textvariable=self._sm_exit_rec_var, width=12, state="readonly")
        self._sm_exit_combo.pack(side=tk.LEFT)
        ttk.Button(xb, text="+", width=3, command=self._sm_add_exit_action).pack(side=tk.LEFT, padx=2)
        ttk.Button(xb, text="-", width=3, command=self._sm_del_exit_action).pack(side=tk.LEFT)

        # 超时设置 + 下一状态
        to_frame = ttk.Frame(ed_frame)
        to_frame.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(to_frame, text="超时(秒)").pack(side=tk.LEFT)
        self._sm_timeout_var = tk.DoubleVar(value=0.0)
        ttk.Spinbox(to_frame, textvariable=self._sm_timeout_var, from_=0, to=3600, increment=1, width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(to_frame, text="超时跳转").pack(side=tk.LEFT, padx=(8, 0))
        self._sm_timeout_jump_var = tk.StringVar(value="")
        self._sm_timeout_jump_combo = ttk.Combobox(to_frame, textvariable=self._sm_timeout_jump_var, width=10, state="readonly")
        self._sm_timeout_jump_combo.pack(side=tk.LEFT, padx=4)
        ttk.Label(to_frame, text="条件达成→").pack(side=tk.LEFT, padx=(12, 0))
        self._sm_next_state_var = tk.StringVar(value="")
        self._sm_next_state_combo = ttk.Combobox(to_frame, textvariable=self._sm_next_state_var, width=10, state="readonly")
        self._sm_next_state_combo.pack(side=tk.LEFT, padx=4)
        ttk.Label(to_frame, text="(空=顺序下一状态)", foreground="gray", font=FONT_SMALL).pack(side=tk.LEFT)

        # ── 流程图 ──
        flow_frame = ttk.LabelFrame(ed_frame, text="流程图", padding="4")
        flow_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        flow_canvas_frame = ttk.Frame(flow_frame)
        flow_canvas_frame.pack(fill=tk.BOTH, expand=True)
        self._sm_flow_canvas = tk.Canvas(flow_canvas_frame, bg="#FAFAFA", height=220, highlightthickness=0)
        flow_scroll_y = ttk.Scrollbar(flow_canvas_frame, orient=tk.VERTICAL, command=self._sm_flow_canvas.yview)
        flow_scroll_x = ttk.Scrollbar(flow_canvas_frame, orient=tk.HORIZONTAL, command=self._sm_flow_canvas.xview)
        self._sm_flow_canvas.configure(xscrollcommand=flow_scroll_x.set, yscrollcommand=flow_scroll_y.set)
        self._sm_flow_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        flow_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        flow_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(flow_frame, text="刷新流程图", command=self._sm_draw_flow).pack(anchor=tk.E, pady=(2, 0))

        # ── 运行状态 + 日志 ──
        log_frame = ttk.LabelFrame(parent, text="运行日志", padding="6")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self._sm_log_text = tk.Text(log_frame, height=4, wrap=tk.WORD, font=FONT_MONO, state=tk.DISABLED)
        sm_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self._sm_log_text.yview)
        self._sm_log_text.configure(yscrollcommand=sm_scroll.set)
        self._sm_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sm_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ── 跳转规则池界面（二级导航 Tab 2）─────────────────────────

    def _build_jump_rules_tab(self, parent: ttk.Frame):
        """独立规则池管理页面。"""

        # ── 顶部：规则选择 + 操作按钮 ──
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(top, text="规则池", font=FONT_BOLD).pack(side=tk.LEFT)
        ttk.Button(top, text="＋ 新建规则", width=10, command=self._jr_new).pack(side=tk.LEFT, padx=(8, 2))
        ttk.Button(top, text="－ 删除", width=6, command=self._jr_delete).pack(side=tk.LEFT)
        ttk.Button(top, text="💾 保存规则", width=10, command=self._jr_save).pack(side=tk.RIGHT, padx=(2, 0))
        ttk.Button(top, text="🔄 刷新", width=6, command=self._jr_refresh_list).pack(side=tk.RIGHT, padx=(2, 0))

        # ── 规则列表（左）+ 编辑器（右）──
        jr_pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        jr_pane.pack(fill=tk.BOTH, expand=True)

        # 左：规则列表
        list_frame = ttk.LabelFrame(jr_pane, text="规则列表", padding="4")
        jr_pane.add(list_frame, weight=1)
        jr_cols = ("jr_name", "jr_target", "jr_conds")
        self._jr_tree = ttk.Treeview(list_frame, columns=jr_cols, show="headings", selectmode="browse", height=12)
        self._jr_tree.heading("jr_name", text="规则名")
        self._jr_tree.heading("jr_target", text="目标状态机/状态")
        self._jr_tree.heading("jr_conds", text="条件数")
        self._jr_tree.column("jr_name", width=100)
        self._jr_tree.column("jr_target", width=120)
        self._jr_tree.column("jr_conds", width=50, anchor=tk.CENTER)
        self._jr_tree.pack(fill=tk.BOTH, expand=True)
        self._jr_tree.bind("<<TreeviewSelect>>", self._jr_on_select)

        # 右：规则编辑器
        ed_frame = ttk.LabelFrame(jr_pane, text="规则编辑", padding="6")
        jr_pane.add(ed_frame, weight=2)

        # 规则名称
        nr = ttk.Frame(ed_frame)
        nr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(nr, text="规则名称").pack(side=tk.LEFT)
        self._jr_name_var = tk.StringVar(value="")
        ttk.Entry(nr, textvariable=self._jr_name_var, width=18).pack(side=tk.LEFT, padx=4)

        # 目标选择
        tr = ttk.Frame(ed_frame)
        tr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(tr, text="目标状态机").pack(side=tk.LEFT)
        self._jr_target_sm_var = tk.StringVar(value="")
        self._jr_target_sm_combo = ttk.Combobox(tr, textvariable=self._jr_target_sm_var, width=12, state="readonly")
        self._jr_target_sm_combo.pack(side=tk.LEFT, padx=2)
        self._jr_target_sm_combo.bind("<<ComboboxSelected>>", self._jr_on_target_sm_change)
        ttk.Label(tr, text="目标状态").pack(side=tk.LEFT, padx=(8, 0))
        self._jr_target_state_var = tk.StringVar(value="")
        self._jr_target_state_combo = ttk.Combobox(tr, textvariable=self._jr_target_state_var, width=12, state="readonly")
        self._jr_target_state_combo.pack(side=tk.LEFT, padx=2)

        # 条件列表
        cframe = ttk.LabelFrame(ed_frame, text="条件（全部满足 → 跳转目标状态）", padding="4")
        cframe.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        ccols = ("cregion", "ccolor", "ctol", "cmode")
        self._jr_cond_tree = ttk.Treeview(cframe, columns=ccols, show="headings", selectmode="browse", height=4)
        self._jr_cond_tree.heading("cregion", text="监测区域")
        self._jr_cond_tree.heading("ccolor", text="颜色")
        self._jr_cond_tree.heading("ctol", text="容差")
        self._jr_cond_tree.heading("cmode", text="触发方式")
        self._jr_cond_tree.column("cregion", width=70)
        self._jr_cond_tree.column("ccolor", width=55, anchor=tk.CENTER)
        self._jr_cond_tree.column("ctol", width=35, anchor=tk.CENTER)
        self._jr_cond_tree.column("cmode", width=60, anchor=tk.CENTER)
        self._jr_cond_tree.pack(fill=tk.BOTH, expand=True)
        self._jr_cond_tree.bind("<Double-1>", self._jr_edit_condition_populate)
        self._jr_editing_cond_idx: int = -1

        cbtn = ttk.Frame(cframe)
        cbtn.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(cbtn, text="区域").pack(side=tk.LEFT)
        self._jr_cond_region_var = tk.StringVar(value="")
        self._jr_cond_region_combo = ttk.Combobox(cbtn, textvariable=self._jr_cond_region_var, width=8, state="readonly")
        self._jr_cond_region_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(cbtn, text="颜色").pack(side=tk.LEFT, padx=(2, 0))
        self._jr_cond_color_var = tk.StringVar(value="#00FF00")
        ttk.Entry(cbtn, textvariable=self._jr_cond_color_var, width=7).pack(side=tk.LEFT, padx=2)
        self._jr_cond_swatch = tk.Canvas(cbtn, width=16, height=16, highlightthickness=1, highlightbackground="#999")
        self._jr_cond_swatch.pack(side=tk.LEFT)
        ttk.Button(cbtn, text="选色", width=4, command=self._jr_pick_color).pack(side=tk.LEFT, padx=1)
        ttk.Button(cbtn, text="吸色", width=4, command=self._jr_eyedropper).pack(side=tk.LEFT)
        ttk.Label(cbtn, text="容差").pack(side=tk.LEFT, padx=(2, 0))
        self._jr_cond_tol_var = tk.IntVar(value=20)
        ttk.Spinbox(cbtn, textvariable=self._jr_cond_tol_var, from_=0, to=200, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(cbtn, text="方式").pack(side=tk.LEFT, padx=(4, 0))
        self._jr_cond_mode_var = tk.StringVar(value="match")
        self._jr_cond_mode_combo = ttk.Combobox(cbtn, textvariable=self._jr_cond_mode_var,
                                                  values=["match", "changed"], width=7, state="readonly")
        self._jr_cond_mode_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(cbtn, text="+", width=3, command=self._jr_add_condition).pack(side=tk.LEFT, padx=(4, 2))
        self._jr_cond_upd_btn = ttk.Button(cbtn, text="更新", width=3, command=self._jr_update_condition, state=tk.DISABLED)
        self._jr_cond_upd_btn.pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(cbtn, text="-", width=3, command=self._jr_del_condition).pack(side=tk.LEFT)

        # 说明
        hint = ttk.Label(ed_frame, text="提示：先在规则池创建规则 → 再在状态编辑页的多选列表中勾选要引用的规则",
                         foreground="gray", font=FONT_ITALIC)
        hint.pack(anchor=tk.W, pady=(4, 0))

        # 初始加载
        self._jr_refresh_list()

    # ── 跳转规则方法 ──────────────────────────────────────────

    def _jr_refresh_list(self):
        """刷新规则列表树。"""
        self._jr_tree.delete(*self._jr_tree.get_children())
        rules = load_all_jump_rules()
        for name, rule in rules.items():
            n_conds = len(rule.conditions)
            target = f"{rule.target_sm}/{rule.target_state}" if (rule.target_sm or rule.target_state) else "(未设置)"
            self._jr_tree.insert("", tk.END, values=(name, target, str(n_conds)))

    def _jr_on_select(self, evt=None):
        """选中规则 → 填充编辑器。"""
        sel = self._jr_tree.selection()
        if not sel:
            return
        name = self._jr_tree.item(sel[0], "values")[0]
        rules = load_all_jump_rules()
        rule = rules.get(name)
        if not rule:
            return

        self._jr_name_var.set(rule.name)
        self._jr_target_sm_var.set(rule.target_sm)
        self._jr_target_state_var.set(rule.target_state)

        # 刷新条件列表
        self._jr_cond_tree.delete(*self._jr_cond_tree.get_children())
        for c in rule.conditions:
            mode_label = "颜色一致" if c.mode == "match" else "变化后一致"
            self._jr_cond_tree.insert("", tk.END, values=(
                c.region_label, c.color, str(c.tolerance), mode_label))

        # 刷新区域 / SM / 状态下拉
        self._jr_refresh_combos()

    def _jr_refresh_combos(self):
        """刷新规则编辑器中的下拉列表。"""
        # 区域列表
        region_names = []
        if hasattr(self, '_pixel_monitor') and self._pixel_monitor:
            region_names = [r.label for r in self._pixel_monitor.regions if r.label]
        self._jr_cond_region_combo["values"] = region_names

        # 目标 SM 列表
        sm_names = list(self._sm_instances.keys())
        self._jr_target_sm_combo["values"] = sm_names

        # 目标状态列表（根据当前选中的 SM）
        target_sm = self._jr_target_sm_var.get()
        if target_sm and target_sm in self._sm_instances:
            eng = self._sm_instances[target_sm]
            state_names = [s.name for s in eng.config.states if s.name]
            self._jr_target_state_combo["values"] = state_names

    def _jr_on_target_sm_change(self, evt=None):
        """目标 SM 变化 → 更新状态列表。"""
        target_sm = self._jr_target_sm_var.get()
        if target_sm and target_sm in self._sm_instances:
            eng = self._sm_instances[target_sm]
            state_names = [s.name for s in eng.config.states if s.name]
            self._jr_target_state_combo["values"] = state_names
            if not self._jr_target_state_var.get():
                if state_names:
                    self._jr_target_state_var.set(state_names[0])
        else:
            self._jr_target_state_combo["values"] = []

    def _jr_new(self):
        """新建规则。"""
        name = simpledialog.askstring("新建跳转规则", "输入规则名称（唯一标识）：", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        # 检查是否重复
        rules = load_all_jump_rules()
        if name in rules:
            messagebox.showwarning("名称冲突", f"规则「{name}」已存在，请换一个名称。")
            return
        # 保存空规则
        rule = JumpRule(name=name)
        save_jump_rule(rule)
        self._jr_refresh_list()
        # 选中并编辑
        for item in self._jr_tree.get_children():
            if self._jr_tree.item(item, "values")[0] == name:
                self._jr_tree.selection_set(item)
                self._jr_on_select()
                break
        self._sm_log(f"已创建跳转规则: {name}")

    def _jr_save(self):
        """保存当前编辑的规则到文件。"""
        name = self._jr_name_var.get().strip()
        if not name:
            self._sm_log("错误: 规则名称不能为空")
            return

        conditions = []
        for item in self._jr_cond_tree.get_children():
            vs = self._jr_cond_tree.item(item, "values")
            mode_label = str(vs[3]) if len(vs) > 3 else "颜色一致"
            conditions.append(SMCondition(
                region_label=str(vs[0]),
                color=str(vs[1]),
                tolerance=int(vs[2]),
                enabled=True,
                mode="changed" if "变化" in mode_label else "match",
            ))

        rule = JumpRule(
            name=name,
            target_sm=self._jr_target_sm_var.get(),
            target_state=self._jr_target_state_var.get(),
            conditions=conditions,
            enabled=True,
        )
        save_jump_rule(rule)
        self._jr_refresh_list()
        # 保持选中
        for item in self._jr_tree.get_children():
            if self._jr_tree.item(item, "values")[0] == name:
                self._jr_tree.selection_set(item)
                break
        self._sm_log(f"跳转规则「{name}」已保存 ({len(conditions)} 个条件)")

    def _jr_delete(self):
        """删除选中的规则。"""
        sel = self._jr_tree.selection()
        if not sel:
            return
        name = self._jr_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno("确认删除", f"确实要删除跳转规则「{name}」吗？"):
            return
        delete_jump_rule(name)
        self._jr_refresh_list()
        self._jr_name_var.set("")
        self._jr_target_sm_var.set("")
        self._jr_target_state_var.set("")
        self._jr_cond_tree.delete(*self._jr_cond_tree.get_children())
        self._sm_log(f"已删除跳转规则: {name}")

    def _jr_add_condition(self):
        """给当前规则添加条件。"""
        region = self._jr_cond_region_var.get().strip()
        color = self._jr_cond_color_var.get().strip()
        tol = self._jr_cond_tol_var.get()
        mode = self._jr_cond_mode_var.get()
        mode_label = "颜色一致" if mode == "match" else "变化后一致"
        if not region:
            return
        self._jr_cond_tree.insert("", tk.END, values=(region, color, str(tol), mode_label))
        try:
            self._jr_cond_swatch.configure(bg=color)
        except Exception:
            pass

    def _jr_del_condition(self):
        """删除选中条件。"""
        sel = self._jr_cond_tree.selection()
        if sel:
            self._jr_cond_tree.delete(sel[0])
            self._jr_editing_cond_idx = -1
            self._jr_cond_upd_btn.configure(state=tk.DISABLED)

    def _jr_edit_condition_populate(self, evt=None):
        """双击条件 → 填入表单。"""
        sel = self._jr_cond_tree.selection()
        if not sel:
            return
        idx = self._jr_cond_tree.index(sel[0])
        vals = self._jr_cond_tree.item(sel[0], "values")
        if len(vals) >= 4:
            self._jr_cond_region_var.set(str(vals[0]))
            self._jr_cond_color_var.set(str(vals[1]))
            try:
                self._jr_cond_tol_var.set(int(vals[2]))
            except ValueError:
                self._jr_cond_tol_var.set(20)
            mode_label = str(vals[3])
            self._jr_cond_mode_var.set("changed" if "变化" in mode_label else "match")
            self._jr_editing_cond_idx = idx
            self._jr_cond_upd_btn.configure(state=tk.NORMAL)
            try:
                self._jr_cond_swatch.configure(bg=str(vals[1]))
            except Exception:
                pass

    def _jr_update_condition(self):
        """更新当前编辑的条件。"""
        idx = self._jr_editing_cond_idx
        if idx < 0:
            return
        children = self._jr_cond_tree.get_children()
        if idx >= len(children):
            return
        region = self._jr_cond_region_var.get().strip()
        color = self._jr_cond_color_var.get().strip()
        tol = self._jr_cond_tol_var.get()
        mode = self._jr_cond_mode_var.get()
        mode_label = "颜色一致" if mode == "match" else "变化后一致"
        if not region:
            return
        self._jr_cond_tree.item(children[idx], values=(region, color, str(tol), mode_label))
        self._jr_editing_cond_idx = -1
        self._jr_cond_upd_btn.configure(state=tk.DISABLED)
        self._sm_log(f"规则条件已更新: {region} {color}")

    def _jr_pick_color(self):
        """选色器。"""
        color = colorchooser.askcolor(color=self._jr_cond_color_var.get(), parent=self.root)
        if color[1]:
            self._jr_cond_color_var.set(color[1])
            try:
                self._jr_cond_swatch.configure(bg=color[1])
            except Exception:
                pass

    def _jr_eyedropper(self):
        """吸色：3秒后取鼠标位置颜色。"""
        self._jr_cond_swatch.configure(bg="#CCC")
        self.root.update()
        time.sleep(0.5)
        try:
            import pyautogui
            x, y = pyautogui.position()
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1]
                img = sct.grab({"left": x, "top": y, "width": 1, "height": 1, "mon": 1})
                r, g, b = img.pixel(0, 0)
                color = f"#{r:02x}{g:02x}{b:02x}"
                self._jr_cond_color_var.set(color)
                try:
                    self._jr_cond_swatch.configure(bg=color)
                except Exception:
                    pass
        except Exception as e:
            self._sm_log(f"吸色失败: {e}")
            self._jr_cond_swatch.configure(bg="#FFF")

    # ── 状态机方法 ────────────────────────────────────────────

    def _sm_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._sm_log_text.configure(state=tk.NORMAL)
        self._sm_log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self._sm_log_text.see(tk.END)
        self._sm_log_text.configure(state=tk.DISABLED)

    def _sm_get_config(self) -> SMConfig:
        """从 GUI 控件和规则文件构建完整 SMConfig。"""
        cfg = SMConfig()
        cfg.name = self._sm_name_var.get()
        cfg.initial_state = self._sm_initial_var.get()
        cfg.interval_ms = self._sm_interval_var.get()
        cfg.loop = self._sm_loop_var.get()

        self._sm_flush_current_state()

        for item in self._sm_tree.get_children():
            name = self._sm_tree.item(item, "values")[0]
            if self._sm_engine:
                existing = self._sm_engine.config.get_state(name)
                if existing:
                    self._sync_rules_from_files(existing)
                    cfg.states.append(existing)
                    continue
            cfg.states.append(SMState(name=name))
        return cfg

    def _sm_flush_current_state(self):
        """将 GUI 编辑器中的条件和动作写回当前选中状态的 SMState 对象。"""
        sel = self._sm_tree.selection()
        if not sel or not self._sm_engine:
            return
        name = sel[0]
        state = self._sm_engine.config.get_state(name)
        if not state:
            return
        state.name = self._sm_state_name_var.get().strip() or name
        state.timeout_s = self._sm_timeout_var.get()
        state.timeout_jump = self._sm_timeout_jump_var.get()
        state.next_state = self._sm_next_state_var.get()

        # 保存规则池引用
        state.jump_rules.clear()
        selected_indices = self._sm_jump_rules_list.curselection()
        for i in selected_indices:
            rule_name = self._sm_jump_rules_list.get(i)
            state.jump_rules.append(rule_name)

        # 只有无规则文件时才写入旧版 conditions（兼容过渡期）
        if not self._sm_rule_files_for_state(state.name or name):
            state.conditions.clear()
            for item in self._sm_cond_tree.get_children():
                vals = self._sm_cond_tree.item(item, "values")
                mode_label = str(vals[3]) if len(vals) > 3 else "颜色一致"
                mode = "changed" if "变化" in mode_label else "match"
                state.conditions.append(SMCondition(
                    region_label=str(vals[0]),
                    color=str(vals[1]),
                    tolerance=int(vals[2]),
                    mode=mode,
                ))

        # 进入动作
        state.on_enter.clear()
        for item in self._sm_enter_tree.get_children():
            vals = self._sm_enter_tree.item(item, "values")
            state.on_enter.append(SMAction(recording=str(vals[0])))

        # 退出动作
        state.on_exit.clear()
        for item in self._sm_exit_tree.get_children():
            vals = self._sm_exit_tree.item(item, "values")
            state.on_exit.append(SMAction(recording=str(vals[0])))

    def _sm_flush_state_by_name(self, name: str):
        """将 GUI 编辑器数据写回指定名称的状态对象（用于切换前保存上一状态）。"""
        if not name or not self._sm_engine:
            return
        state = self._sm_engine.config.get_state(name)
        if not state:
            return
        state.name = self._sm_state_name_var.get().strip() or name
        state.timeout_s = self._sm_timeout_var.get()
        state.timeout_jump = self._sm_timeout_jump_var.get()
        state.next_state = self._sm_next_state_var.get()
        # 保存规则池引用
        state.jump_rules.clear()
        selected_indices = self._sm_jump_rules_list.curselection()
        for i in selected_indices:
            rule_name = self._sm_jump_rules_list.get(i)
            state.jump_rules.append(rule_name)
        # 只有无规则文件时才写入旧版 conditions
        if not self._sm_rule_files_for_state(state.name):
            state.conditions.clear()
            for item in self._sm_cond_tree.get_children():
                vals = self._sm_cond_tree.item(item, "values")
                mode_label = str(vals[3]) if len(vals) > 3 else "颜色一致"
                state.conditions.append(SMCondition(
                    region_label=str(vals[0]), color=str(vals[1]), tolerance=int(vals[2]),
                    mode="changed" if "变化" in mode_label else "match",
                ))
        state.on_enter.clear()
        for item in self._sm_enter_tree.get_children():
            vals = self._sm_enter_tree.item(item, "values")
            state.on_enter.append(SMAction(recording=str(vals[0])))
        state.on_exit.clear()
        for item in self._sm_exit_tree.get_children():
            vals = self._sm_exit_tree.item(item, "values")
            state.on_exit.append(SMAction(recording=str(vals[0])))

    def _sm_sync_engine_from_gui(self):
        """将 GUI 控件数据同步到当前活跃状态机引擎（不写磁盘）。"""
        self._sm_sync_engine_from_gui_for(self._sm_active)

    def _sm_sync_engine_from_gui_for(self, name: str):
        """将 GUI 控件数据同步到指定名称的状态机引擎。"""
        if not name:
            return
        # 保存当前编辑
        if self._sm_editing_state:
            self._sm_save_rule_for_state(self._sm_editing_state)
            self._sm_flush_state_by_name(self._sm_editing_state)
        # 构建配置并更新引擎
        eng = self._sm_instances.get(name)
        if not eng:
            return
        cfg = self._sm_get_config()
        eng.config = cfg
        if self._pixel_monitor:
            eng.set_pixel_monitor(self._pixel_monitor)
        eng.set_jump_rules(load_all_jump_rules())

    def _sm_apply_config(self, cfg: SMConfig):
        """将配置加载到当前活跃状态机的编辑界面。"""
        self._sm_name_var.set(cfg.name)
        self._sm_interval_var.set(cfg.interval_ms)
        self._sm_loop_var.set(cfg.loop)
        self._sm_initial_var.set(cfg.initial_state)
        self._sm_tree.delete(*self._sm_tree.get_children())
        # 更新或创建活跃实例的引擎
        name = self._sm_active or cfg.name
        eng = StateMachineEngine(cfg)
        eng.set_replay_callback(self._replay_by_name)
        eng.set_event_callback(lambda ev: self.root.after(0, self._on_sm_event, ev))
        if self._pixel_monitor:
            eng.set_pixel_monitor(self._pixel_monitor)
        # 加载规则池
        eng.set_jump_rules(load_all_jump_rules())
        self._sm_instances[name] = eng
        if not self._sm_active and name:
            self._sm_active = name
            self._sm_refresh_selector()
        # 将所有状态的内联规则迁移到独立文件
        for s in cfg.states:
            self._sm_migrate_rules_to_files(s.name)
            n_conds = len(self._sm_rule_files_for_state(s.name))
            n_acts = len([a for a in s.on_enter if a.enabled]) + len([a for a in s.on_exit if a.enabled])
            self._sm_tree.insert("", tk.END, iid=s.name, values=(s.name, str(n_conds), str(n_acts)))
        self._sm_refresh_combos()

    def _sm_refresh_combos(self):
        names = self._sm_get_state_names()
        targets = names + ["STOP"]
        self._sm_initial_combo["values"] = names
        self._sm_timeout_jump_combo["values"] = names
        self._sm_next_state_combo["values"] = targets
        self._sm_trans_target_combo["values"] = targets
        self._sm_ct_target_combo["values"] = targets
        regions = [r.label for r in self._pixel_monitor.regions] if self._pixel_monitor else []
        self._sm_cond_region_combo["values"] = regions
        recs = [f.stem for f in sorted(RECORDINGS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)]
        for cb in [self._sm_enter_combo, self._sm_exit_combo]:
            cb["values"] = recs

    def _sm_get_state_names(self) -> list[str]:
        """获取当前状态机的所有状态名称列表。"""
        return [self._sm_tree.item(i, "values")[0] for i in self._sm_tree.get_children()]

    def _get_current_sm_state(self):
        """获取当前正在编辑的状态对象。"""
        if not self._sm_engine:
            return None
        name = self._sm_editing_state
        if not name:
            return None
        return self._sm_engine.config.get_state(name)

    def _sm_refresh_jump_rules_list(self):
        """刷新规则池引用列表（多选 listbox）。"""
        self._sm_jump_rules_list.delete(0, tk.END)
        rules = load_all_jump_rules()
        for name in sorted(rules.keys()):
            self._sm_jump_rules_list.insert(tk.END, name)

    def _sm_refresh_lists(self):
        """刷新录制列表和区域列表（新增录制/区域后使用）。"""
        self._sm_refresh_combos()
        n_recs = len(self._sm_enter_combo["values"])
        n_regions = len(self._sm_cond_region_combo["values"]) if self._pixel_monitor else 0
        self._sm_log(f"列表已刷新: {n_recs} 个录制, {n_regions} 个监测区域")

    def _sm_load_config(self):
        """启动时自动从默认路径加载状态机配置。"""
        try:
            eng = StateMachineEngine.load_config()
            if not eng.config.states:
                return  # 空配置，跳过
            name = eng.config.name
            eng.set_replay_callback(self._replay_by_name)
            eng.set_event_callback(lambda ev: self.root.after(0, self._on_sm_event, ev))
            if self._pixel_monitor:
                eng.set_pixel_monitor(self._pixel_monitor)
            self._sm_instances[name] = eng
            self._sm_refresh_selector()
            self._sm_selector_var.set(name)
            self._on_sm_selector_change()
            self._sm_log(f"已加载状态机「{name}」({len(eng.config.states)} 个状态)")
        except Exception:
            pass  # 文件不存在或格式错误，静默跳过

    def _sm_load_config_files(self):
        """从文件对话框加载状态机配置文件（支持多选）。"""
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            parent=self.root,
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            title="选择状态机配置文件（可多选）",
        )
        if not paths:
            return
        for p in paths:
            try:
                eng = StateMachineEngine.load_config(Path(p))
                name = eng.config.name
                if name in self._sm_instances:
                    name = f"{name}_{len(self._sm_instances)}"
                    eng.config.name = name
                eng.set_replay_callback(self._replay_by_name)
                eng.set_event_callback(lambda ev: self.root.after(0, self._on_sm_event, ev))
                if self._pixel_monitor:
                    eng.set_pixel_monitor(self._pixel_monitor)
                self._sm_instances[name] = eng
                self._sm_log(f"已加载: {name} ({len(eng.config.states)} 个状态)")
            except Exception as e:
                self._sm_log(f"加载失败: {p} — {e}")
        self._sm_refresh_selector()
        if self._sm_instances:
            last_name = list(self._sm_instances.keys())[-1]
            self._sm_selector_var.set(last_name)
            self._on_sm_selector_change()

    def _sm_save(self):
        """保存当前活跃状态机配置。"""
        if self._sm_editing_state:
            self._sm_save_rule_for_state(self._sm_editing_state)
            self._sm_flush_state_by_name(self._sm_editing_state)
        if not self._sm_active:
            return
        cfg = self._sm_get_config()
        eng = StateMachineEngine(cfg)
        eng.set_replay_callback(self._replay_by_name)
        eng.set_event_callback(lambda ev: self.root.after(0, self._on_sm_event, ev))
        if self._pixel_monitor:
            eng.set_pixel_monitor(self._pixel_monitor)
        eng.save_config()  # 保存到 state_machine.json
        self._sm_instances[self._sm_active] = eng
        self._sm_log(f"已保存状态机「{cfg.name}」")

    def _sm_load(self):
        self._sm_load_config_files()

    def _sm_export(self):
        """导出状态机配置到用户指定路径。"""
        if self._sm_editing_state:
            self._sm_save_rule_for_state(self._sm_editing_state)
            self._sm_flush_state_by_name(self._sm_editing_state)
        cfg = self._sm_get_config()
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialfile=f"{cfg.name or 'state_machine'}.json",
        )
        if not path:
            return
        engine = StateMachineEngine(cfg)
        engine.save_config(Path(path))
        self._sm_log(f"已导出到: {path}")

    def _sm_toggle(self):
        """切换当前活跃状态机的运行/停止。"""
        if self._sm_engine and self._sm_engine.running:
            self._sm_engine.stop()
            self._sm_log(f"状态机「{self._sm_active}」已停止")
        else:
            if self._sm_editing_state:
                self._sm_save_rule_for_state(self._sm_editing_state)
                self._sm_flush_state_by_name(self._sm_editing_state)
            self._sm_sync_engine_from_gui()
            if self._pixel_monitor:
                self._sm_engine.set_pixel_monitor(self._pixel_monitor)
            self._sm_engine.start()
            self._sm_log(f"状态机「{self._sm_active}」已启动 → 初始状态: {self._sm_engine.current_state}")
        self._sm_update_status()

    def _sm_pause(self):
        if not self._sm_engine:
            return
        if self._sm_engine._paused:
            self._sm_engine.resume()
            self._sm_log("状态机继续运行")
        else:
            self._sm_engine.pause()
            self._sm_pause_btn.configure(text="▶ 继续")
            self._sm_log("状态机已暂停")

    def _sm_step(self):
        if not self._sm_engine:
            return
        self._sm_engine.step_once()
        self._sm_pause_btn.configure(text="▶ 继续")
        self._sm_log(f"单步执行 → 当前状态: {self._sm_engine.current_state}")

    def _sm_update_status(self):
        """更新状态机运行状态显示（显示所有实例状态）。"""
        running = [name for name, eng in self._sm_instances.items() if eng.running]
        any_running = bool(running)
        self._sm_start_btn.configure(text="■ 全部停止" if any_running else "▶ 全部运行")
        # 更新按钮状态
        active_eng = self._sm_engine
        if active_eng and active_eng.running:
            if active_eng._paused:
                self._sm_pause_btn.configure(text="▶ 继续", state=tk.NORMAL)
            else:
                self._sm_pause_btn.configure(text="⏸ 暂停", state=tk.NORMAL)
            self._sm_step_btn.configure(state=tk.NORMAL)
        else:
            self._sm_pause_btn.configure(state=tk.DISABLED)
            self._sm_step_btn.configure(state=tk.DISABLED)
        # 高亮活跃状态机的当前状态
        if active_eng and active_eng.running:
            snap = active_eng.get_status_snapshot()
            for item in self._sm_tree.get_children():
                if item == snap["current_state"]:
                    self._sm_tree.selection_set(item)
                self._sm_tree.tag_configure("active_state", background="#C8E6C9")
                self._sm_tree.tag_configure("inactive_state", background="")
            for i in self._sm_tree.get_children():
                self._sm_tree.item(i, tags=("active_state",) if i == snap["current_state"] else ("inactive_state",))
        self.root.after(500, self._sm_update_status)

    def _on_sm_event(self, event):
        ts = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
        msg = event.message or f"{event.type}: {event.state.name}"
        self._sm_log(f"{ts} [{event.type}] {msg}")

    def _on_sm_state_select(self, evt):
        # 先保存上一个状态的当前规则和编辑内容
        prev_state = self._sm_editing_state
        new_sel = self._sm_tree.selection()
        if prev_state and prev_state != (new_sel or [None])[0]:
            self._sm_save_rule_for_state(prev_state)
            self._sm_flush_state_by_name(prev_state)
        sel = new_sel
        if not sel or not self._sm_engine:
            self._sm_editing_state = ""
            return
        name = sel[0]
        self._sm_editing_state = name
        self._sm_edit_label.set(f"编辑状态: {name}")
        state = self._sm_engine.config.get_state(name)
        if not state:
            return
        self._sm_state_name_var.set(state.name)

        # 转移规则 — 优先从文件加载，文件不存在则迁移旧数据
        self._sm_migrate_rules_to_files(name)
        rule_files = self._sm_rule_files_for_state(name)
        self._sm_building = True
        if rule_files:
            trans_names = [f"规则{i}" for i in range(1, len(rule_files) + 1)]
            self._sm_trans_combo["values"] = trans_names
            self._sm_trans_var.set(trans_names[0])
        else:
            self._sm_trans_combo["values"] = []
            self._sm_trans_var.set("")
            self._sm_trans_target_var.set("")
            self._sm_cond_tree.delete(*self._sm_cond_tree.get_children())
        self._sm_building = False
        if rule_files:
            self._sm_load_rule(name, 0)

        # 进入动作
        self._sm_enter_tree.delete(*self._sm_enter_tree.get_children())
        for a in state.on_enter:
            dl = f"{a.delay_before:.1f}s" if a.delay_before else ""
            self._sm_enter_tree.insert("", tk.END, values=(a.recording, dl))

        # 退出动作
        self._sm_exit_tree.delete(*self._sm_exit_tree.get_children())
        for a in state.on_exit:
            dl = f"{a.delay_before:.1f}s" if a.delay_before else ""
            self._sm_exit_tree.insert("", tk.END, values=(a.recording, dl))

        self._sm_timeout_var.set(state.timeout_s)
        self._sm_timeout_jump_var.set(state.timeout_jump)
        self._sm_next_state_var.set(state.next_state)

        # 规则池引用 — 刷新列表并勾选当前状态引用的规则
        self._sm_refresh_jump_rules_list()
        for i in range(self._sm_jump_rules_list.size()):
            rule_name = self._sm_jump_rules_list.get(i)
            if rule_name in state.jump_rules:
                self._sm_jump_rules_list.selection_set(i)

        self._sm_refresh_combos()

        # ── 云端触发/操作列表 ──
        self._sm_refresh_ct_tree(state)
        self._sm_refresh_cae_tree(state)
        self._sm_refresh_cax_tree(state)
        self._sm_draw_flow()

    def _sm_add_state(self):
        name = f"state_{len(self._sm_tree.get_children()) + 1}"
        self._sm_tree.insert("", tk.END, iid=name, values=(name, "0", "0"))
        if not self._sm_initial_var.get():
            self._sm_initial_var.set(name)
        self._sm_refresh_combos()
        self._sm_log(f"新增状态: {name}")

    def _sm_delete_state(self):
        sel = self._sm_tree.selection()
        if not sel:
            return
        name = sel[0]
        self._sm_tree.delete(name)
        if self._sm_initial_var.get() == name:
            self._sm_initial_var.set("")
        # 清除该状态的所有规则文件
        for f in self._sm_rule_files_for_state(name):
            f.unlink(missing_ok=True)
        self._sm_cond_tree.delete(*self._sm_cond_tree.get_children())
        self._sm_enter_tree.delete(*self._sm_enter_tree.get_children())
        self._sm_exit_tree.delete(*self._sm_exit_tree.get_children())
        self._sm_refresh_combos()
        self._sm_log(f"已删除状态: {name}")

    def _sm_move_state(self, direction: int):
        sel = self._sm_tree.selection()
        if not sel:
            return
        name = sel[0]
        items = self._sm_tree.get_children()
        idx = items.index(name)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(items):
            return
        # Reorder by re-inserting
        vals = self._sm_tree.item(name, "values")
        self._sm_tree.delete(name)
        target = items[new_idx] if direction > 0 else items[new_idx]
        pos = "after" if direction > 0 else "before"
        # Simple approach: rebuild tree
        cur_items = list(self._sm_tree.get_children())
        cur_items.remove(target if direction > 0 else items[new_idx])
        if direction < 0:
            cur_items.insert(new_idx, name)
        else:
            target_idx = cur_items.index(target) + 1
            cur_items.insert(target_idx, name)
        self._sm_tree.delete(*self._sm_tree.get_children())
        for i, item in enumerate(cur_items):
            if item == name:
                self._sm_tree.insert("", tk.END, iid=name, values=vals)
            else:
                old_vals = self._sm_tree.item(item, "values") if self._sm_tree.exists(item) else ("", "0", "0")
                self._sm_tree.insert("", tk.END, iid=item, values=old_vals)
        self._sm_tree.selection_set(name)
        self._sm_refresh_combos()

    # ── 云端触发操作 ─────────────────────────────────────────

    def _sm_add_cloud_trigger(self):
        """添加云端触发到当前状态。"""
        msg_type = self._sm_ct_msg_var.get().strip()
        target = self._sm_ct_target_var.get().strip()
        if not msg_type or not target:
            messagebox.showwarning("提示", "消息类型和目标状态不能为空")
            return

        sel = self._sm_tree.selection()
        if not sel:
            return
        state = self._get_current_sm_state()
        if not state:
            return
        self._sm_save_rule_for_state(state.name)

        # 检查重复
        for ct in state.cloud_triggers:
            if ct.message_type == msg_type and ct.target_state == target:
                messagebox.showinfo("提示", "该云端触发已存在")
                return

        state.cloud_triggers.append(SMCloudTrigger(message_type=msg_type, target_state=target))
        self._sm_refresh_ct_tree(state)

    def _sm_update_cloud_trigger(self):
        """更新选中的云端触发。"""
        sel = self._sm_tree.selection()
        if not sel or self._sm_editing_ct_idx < 0:
            return
        state = self._get_current_sm_state()
        if not state or self._sm_editing_ct_idx >= len(state.cloud_triggers):
            return
        self._sm_save_rule_for_state(state.name)

        msg_type = self._sm_ct_msg_var.get().strip()
        target = self._sm_ct_target_var.get().strip()
        if not msg_type or not target:
            return
        state.cloud_triggers[self._sm_editing_ct_idx] = SMCloudTrigger(
            message_type=msg_type, target_state=target)
        self._sm_refresh_ct_tree(state)
        self._sm_editing_ct_idx = -1
        self._sm_ct_upd_btn.configure(state=tk.DISABLED)

    def _sm_del_cloud_trigger(self):
        """删除选中的云端触发。"""
        sel = self._sm_tree.selection()
        if not sel:
            return
        ct_sel = self._sm_ct_tree.selection()
        if not ct_sel:
            return
        state = self._get_current_sm_state()
        if not state:
            return
        self._sm_save_rule_for_state(state.name)

        try:
            idx = int(ct_sel[0])
        except ValueError:
            return
        if 0 <= idx < len(state.cloud_triggers):
            state.cloud_triggers.pop(idx)
        self._sm_refresh_ct_tree(state)
        self._sm_editing_ct_idx = -1
        self._sm_ct_upd_btn.configure(state=tk.DISABLED)

    def _on_sm_ct_select(self, evt=None):
        """选中云端触发行时回填编辑表单。"""
        ct_sel = self._sm_ct_tree.selection()
        if not ct_sel:
            self._sm_editing_ct_idx = -1
            self._sm_ct_upd_btn.configure(state=tk.DISABLED)
            return
        state = self._get_current_sm_state()
        if not state:
            return
        try:
            idx = int(ct_sel[0])
        except ValueError:
            return
        if 0 <= idx < len(state.cloud_triggers):
            ct = state.cloud_triggers[idx]
            self._sm_editing_ct_idx = idx
            self._sm_ct_msg_var.set(ct.message_type)
            self._sm_ct_target_var.set(ct.target_state)
            self._sm_ct_upd_btn.configure(state=tk.NORMAL)
        else:
            self._sm_editing_ct_idx = -1
            self._sm_ct_upd_btn.configure(state=tk.DISABLED)

    def _sm_refresh_ct_tree(self, state):
        """刷新云端触发列表。"""
        self._sm_ct_tree.delete(*self._sm_ct_tree.get_children())
        for i, ct in enumerate(state.cloud_triggers):
            self._sm_ct_tree.insert("", tk.END, iid=str(i),
                                     values=(ct.message_type, ct.target_state))

    def _sm_refresh_ct_target_combo(self):
        """刷新云端触发目标状态下拉列表。"""
        names = self._sm_get_state_names()
        self._sm_ct_target_combo["values"] = names

    # ── 云端操作 (enter) ──────────────────────────────────────

    def _sm_add_cloud_action_enter(self):
        """添加进入时云端操作。"""
        state = self._get_current_sm_state()
        if not state:
            return
        self._sm_save_rule_for_state(state.name)
        status = self._sm_cae_status_var.get().strip()
        message = self._sm_cae_msg_var.get().strip()
        if not status:
            return
        state.cloud_actions_on_enter.append(
            SMCloudAction(status=status, message=message))
        self._sm_refresh_cae_tree(state)

    def _sm_del_cloud_action_enter(self):
        """删除选中的进入时云端操作。"""
        state = self._get_current_sm_state()
        if not state:
            return
        cae_sel = self._sm_cae_tree.selection()
        if not cae_sel:
            return
        self._sm_save_rule_for_state(state.name)
        try:
            idx = int(cae_sel[0])
        except ValueError:
            return
        if 0 <= idx < len(state.cloud_actions_on_enter):
            state.cloud_actions_on_enter.pop(idx)
        self._sm_refresh_cae_tree(state)

    def _sm_refresh_cae_tree(self, state):
        """刷新进入时云端操作列表。"""
        self._sm_cae_tree.delete(*self._sm_cae_tree.get_children())
        for i, ca in enumerate(state.cloud_actions_on_enter):
            self._sm_cae_tree.insert("", tk.END, iid=str(i),
                                      values=(ca.status, ca.message or "—"))

    # ── 云端操作 (exit) ───────────────────────────────────────

    def _sm_add_cloud_action_exit(self):
        """添加退出时云端操作。"""
        state = self._get_current_sm_state()
        if not state:
            return
        self._sm_save_rule_for_state(state.name)
        status = self._sm_cax_status_var.get().strip()
        message = self._sm_cax_msg_var.get().strip()
        if not status:
            return
        state.cloud_actions_on_exit.append(
            SMCloudAction(status=status, message=message))
        self._sm_refresh_cax_tree(state)

    def _sm_del_cloud_action_exit(self):
        """删除选中的退出时云端操作。"""
        state = self._get_current_sm_state()
        if not state:
            return
        cax_sel = self._sm_cax_tree.selection()
        if not cax_sel:
            return
        self._sm_save_rule_for_state(state.name)
        try:
            idx = int(cax_sel[0])
        except ValueError:
            return
        if 0 <= idx < len(state.cloud_actions_on_exit):
            state.cloud_actions_on_exit.pop(idx)
        self._sm_refresh_cax_tree(state)

    def _sm_refresh_cax_tree(self, state):
        """刷新退出时云端操作列表。"""
        self._sm_cax_tree.delete(*self._sm_cax_tree.get_children())
        for i, ca in enumerate(state.cloud_actions_on_exit):
            self._sm_cax_tree.insert("", tk.END, iid=str(i),
                                      values=(ca.status, ca.message or "—"))

    def _sm_apply_state_name(self):
        sel = self._sm_tree.selection()
        if not sel:
            return
        old_name = sel[0]
        new_name = self._sm_state_name_var.get().strip()
        if not new_name or new_name == old_name:
            return
        # 重命名规则文件
        for old_path in self._sm_rule_files_for_state(old_name):
            idx = int(old_path.stem.rsplit('_', 1)[1])
            new_path = self._sm_rule_path(new_name, idx)
            old_path.rename(new_path)
            try:
                d = json.loads(new_path.read_text(encoding="utf-8"))
                d["state"] = new_name
                new_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        # Update tree item
        vals = list(self._sm_tree.item(old_name, "values"))
        vals[0] = new_name
        self._sm_tree.delete(old_name)
        self._sm_tree.insert("", tk.END, iid=new_name, values=vals)
        self._sm_tree.selection_set(new_name)
        self._sm_editing_state = new_name
        self._sm_refresh_combos()
        self._sm_log(f"状态改名: {old_name} → {new_name}")

    def _sm_pick_color(self):
        """状态机条件：系统色盘选色。"""
        result = colorchooser.askcolor(color=self._sm_cond_color_var.get(), title="选择条件颜色")
        if result and result[1]:
            self._sm_cond_color_var.set(result[1])
            try:
                self._sm_cond_swatch.configure(bg=result[1])
            except Exception:
                pass

    def _sm_eyedropper(self):
        """状态机条件：屏幕吸色工具。"""
        if not hasattr(self, '_sm_cond_color_var'):
            return

        _orig_color = self._sm_cond_color_var.get()

        overlay = tk.Toplevel(self.root)
        overlay.attributes('-fullscreen', True)
        overlay.attributes('-alpha', 0.001)
        overlay.attributes('-topmost', True)
        overlay.attributes('-toolwindow', True)
        overlay.configure(cursor='crosshair', bg='black')
        overlay.grab_set()
        overlay.focus_force()

        preview = tk.Toplevel(overlay)
        preview.attributes('-topmost', True)
        preview.attributes('-toolwindow', True)
        preview.overrideredirect(True)
        preview_label = tk.Label(preview, text="", width=8, font=FONT_MONO_SM,
                                 relief=tk.SOLID, borderwidth=1)
        preview_label.pack()
        preview.withdraw()

        _last_update: list[float] = [0.0]

        def _cleanup(restore=False):
            try:
                preview.destroy()
                overlay.grab_release()
                overlay.destroy()
            except Exception:
                pass
            if restore:
                self._sm_cond_color_var.set(_orig_color)

        def _pick_color_at(evt):
            now = time.time()
            if now - _last_update[0] < 0.05:
                return
            _last_update[0] = now
            x, y = evt.x_root, evt.y_root
            try:
                from pixel_monitor import _capture_region_rgb
                data = _capture_region_rgb(x, y, x + 1, y + 1)
                if data and len(data) >= 3:
                    r, g, b = data[0], data[1], data[2]
                    hex_c = f"#{r:02X}{g:02X}{b:02X}"
                    self._sm_cond_color_var.set(hex_c)
                    preview_label.configure(text=f" {hex_c} ", bg=hex_c)
                    bright = (r * 299 + g * 587 + b * 114) / 1000
                    preview_label.configure(fg="black" if bright > 128 else "white")
                    preview.geometry(f"+{x+16}+{y+16}")
                    preview.deiconify()
                    preview.lift()
                    try:
                        self._sm_cond_swatch.configure(bg=hex_c)
                    except Exception:
                        pass
            except Exception:
                pass

        overlay.bind('<Button-1>', _pick_color_at)
        overlay.bind('<B1-Motion>', _pick_color_at)
        overlay.bind('<ButtonRelease-1>', lambda e: _cleanup(restore=False))
        overlay.bind('<Escape>', lambda _e: _cleanup(restore=True))

    def _sm_add_condition(self):
        sel = self._sm_tree.selection()
        if not sel:
            return
        region = self._sm_cond_region_var.get().strip()
        color = self._sm_cond_color_var.get().strip()
        tol = self._sm_cond_tol_var.get()
        mode = self._sm_cond_mode_var.get()
        mode_label = "颜色一致" if mode == "match" else "变化后一致"
        if not region:
            return
        self._sm_cond_tree.insert("", tk.END, values=(region, color, str(tol), mode_label))
        # 更新色块
        try:
            self._sm_cond_swatch.configure(bg=color)
        except Exception:
            pass

    def _sm_del_condition(self):
        sel = self._sm_cond_tree.selection()
        if sel:
            self._sm_cond_tree.delete(sel[0])
            self._sm_editing_cond_idx = -1
            self._sm_cond_upd_btn.configure(state=tk.DISABLED)

    def _sm_edit_condition_populate(self, evt=None):
        """双击条件行 → 将条件值填入编辑表单。"""
        sel = self._sm_cond_tree.selection()
        if not sel:
            return
        idx = self._sm_cond_tree.index(sel[0])
        vals = self._sm_cond_tree.item(sel[0], "values")
        if len(vals) >= 4:
            self._sm_cond_region_var.set(str(vals[0]))
            self._sm_cond_color_var.set(str(vals[1]))
            try:
                self._sm_cond_tol_var.set(int(vals[2]))
            except ValueError:
                self._sm_cond_tol_var.set(20)
            mode_label = str(vals[3])
            self._sm_cond_mode_var.set("changed" if "变化" in mode_label else "match")
            self._sm_editing_cond_idx = idx
            self._sm_cond_upd_btn.configure(state=tk.NORMAL)
            # 更新色块
            try:
                self._sm_cond_swatch.configure(bg=str(vals[1]))
            except Exception:
                pass

    def _sm_update_condition(self):
        """更新当前编辑的条件。"""
        idx = self._sm_editing_cond_idx
        if idx < 0:
            return
        children = self._sm_cond_tree.get_children()
        if idx >= len(children):
            return
        region = self._sm_cond_region_var.get().strip()
        color = self._sm_cond_color_var.get().strip()
        tol = self._sm_cond_tol_var.get()
        mode = self._sm_cond_mode_var.get()
        mode_label = "颜色一致" if mode == "match" else "变化后一致"
        if not region:
            return
        self._sm_cond_tree.item(children[idx], values=(region, color, str(tol), mode_label))
        self._sm_editing_cond_idx = -1
        self._sm_cond_upd_btn.configure(state=tk.DISABLED)
        self._sm_log(f"条件已更新: {region} {color}")

    # ── 转移规则管理（文件存储 — 每个规则独立 JSON，杜绝耦合）──

    def _sm_rule_path(self, state_name: str, idx: int) -> Path:
        return RULES_DIR / f"{state_name}_{idx}.json"

    def _sm_rule_files_for_state(self, state_name: str) -> list[Path]:
        if not state_name:
            return []
        return sorted(RULES_DIR.glob(f"{state_name}_*.json"),
                       key=lambda p: int(p.stem.rsplit('_', 1)[1]))

    def _sm_save_rule_for_state(self, state_name: str):
        """将 GUI 条件树写入规则文件。"""
        if not state_name:
            return
        cur = self._sm_trans_var.get()
        vals = list(self._sm_trans_combo["values"])
        if cur not in vals:
            return
        idx = vals.index(cur)
        conditions = []
        for item in self._sm_cond_tree.get_children():
            vs = self._sm_cond_tree.item(item, "values")
            mode_label = str(vs[3]) if len(vs) > 3 else "颜色一致"
            conditions.append({
                "region_label": str(vs[0]),
                "color": str(vs[1]),
                "tolerance": int(vs[2]),
                "mode": "changed" if "变化" in mode_label else "match",
                "enabled": True,
            })
        data = {
            "state": state_name, "index": idx,
            "target": self._sm_trans_target_var.get(),
            "enabled": True, "conditions": conditions,
        }
        self._sm_rule_path(state_name, idx).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sm_save_rule(self):
        """保存当前规则按钮回调。"""
        sel = self._sm_tree.selection()
        if not sel:
            return
        self._sm_save_rule_for_state(sel[0])
        n_conds = len(self._sm_cond_tree.get_children())
        self._sm_tree.set(sel[0], "sconds", str(n_conds))
        self._sm_log(f"规则已保存 ({n_conds} 个条件)")

    def _sm_migrate_rules_to_files(self, state_name: str):
        """一次性：将引擎内存中的旧规则迁移到文件（文件存在则跳过）。"""
        if not self._sm_engine:
            return
        state = self._sm_engine.config.get_state(state_name)
        if not state:
            return
        if self._sm_rule_files_for_state(state_name):
            return  # 已有文件，不覆盖

        sources: list[tuple[str, list]] = []
        if state.transitions:
            for tr in state.transitions:
                sources.append((tr.target, [
                    {"region_label": c.region_label, "color": c.color,
                     "tolerance": c.tolerance, "mode": c.mode, "enabled": c.enabled}
                    for c in tr.conditions
                ]))
        elif state.conditions:
            sources.append((state.next_state, [
                {"region_label": c.region_label, "color": c.color,
                 "tolerance": c.tolerance, "mode": c.mode, "enabled": c.enabled}
                for c in state.conditions
            ]))

        for idx, (target, conds) in enumerate(sources):
            data = {"state": state_name, "index": idx, "target": target,
                    "enabled": True, "conditions": conds}
            self._sm_rule_path(state_name, idx).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sm_add_transition(self):
        """添加转移规则 → 新建空文件。"""
        sel = self._sm_tree.selection()
        if not sel:
            return
        state_name = sel[0]
        self._sm_save_rule_for_state(state_name)
        self._sm_migrate_rules_to_files(state_name)

        existing = self._sm_rule_files_for_state(state_name)
        next_idx = len(existing)
        data = {"state": state_name, "index": next_idx,
                "target": "", "enabled": True, "conditions": []}
        self._sm_rule_path(state_name, next_idx).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        trans_names = [f"规则{i}" for i in range(1, next_idx + 2)]
        self._sm_building = True
        self._sm_cond_tree.delete(*self._sm_cond_tree.get_children())
        self._sm_trans_target_var.set("")
        self._sm_trans_combo["values"] = trans_names
        self._sm_trans_var.set(f"规则{next_idx + 1}")
        self._sm_building = False
        self._sm_log(f"新增转移规则: 规则{next_idx + 1}")

    def _sm_del_transition(self):
        """删除转移规则 → 删除文件并重排后续索引。"""
        sel = self._sm_tree.selection()
        if not sel:
            return
        state_name = sel[0]
        cur = self._sm_trans_var.get()
        vals = list(self._sm_trans_combo["values"])
        if cur not in vals:
            return
        idx = vals.index(cur)

        path = self._sm_rule_path(state_name, idx)
        if path.exists():
            path.unlink()

        # 重排后续文件索引
        i = idx + 1
        while True:
            old = self._sm_rule_path(state_name, i)
            if not old.exists():
                break
            new = self._sm_rule_path(state_name, i - 1)
            old.rename(new)
            try:
                d = json.loads(new.read_text(encoding="utf-8"))
                d["index"] = i - 1
                new.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            i += 1

        remaining = self._sm_rule_files_for_state(state_name)
        self._sm_building = True
        if remaining:
            new_vals = [f"规则{i}" for i in range(1, len(remaining) + 1)]
            new_idx = min(idx, len(remaining) - 1)
            self._sm_trans_combo["values"] = new_vals
            self._sm_trans_var.set(new_vals[new_idx])
        else:
            self._sm_trans_combo["values"] = []
            self._sm_trans_var.set("")
            self._sm_trans_target_var.set("")
            self._sm_cond_tree.delete(*self._sm_cond_tree.get_children())
        self._sm_building = False
        if remaining:
            self._sm_load_rule(state_name, min(idx, len(remaining) - 1))
        self._sm_log(f"已删除转移规则: {cur}")

    def _on_sm_trans_select(self, evt):
        """切换规则：先保存当前到文件，再从文件加载新规则。"""
        if getattr(self, '_sm_building', False):
            return
        sel = self._sm_tree.selection()
        if not sel:
            return
        state_name = sel[0]
        self._sm_save_rule_for_state(state_name)
        cur = self._sm_trans_var.get()
        vals = list(self._sm_trans_combo["values"])
        if cur in vals:
            self._sm_load_rule(state_name, vals.index(cur))

    def _sm_load_rule(self, state_name: str, idx: int):
        """从规则文件加载到条件编辑器。"""
        path = self._sm_rule_path(state_name, idx)
        self._sm_cond_tree.delete(*self._sm_cond_tree.get_children())
        if not path.exists():
            self._sm_trans_target_var.set("")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._sm_trans_target_var.set("")
            return
        self._sm_trans_target_var.set(data.get("target", ""))
        for c in data.get("conditions", []):
            mode_label = "颜色一致" if c.get("mode") == "match" else "变化后一致"
            self._sm_cond_tree.insert("", tk.END, values=(
                c.get("region_label", ""),
                c.get("color", "#00FF00"),
                str(c.get("tolerance", 20)),
                mode_label,
            ))
        self._sm_refresh_combos()

    def _sync_rules_from_files(self, state: SMState):
        """将规则文件同步到 state.transitions（供引擎运行和持久化）。"""
        state.transitions.clear()
        for path in self._sm_rule_files_for_state(state.name):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            state.transitions.append(SMTransition(
                target=data.get("target", ""),
                enabled=data.get("enabled", True),
                conditions=[
                    SMCondition(
                        region_label=c.get("region_label", ""),
                        color=c.get("color", "#00FF00"),
                        tolerance=c.get("tolerance", 20),
                        enabled=c.get("enabled", True),
                        mode=c.get("mode", "match"),
                    )
                    for c in data.get("conditions", [])
                ],
            ))

    def _sm_add_enter_action(self):
        sel = self._sm_tree.selection()
        if not sel:
            return
        rec = self._sm_enter_rec_var.get().strip()
        if not rec:
            return
        self._sm_enter_tree.insert("", tk.END, values=(rec, ""))

    def _sm_del_enter_action(self):
        sel = self._sm_enter_tree.selection()
        if sel:
            self._sm_enter_tree.delete(sel[0])

    def _sm_add_exit_action(self):
        sel = self._sm_tree.selection()
        if not sel:
            return
        rec = self._sm_exit_rec_var.get().strip()
        if not rec:
            return
        self._sm_exit_tree.insert("", tk.END, values=(rec, ""))

    def _sm_del_exit_action(self):
        sel = self._sm_exit_tree.selection()
        if sel:
            self._sm_exit_tree.delete(sel[0])

    def _sm_draw_flow(self):
        """绘制状态机流程图（PLC SFC 风格）— 垂直布局 + 正交连线。"""
        if self._sm_editing_state:
            self._sm_save_rule_for_state(self._sm_editing_state)
            self._sm_flush_state_by_name(self._sm_editing_state)
        self._sm_sync_engine_from_gui()
        canvas = self._sm_flow_canvas
        canvas.delete("all")

        if not self._sm_engine or not self._sm_engine.config.states:
            canvas.create_text(200, 40, text="(无状态，请先添加状态)", fill="#999",
                               font=FONT_BODY)
            return

        cfg = self._sm_engine.config
        states = cfg.states
        n = len(states)

        # ── 垂直布局参数 ──
        box_w, box_h = 135, 52
        gap = 78                     # 相邻状态框顶边间距
        start_y = 32

        cw = canvas.winfo_width()
        if cw > 10:
            center_x = cw // 2
        else:
            center_x = 300
        box_x = center_x - box_w // 2  # 所有状态框左 X（水平居中）

        route_offsets: dict[str, int] = {}  # 非顺序转移的路由偏移量（防重叠）
        _next_offset = 55

        # ── 计算位置 ──
        positions: dict[str, tuple[int, int]] = {}
        for i, s in enumerate(states):
            positions[s.name] = (box_x, start_y + i * gap)

        # ── 解析每个状态的所有转移目标（多分支）──
        def _get_transitions(s: SMState, i: int) -> list[tuple[str, str]]:
            """返回 [(目标状态名, 条件标签), ...]"""
            result: list[tuple[str, str]] = []
            # 新版：多转移规则
            if s.transitions:
                for ti, tr in enumerate(s.transitions):
                    t = tr.target
                    if not t or t == "STOP":
                        continue
                    # 条件标签
                    if tr.conditions:
                        parts = [f"{c.region_label}:{c.color}" for c in tr.conditions[:2]]
                        lbl = ", ".join(parts)
                    else:
                        lbl = f"规则{ti + 1}"
                    if t in positions:
                        result.append((t, lbl))
                return result
            # 旧版：单条件 → next_state
            t = s.next_state
            if not t:
                if i + 1 < n:
                    t = states[i + 1].name
                elif cfg.loop and n > 0:
                    t = states[0].name
            if t and t != "STOP" and t in positions:
                lbl = ", ".join(f"{c.region_label}:{c.color}" for c in s.conditions[:2]) if s.conditions else "(无条件)"
                if len(s.conditions) > 2:
                    lbl += f" +{len(s.conditions) - 2}"
                result.append((t, lbl))
            return result

        def _is_seq(i: int, target: str) -> bool:
            if i + 1 < n and target == states[i + 1].name:
                return True
            if i == n - 1 and cfg.loop and n > 1 and target == states[0].name:
                return True
            return False

        # ── 画连线 ──
        for i, s in enumerate(states):
            sx, sy = positions[s.name]
            sx_c = sx + box_w // 2
            sy_c = sy + box_h
            active = s.name == self._sm_engine.current_state

            for ti, (target, cond_text) in enumerate(_get_transitions(s, i)):
                tx, ty = positions[target]
                tx_c = tx + box_w // 2
                ty_c = ty

                lc = "#388E3C" if active else "#666666"

                if _is_seq(i, target) and ti == 0:
                    # 第一个顺序转移：竖直连线
                    canvas.create_line(sx_c, sy_c, tx_c, ty_c,
                                       arrow=tk.LAST, fill=lc, width=2)
                    mid_y = (sy_c + ty_c) // 2
                    canvas.create_text(sx_c + 14, mid_y, text=cond_text,
                                       fill="#444", font=FONT_TINY, anchor=tk.W)
                else:
                    # 非顺序或多分支转移：右侧直角路由
                    route_key = f"{target}_{ti}"
                    if route_key not in route_offsets:
                        route_offsets[route_key] = _next_offset
                        _next_offset += 28
                    rx = box_x + box_w + route_offsets[route_key]

                    # 源框底部 → 右 → 垂直 → 左 → 目标框顶部
                    canvas.create_line(sx_c, sy_c, sx_c, sy_c + 8, fill=lc, width=2)
                    canvas.create_line(sx_c, sy_c + 8, rx, sy_c + 8, fill=lc, width=2)
                    canvas.create_line(rx, sy_c + 8, rx, ty_c - 8, fill=lc, width=2)
                    canvas.create_line(rx, ty_c - 8, tx_c, ty_c - 8, fill=lc, width=2)
                    canvas.create_line(tx_c, ty_c - 8, tx_c, ty_c,
                                       arrow=tk.LAST, fill=lc, width=2)

                    # 条件文本
                    canvas.create_text((sx_c + rx) // 2, sy_c + 2, text=cond_text,
                                       fill="#444", font=FONT_TINY, anchor=tk.S)

        # ── 画状态方框 ──
        for s in states:
            x, y = positions[s.name]
            is_current = s.name == self._sm_engine.current_state
            is_initial = s.name == cfg.initial_state

            fill_color = "#C8E6C9" if is_current else "#E3F2FD" if is_initial else "#FAFAFA"
            outline_color = "#2E7D32" if is_current else "#1565C0" if is_initial else "#BDBDBD"
            olw = 3 if is_initial else 2

            canvas.create_rectangle(x, y, x + box_w, y + box_h,
                                    fill=fill_color, outline=outline_color,
                                    width=olw, tags=("state", s.name))

            # 状态名
            dname = s.name if len(s.name) <= 14 else s.name[:13] + "…"
            canvas.create_text(x + box_w // 2, y + 14, text=dname,
                               font=(FONT_BODY[0], FONT_BODY[1], "bold" if is_current else "normal"),
                               fill="#222", tags=("state", s.name))

            # 条件 / 动作计数
            nc = len([c for c in s.conditions if c.enabled])
            na = len([a for a in s.on_enter if a.enabled])
            canvas.create_text(x + box_w // 2, y + 34,
                               text=f"{nc}条件  {na}动作",
                               font=FONT_TINY, fill="#777",
                               tags=("state", s.name))

            if is_initial:
                canvas.create_text(x + box_w // 2, y - 10, text="▼ 初始",
                                   font=(FONT_TINY[0], FONT_TINY[1], "bold"),
                                   fill="#1565C0")

        # ── 滚动区域 ──
        total_h = start_y + n * gap + 40
        total_w = center_x * 2
        canvas.configure(scrollregion=(0, 0, total_w, total_h))

    def _pixel_get_cursor_pos(self) -> tuple[int, int] | None:
        if IS_WINDOWS:
            from ctypes import wintypes
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return (pt.x, pt.y)
        # Linux: 按可靠性排序 — Tkinter 零依赖最先
        # 方法 1: Tkinter（跨平台，需要窗口存在）
        try:
            return (self.root.winfo_pointerx(), self.root.winfo_pointery())
        except Exception:
            pass
        # 方法 2: xdotool（X11+Wayland 均支持）
        try:
            result = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                x = y = 0
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("x="):
                        x = int(line.split("=")[1])
                    elif line.startswith("y="):
                        y = int(line.split("=")[1])
                return (x, y)
        except (FileNotFoundError, Exception):
            pass
        # 方法 3: python-xlib
        try:
            from Xlib import display
            d = display.Display()
            root = d.screen().root
            data = root.query_pointer()
            return (data.root_x, data.root_y)
        except Exception:
            pass
        return None

    def _pixel_capture_p1(self):
        if not hasattr(self, '_px_x1_var'):
            return
        pos = self._pixel_get_cursor_pos()
        if pos:
            self._px_x1_var.set(pos[0])
            self._px_y1_var.set(pos[1])
            self._px_p1_label.configure(text=f"({pos[0]},{pos[1]})")
        else:
            if IS_LINUX:
                self._px_log("取坐标失败 — 请先安装 xdotool: sudo apt install xdotool")
            else:
                self._px_log("当前平台不支持鼠标坐标获取")

    def _pixel_capture_p2(self):
        if not hasattr(self, '_px_x2_var'):
            return
        pos = self._pixel_get_cursor_pos()
        if pos:
            self._px_x2_var.set(pos[0])
            self._px_y2_var.set(pos[1])
            self._px_p2_label.configure(text=f"({pos[0]},{pos[1]})")
        else:
            if IS_LINUX:
                self._px_log("取坐标失败 — 请先安装 xdotool: sudo apt install xdotool")
            else:
                self._px_log("当前平台不支持鼠标坐标获取")

    def _pixel_capture_color(self):
        """F8: 取鼠标位置像素颜色 → 填入规则颜色框。"""
        if not hasattr(self, '_px_rule_color_var'):
            return
        pos = self._pixel_get_cursor_pos()
        if not pos:
            return
        # 取色
        from pixel_monitor import _capture_region_rgb
        data = _capture_region_rgb(pos[0], pos[1], pos[0] + 1, pos[1] + 1)
        if data and len(data) >= 3:
            r, g, b = data[0], data[1], data[2]
            hex_c = f"#{r:02X}{g:02X}{b:02X}"
            self._px_rule_color_var.set(hex_c)
            self._px_log(f"取色 ({pos[0]},{pos[1]}) → {hex_c}  R{r} G{g} B{b}")

    def _start_mouse_tracking(self):
        """启动鼠标实时坐标轮询（约 10 fps）。"""
        if self._px_mouse_tracking:
            return
        self._px_mouse_tracking = True
        self._poll_mouse_position()

    def _stop_mouse_tracking(self):
        self._px_mouse_tracking = False

    def _poll_mouse_position(self):
        """轮询鼠标位置并更新标签（10 fps）。"""
        if not self._px_mouse_tracking:
            return
        try:
            pos = self._pixel_get_cursor_pos()
            if pos:
                self._px_mouse_label.configure(text=f"鼠标: ({pos[0]}, {pos[1]})")
            else:
                self._px_mouse_label.configure(text="鼠标: (请安装 xdotool)")
        except Exception:
            self._px_mouse_label.configure(text="鼠标: (错误)")
        self.root.after(100, self._poll_mouse_position)

    def _px_mouse_to(self, target: str):
        """将当前鼠标坐标填入 P1 或 P2。"""
        pos = self._pixel_get_cursor_pos()
        if not pos:
            if IS_LINUX:
                self._px_log("取坐标失败 — 请安装 xdotool: sudo apt install xdotool")
            else:
                self._px_log("当前平台不支持鼠标坐标获取")
            return
        if target == "P1":
            if hasattr(self, '_px_x1_var'):
                self._px_x1_var.set(pos[0])
                self._px_y1_var.set(pos[1])
                self._px_p1_label.configure(text=f"({pos[0]},{pos[1]})")
        else:
            if hasattr(self, '_px_x2_var'):
                self._px_x2_var.set(pos[0])
                self._px_y2_var.set(pos[1])
                self._px_p2_label.configure(text=f"({pos[0]},{pos[1]})")

    def _pixel_pick_color(self):
        """打开系统色盘选色。"""
        result = colorchooser.askcolor(color=self._px_rule_color_var.get(), title="选择触发颜色")
        if result and result[1]:
            self._px_rule_color_var.set(result[1])

    def _pixel_eyedropper(self):
        """吸色工具：窗口保持可见，按住左键拖动实时取色，松开确认，Esc恢复原色。"""
        if not hasattr(self, '_px_rule_color_var'):
            return

        # 记住原有颜色，Esc 时恢复
        _orig_color = self._px_rule_color_var.get()

        overlay = tk.Toplevel(self.root)
        overlay.attributes('-fullscreen', True)
        overlay.attributes('-alpha', 0.001)
        overlay.attributes('-topmost', True)
        overlay.attributes('-toolwindow', True)
        overlay.configure(cursor='crosshair', bg='black')
        overlay.grab_set()
        overlay.focus_force()

        # 浮动色块预览
        preview = tk.Toplevel(overlay)
        preview.attributes('-topmost', True)
        preview.attributes('-toolwindow', True)
        preview.overrideredirect(True)
        preview.configure(bg='#CCCCCC')
        preview_label = tk.Label(preview, text="", width=8, font=FONT_MONO_SM,
                                 relief=tk.SOLID, borderwidth=1, bg='#CCCCCC')
        preview_label.pack()
        preview.withdraw()

        _last_update: list[float] = [0.0]

        def _cleanup(restore: bool = False):
            try:
                preview.destroy()
                overlay.grab_release()
                overlay.destroy()
            except Exception:
                pass
            if restore:
                self._px_rule_color_var.set(_orig_color)

        def _pick_color_at(evt):
            now = time.time()
            if now - _last_update[0] < 0.05:
                return
            _last_update[0] = now
            x, y = evt.x_root, evt.y_root
            try:
                from pixel_monitor import _capture_region_rgb
                data = _capture_region_rgb(x, y, x + 1, y + 1)
                if data and len(data) >= 3:
                    r, g, b = data[0], data[1], data[2]
                    hex_c = f"#{r:02X}{g:02X}{b:02X}"
                    # 实时更新主窗口颜色
                    self._px_rule_color_var.set(hex_c)
                    # 更新浮动预览
                    preview_label.configure(text=f" {hex_c} ", bg=hex_c)
                    bright = (r * 299 + g * 587 + b * 114) / 1000
                    preview_label.configure(fg="black" if bright > 128 else "white")
                    preview.geometry(f"+{x+16}+{y+16}")
                    preview.deiconify()
                    preview.lift()
            except Exception:
                pass

        def on_press(evt):
            _pick_color_at(evt)

        def on_drag(evt):
            _pick_color_at(evt)

        def on_release(evt):
            _pick_color_at(evt)
            self._px_log(f"吸色 → {self._px_rule_color_var.get()}")
            _cleanup(restore=False)

        overlay.bind('<Button-1>', on_press)
        overlay.bind('<B1-Motion>', on_drag)
        overlay.bind('<ButtonRelease-1>', on_release)
        overlay.bind('<Escape>', lambda _e: _cleanup(restore=True))

    def _pixel_add_region(self):
        name = self._px_name_var.get().strip()
        if not name:
            messagebox.showinfo("提示", "请输入区域名称")
            return
        if self._px_tree.exists(name):
            messagebox.showinfo("提示", f"区域「{name}」已存在，请换一个名称")
            return
        x1, y1 = self._px_x1_var.get(), self._px_y1_var.get()
        x2, y2 = self._px_x2_var.get(), self._px_y2_var.get()
        if x1 == x2 or y1 == y2:
            messagebox.showinfo("提示", "两点不能在同一行或同一列")
            return
        if not self._pixel_monitor:
            self._pixel_monitor = PixelMonitor(interval=self._px_interval_var.get() / 1000.0)
        self._pixel_monitor.add_region(name, x1, y1, x2, y2)
        r = self._pixel_monitor.regions[-1]
        self._px_tree.insert("", tk.END, iid=name,
                             values=(name, f"{r.width}x{r.height}", "—", "0"))
        self._px_name_var.set("")
        self._px_p1_label.configure(text="")
        self._px_p2_label.configure(text="")
        self._pixel_save_config()
        self._px_log(f"[{name}] 新增区域 ({x1},{y1})-({x2},{y2})  {r.width}x{r.height}")

    def _pixel_delete_region(self, event):
        sel = self._px_tree.selection()
        if not sel:
            return
        name = sel[0]
        if messagebox.askyesno("确认", f"删除监测区域 \"{name}\"?"):
            self._px_tree.delete(name)
            self._px_rule_tree.delete(*self._px_rule_tree.get_children())
            self._px_rule_label_var.set("← 选中左侧区域后编辑规则")
            if self._pixel_monitor:
                self._pixel_monitor.remove_region(name)
            self._pixel_save_config()

    def _pixel_add_rule(self):
        sel = self._px_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧列表选中一个区域")
            return
        name = sel[0]
        color = self._px_rule_color_var.get().strip()
        tol = self._px_rule_tol_var.get()
        recording = self._px_rule_recording_var.get().strip()
        if not color.startswith("#") or len(color) != 7:
            messagebox.showinfo("提示", "颜色格式: #RRGGBB (可用 F8 取色)")
            return
        if not recording:
            messagebox.showinfo("提示", "请选择回放录制")
            return
        if self._pixel_monitor:
            r = self._pixel_monitor.get_region(name)
            if r is not None:
                r.rules.append(PixelRule(color=color, tolerance=tol, recording=recording))
                iid = self._px_rule_tree.insert("", tk.END, values=(color, str(tol), recording))
                self._px_tag_rule_row(iid, color)
                self._pixel_save_config()
                self._px_log(f"[{name}] 规则: {color} (容差{tol}) → 回放「{recording}」")

    def _pixel_delete_rule(self):
        sel_region = self._px_tree.selection()
        sel_rule = self._px_rule_tree.selection()
        if not sel_region or not sel_rule:
            return
        region_name = sel_region[0]
        rule_idx = self._px_rule_tree.index(sel_rule[0])
        if self._pixel_monitor:
            r = self._pixel_monitor.get_region(region_name)
            if r and 0 <= rule_idx < len(r.rules):
                removed = r.rules.pop(rule_idx)
                self._px_rule_tree.delete(sel_rule[0])
                self._pixel_save_config()
                self._px_log(f"[{region_name}] 已删除规则: {removed.color}")
        self._px_editing_rule_idx = -1
        self._px_rule_upd_btn.configure(state=tk.DISABLED)

    def _pixel_edit_rule_populate(self, evt=None):
        """双击规则行 → 将规则值填入编辑表单。"""
        sel_rule = self._px_rule_tree.selection()
        if not sel_rule:
            return
        idx = self._px_rule_tree.index(sel_rule[0])
        vals = self._px_rule_tree.item(sel_rule[0], "values")
        if len(vals) >= 3:
            self._px_rule_color_var.set(str(vals[0]))
            try:
                self._px_rule_tol_var.set(int(vals[1]))
            except ValueError:
                self._px_rule_tol_var.set(20)
            self._px_rule_recording_var.set(str(vals[2]))
            self._px_editing_rule_idx = idx
            self._px_rule_upd_btn.configure(state=tk.NORMAL)

    def _pixel_update_rule(self):
        """更新当前编辑的规则。"""
        sel_region = self._px_tree.selection()
        if not sel_region:
            return
        region_name = sel_region[0]
        idx = self._px_editing_rule_idx
        if idx < 0:
            return
        if self._pixel_monitor:
            r = self._pixel_monitor.get_region(region_name)
            if r and idx < len(r.rules):
                color = self._px_rule_color_var.get().strip()
                tol = self._px_rule_tol_var.get()
                recording = self._px_rule_recording_var.get().strip()
                if not color.startswith("#") or len(color) != 7:
                    return
                r.rules[idx] = PixelRule(color=color, tolerance=tol, recording=recording)
                # 更新树显示
                sel_rule = self._px_rule_tree.selection()
                if sel_rule:
                    self._px_rule_tree.item(sel_rule[0], values=(color, str(tol), recording))
                    self._px_tag_rule_row(sel_rule[0], color)
                self._pixel_save_config()
                self._px_log(f"[{region_name}] 规则已更新: {color} (容差{tol}) → 回放「{recording}」")
                self._px_editing_rule_idx = -1
                self._px_rule_upd_btn.configure(state=tk.DISABLED)

    def _pixel_refresh_recordings(self):
        names = []
        for f in sorted(RECORDINGS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True):
            names.append(f.stem)
        self._px_rule_combo["values"] = names
        if names and not self._px_rule_recording_var.get():
            self._px_rule_combo.current(0)

    def _toggle_pixel_monitor(self):
        if self._pixel_monitor and self._pixel_monitor.running:
            self._pixel_monitor.stop()
            self._px_start_btn.configure(text="开始监测")
            self._pixel_save_config()
            # 重置所有区域状态
            for item in self._px_tree.get_children():
                self._px_tree.set(item, "status", "—")
            self._px_log("监测已停止")
        else:
            if not self._pixel_monitor:
                self._pixel_monitor = PixelMonitor(interval=self._px_interval_var.get() / 1000.0)
            if not self._pixel_monitor.regions:
                messagebox.showinfo("提示", "请先添加至少一个监测区域")
                return
            self._pixel_monitor.interval = self._px_interval_var.get() / 1000.0
            self._pixel_monitor.set_callback(lambda ev: self.root.after(0, self._on_pixel_change, ev))
            self._pixel_monitor.set_trigger_callback(lambda ev: self.root.after(0, self._on_rule_trigger, ev))
            self._pixel_monitor.start()
            self._pixel_save_config()
            self._px_start_btn.configure(text="停止监测")
            for item in self._px_tree.get_children():
                r = self._pixel_monitor.get_region(item)
                self._px_tree.set(item, "status", r.current_color_name if r and r.current_color_name else "等待采样")
            self._px_log(f"监测已启动 — {len(self._pixel_monitor.regions)} 个区域, 间隔 {self._px_interval_var.get()}ms")
            self._pixel_update_display()

    def _on_pixel_change(self, event):
        label = event.region.label
        ts = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
        if self._px_tree.exists(label):
            cname = event.region.current_color_name
            self._px_tree.set(label, "status", cname)
            self._px_tree.set(label, "changes", str(event.region.change_count))
            if event.region.current_color_hex:
                tag = f"rc_{event.region.current_color_hex.lstrip('#')}"
                self._px_tree.tag_configure(tag, foreground=event.region.current_color_hex)
                self._px_tree.item(label, tags=(tag,))
        w, h = event.region.width, event.region.height
        self._px_log(f"{ts} [{label}] 画面变化 → {event.region.current_color_name}  {w}x{h} 区域")

    def _on_rule_trigger(self, event):
        ts = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
        recording = event.rule.recording
        from pixel_monitor import _hex_to_rgb, _rgb_to_color_name
        tr, tg, tb = _hex_to_rgb(event.rule.color)
        cname = _rgb_to_color_name(tr, tg, tb)
        self._px_log(f"{ts} *** [{event.region.label}] 检测到「{cname}」→ 自动回放「{recording}」***")
        self._replay_by_name(recording)

    def _replay_by_name(self, name: str, speed: float = 1.0):
        json_path = RECORDINGS_DIR / f"{name}.json"
        if not json_path.exists():
            self._px_log(f"  录制文件不存在: {name}")
            return
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["speed"] = speed
        data["start_delay"] = 1  # 自动触发时减少等待
        if IS_LINUX:
            self._play_with_sendinput(data, speed, name)
            return
        ahk_path = RECORDINGS_DIR / f"{name}.ahk"
        with open(ahk_path, "w", encoding="utf-8") as f:
            f.write(generate_ahk_script(data))
        if self._ahk_path:
            try:
                subprocess.Popen(
                    [self._ahk_path, str(ahk_path)],
                    creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )
            except Exception:
                self._play_with_sendinput(data, speed, name)
        else:
            self._play_with_sendinput(data, speed, name)

    def _pixel_update_display(self):
        if not (self._pixel_monitor and self._pixel_monitor.running):
            return
        for item in self._px_tree.get_children():
            r = self._pixel_monitor.get_region(item)
            if r and r.current_color_name:
                self._px_tree.set(item, "status", r.current_color_name)
                if r.current_color_hex:
                    tag = f"rc_{r.current_color_hex.lstrip('#')}"
                    self._px_tree.tag_configure(tag, foreground=r.current_color_hex)
                    self._px_tree.item(item, tags=(tag,))
        self.root.after(1000, self._pixel_update_display)

    def _px_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._px_log_text.configure(state=tk.NORMAL)
        self._px_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
        self._px_log_text.see(tk.END)
        self._px_log_text.configure(state=tk.DISABLED)

    # ── 全局热键 ────────────────────────────────────────────

    def _start_hotkeys(self):
        # ── tkinter 级热键（窗口获焦时可靠，跨平台兜底）──
        self.root.bind_all("<F9>", lambda e: self._start_recording())
        self.root.bind_all("<F10>", lambda e: self._stop_recording())
        if PIXEL_OK:
            self.root.bind_all("<F6>", lambda e: self._pixel_capture_p1())
            self.root.bind_all("<F7>", lambda e: self._pixel_capture_p2())
            self.root.bind_all("<F8>", lambda e: self._pixel_capture_color())

        # ── pynput 全局热键（窗口失焦时仍可触发）──
        if not PYNPUT_OK:
            if IS_LINUX:
                self._status_var.set("pynput 未安装 — 热键仅窗口获焦时有效")
            return
        try:
            hotkeys: dict = {
                "<f9>": lambda: self.root.after(0, self._start_recording),
                "<f10>": lambda: self.root.after(0, self._stop_recording),
            }
            if PIXEL_OK:
                hotkeys["<f6>"] = lambda: self.root.after(0, self._pixel_capture_p1)
                hotkeys["<f7>"] = lambda: self.root.after(0, self._pixel_capture_p2)
                hotkeys["<f8>"] = lambda: self.root.after(0, self._pixel_capture_color)

            self._hotkey_listener = keyboard.GlobalHotKeys(hotkeys)
            self._hotkey_listener.start()
            if IS_LINUX:
                self._status_var.set("全局热键已启动 (pynput + xdotool)")
        except Exception as e:
            if IS_LINUX:
                self._status_var.set(f"全局热键失败: {e} — 使用窗口热键")

    # ── 录制操作 ────────────────────────────────────────────

    def _on_kb_record_toggle(self):
        """键盘录制开关切换。"""
        self.session.record_keyboard = self._kb_record_var.get()
        state = "已启用" if self.session.record_keyboard else "已禁用"
        self._status_var.set(f"键盘录制: {state}")

    def _start_recording(self):
        if self.session.recording:
            return
        if not PYNPUT_OK:
            messagebox.showerror("依赖缺失", "请先安装 pynput 库:\npip install pynput")
            return

        try:
            self.session.start()
        except Exception as exc:
            messagebox.showerror("录制启动失败", str(exc))
            return

        self._btn_record.configure(state=tk.DISABLED)
        self._btn_stop.configure(state=tk.NORMAL)
        self._btn_play.configure(state=tk.DISABLED)
        self._status_var.set("● 正在录制中...")
        self._update_recording_display()

    def _stop_recording(self):
        if not self.session.recording:
            return

        self.session.stop()

        # 弹出命名对话框
        default_name = datetime.now().strftime("recording_%Y%m%d_%H%M%S")
        name = simpledialog.askstring(
            "保存录制",
            "请输入录制名称:",
            parent=self.root,
            initialvalue=default_name,
        )
        if not name or not name.strip():
            name = default_name
        else:
            name = name.strip()

        # 保存 JSON 数据
        data = self.session.get_data(name)
        json_path = RECORDINGS_DIR / f"{name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 生成 AHK 脚本（默认速度 1x）
        data["speed"] = 1.0
        data["start_delay"] = DEFAULT_START_DELAY
        ahk_path = RECORDINGS_DIR / f"{name}.ahk"
        with open(ahk_path, "w", encoding="utf-8") as f:
            f.write(generate_ahk_script(data))

        self._btn_record.configure(state=tk.NORMAL)
        self._btn_stop.configure(state=tk.DISABLED)
        self._btn_play.configure(state=tk.NORMAL)
        self._status_var.set(
            f"✓ 已保存: {name}  |  {data['duration_ms'] / 1000:.1f}s  |  {data['event_count']} 个事件"
        )
        self._info_var.set("")
        self._refresh_list()

    # ── 回放操作 ────────────────────────────────────────────

    def _play_selected(self):
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在列表中选择一个录制")
            return

        name = self._tree.item(selection[0])["values"][0]
        speed = self._speed_var.get()

        # 读取 JSON
        json_path = RECORDINGS_DIR / f"{name}.json"
        if not json_path.exists():
            messagebox.showerror("错误", f"录制文件不存在: {name}")
            return

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 更新 speed 并重新生成 AHK 脚本
        data["speed"] = speed
        data["start_delay"] = DEFAULT_START_DELAY
        if IS_LINUX:
            self._play_with_sendinput(data, speed, name)
            return
        ahk_path = RECORDINGS_DIR / f"{name}.ahk"
        with open(ahk_path, "w", encoding="utf-8") as f:
            f.write(generate_ahk_script(data))

        if self._ahk_path:
            # 优先使用 AutoHotkey 回放
            try:
                subprocess.Popen(
                    [self._ahk_path, str(ahk_path)],
                    creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )
                self._status_var.set(
                    f"▶ AHK 回放中: {name} ({speed}x)  — 按 Esc 可中断"
                )
            except Exception as exc:
                messagebox.showerror("回放失败", f"无法启动 AutoHotkey:\n{exc}")
                self._play_with_sendinput(data, speed, name)
        else:
            # 无 AHK，使用内置引擎
            self._play_with_sendinput(data, speed, name)

    def _play_with_sendinput(self, data: dict, speed: float, name: str):
        """后台线程中使用平台引擎回放。"""
        self._playback_thread = threading.Thread(
            target=self._run_playback,
            args=(data, speed, name),
            daemon=True,
        )
        self._playback_thread.start()

    def _run_playback(self, data: dict, speed: float, name: str):
        """回放执行体（在后台线程中运行，自动选择平台引擎）。"""
        engine_name = "xdotool" if IS_LINUX else "SendInput"
        self.root.after(0, lambda: self._status_var.set(
            f"▶ {engine_name} 回放中: {name} ({speed}x)..."
        ))
        try:
            play_back(data, speed)
            self.root.after(0, lambda: self._status_var.set(
                f"✓ 回放完成: {name}"
            ))
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror(
                "回放异常", str(exc)
            ))

    # ── 列表管理 ────────────────────────────────────────────

    def _refresh_list(self):
        for item in self._tree.get_children():
            self._tree.delete(item)

        recordings: list[tuple[str, str, str, int]] = []
        for f in sorted(RECORDINGS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                recordings.append((
                    d.get("name", f.stem),
                    d.get("created", ""),
                    f"{d.get('duration_ms', 0) / 1000:.1f}s",
                    d.get("event_count", 0),
                ))
            except Exception:
                pass

        for rec in recordings:
            self._tree.insert("", tk.END, values=rec)

    def _delete_selected(self):
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择一个录制")
            return

        name = self._tree.item(selection[0])["values"][0]
        if not messagebox.askyesno("确认删除", f'确定要删除录制 "{name}" 吗？\n\n此操作不可恢复。'):
            return

        for ext in (".json", ".ahk"):
            p = RECORDINGS_DIR / f"{name}{ext}"
            if p.exists():
                p.unlink()

        self._status_var.set(f"已删除: {name}")
        self._refresh_list()

    # ── 实时显示 ────────────────────────────────────────────

    def _update_recording_display(self):
        if not self.session.recording:
            return
        self._info_var.set(
            f"录制时长: {self.session.elapsed_seconds:.1f}s  |  已采集事件: {self.session.event_count}"
        )
        self.root.after(100, self._update_recording_display)

    # ── 关闭 ────────────────────────────────────────────────

    def _on_close(self):
        if self.session.recording:
            self.session.stop()
        if self._hotkey_listener:
            self._hotkey_listener.stop()
        if self._cloud_svc and self._cloud_svc.running:
            self._cloud_svc.stop()
        if self._pixel_monitor and self._pixel_monitor.running:
            self._pixel_monitor.stop()
        self._pixel_save_config()
        if self._sm_engine and self._sm_engine.running:
            self._sm_engine.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

def main():
    if not PYNPUT_OK:
        # 无 GUI 依赖时也至少弹个提示
        import tkinter.messagebox as _mb

        root = tk.Tk()
        root.withdraw()
        _mb.showinfo(
            "缺少依赖",
            "需要安装 pynput 库才能使用录制功能。\n\n"
            "请在命令行执行:\n"
            "    pip install pynput\n\n"
            "安装后重新运行本程序。",
        )
        root.destroy()
        return

    app = App()
    app.run()


if __name__ == "__main__":
    main()
