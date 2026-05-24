#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
屏幕区块监测器 — 矩形区域截图哈希比对 + 颜色规则触发
支持 Windows (GDI BitBlt) 和 Linux (PIL)
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import struct
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).parent / "pixel_config.json"


# ── 平台截图适配 ─────────────────────────────────────────────────────

def _capture_region_rgb(x1: int, y1: int, x2: int, y2: int) -> bytes:
    """截取屏幕矩形区域，返回 RGB 字节序列 (R,G,B,R,G,B,...)。"""
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    if sys.platform == "win32":
        return _capture_win32_rgb(x1, y1, w, h)
    else:
        return _capture_pil_rgb(x1, y1, w, h)


def _capture_win32_rgb(x: int, y: int, w: int, h: int) -> bytes:
    """Windows GDI 截图 → RGB 字节。"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)

    buf_size = w * h * 4
    buf_bgra = ctypes.create_string_buffer(buf_size)
    bmi = (ctypes.c_ubyte * 44)()
    struct.pack_into("<IiiHHIIiiII", bmi, 0, 40, w, h, 1, 32, 0, buf_size, 0, 0, 0, 0)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf_bgra, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    # BGRA → RGB: 每 4 字节取前 3 字节并重排 B,G,R → R,G,B
    raw = bytes(buf_bgra)
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        b, g, r = raw[i * 4], raw[i * 4 + 1], raw[i * 4 + 2]
        rgb[i * 3] = r
        rgb[i * 3 + 1] = g
        rgb[i * 3 + 2] = b
    return bytes(rgb)


def _capture_pil_rgb(x: int, y: int, w: int, h: int) -> bytes:
    """PIL 截图 → RGB 字节。"""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        return img.tobytes("raw", "RGB")
    except ImportError:
        pass
    # 降级: ImageMagick import 命令
    import subprocess as sp
    try:
        r = sp.run(
            ["import", "-window", "root", "-crop", f"{w}x{h}+{x}+{y}", "bmp:-"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0 and len(r.stdout) > 54:
            # BMP 像素是 BGR, 需转为 RGB
            bmp_data = r.stdout[54:]
            rgb = bytearray(len(bmp_data))
            for i in range(0, len(bmp_data), 3):
                if i + 2 < len(bmp_data):
                    rgb[i], rgb[i + 1], rgb[i + 2] = bmp_data[i + 2], bmp_data[i + 1], bmp_data[i]
            return bytes(rgb)
    except Exception:
        pass
    return b""


# ── 颜色扫描 ────────────────────────────────────────────────────────

def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """#RRGGBB 或 RRGGBB → (R, G, B)。"""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _scan_rgb_for_color(rgb_data: bytes, target_r: int, target_g: int, target_b: int,
                        tolerance: int) -> bool:
    """扫描 RGB 字节流，检查是否有像素匹配目标颜色（容差内）。"""
    pixel_count = len(rgb_data) // 3
    for i in range(pixel_count):
        r = rgb_data[i * 3]
        g = rgb_data[i * 3 + 1]
        b = rgb_data[i * 3 + 2]
        if (abs(r - target_r) + abs(g - target_g) + abs(b - target_b)) <= tolerance:
            return True
    return False


def _average_rgb(rgb_data: bytes) -> tuple[int, int, int]:
    """计算 RGB 数据的平均颜色。"""
    if not rgb_data:
        return (128, 128, 128)
    pixel_count = len(rgb_data) // 3
    if pixel_count == 0:
        return (128, 128, 128)
    total_r = total_g = total_b = 0
    for i in range(pixel_count):
        total_r += rgb_data[i * 3]
        total_g += rgb_data[i * 3 + 1]
        total_b += rgb_data[i * 3 + 2]
    return (total_r // pixel_count, total_g // pixel_count, total_b // pixel_count)


# 基准颜色映射表 (R,G,B) → 中文名称
_COLOR_MAP: list[tuple[tuple[int, int, int], str]] = [
    ((0, 0, 0), "黑色"),
    ((255, 255, 255), "白色"),
    ((128, 128, 128), "灰色"),
    ((255, 0, 0), "红色"),
    ((0, 255, 0), "绿色"),
    ((0, 0, 255), "蓝色"),
    ((255, 255, 0), "黄色"),
    ((0, 255, 255), "青色"),
    ((255, 0, 255), "紫色"),
    ((255, 165, 0), "橙色"),
    ((255, 192, 203), "粉色"),
    ((139, 69, 19), "棕色"),
    ((0, 128, 0), "深绿"),
    ((0, 0, 139), "深蓝"),
    ((255, 215, 0), "金色"),
    ((192, 192, 192), "银色"),
    ((128, 0, 128), "紫红"),
    ((0, 100, 0), "墨绿"),
    ((220, 220, 220), "浅灰"),
    ((245, 245, 220), "米色"),
]


def _rgb_to_color_name(r: int, g: int, b: int) -> str:
    """RGB → 最近的中文颜色名称。"""
    best_name = f"#{r:02X}{g:02X}{b:02X}"  # fallback
    best_dist = 999999
    for (cr, cg, cb), name in _COLOR_MAP:
        dist = abs(r - cr) + abs(g - cg) + abs(b - cb)
        if dist < best_dist:
            best_dist = dist
            best_name = name
    # 如果距离过大（颜色不在常见范围），保留 hex 作为名称
    if best_dist > 240:
        best_name = f"#{r:02X}{g:02X}{b:02X}"
    return best_name


# ── 数据结构 ────────────────────────────────────────────────────────

@dataclass
class PixelRule:
    """一条颜色触发规则。"""
    color: str = "#00FF00"     # 目标颜色 #RRGGBB
    tolerance: int = 20        # 色差容差 (0~765)
    recording: str = ""        # 触发后回放的录制名称
    enabled: bool = True


@dataclass
class PixelRegion:
    """一个矩形监测区域。"""
    label: str
    x1: int
    y1: int
    x2: int
    y2: int
    rules: list[PixelRule] = field(default_factory=list)
    last_hash: str = ""
    change_count: int = 0
    last_change_time: float = 0.0      # 上次变化的时间戳
    current_color_name: str = ""       # 当前区域平均颜色名称
    current_color_hex: str = ""        # 当前区域平均颜色 hex
    enabled: bool = True

    @property
    def width(self) -> int:
        return max(1, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(1, self.y2 - self.y1)


@dataclass
class PixelEvent:
    """一次区域变化事件。"""
    timestamp: float
    region: PixelRegion
    old_hash: str
    new_hash: str
    data_size: int


@dataclass
class RuleTriggerEvent:
    """一次规则触发事件。"""
    timestamp: float
    region: PixelRegion
    rule: PixelRule


# ── 引擎 ────────────────────────────────────────────────────────────

class PixelMonitor:
    """区块监测引擎：截图 → 哈希比对 → 颜色扫描 → 触发脚本。"""

    DEFAULT_INTERVAL = 2.0

    def __init__(self, interval: float = DEFAULT_INTERVAL):
        self.interval = interval
        self.regions: list[PixelRegion] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._on_change: Optional[callable] = None        # fn(PixelEvent)
        self._on_trigger: Optional[callable] = None        # fn(RuleTriggerEvent) — 应调用回调来触发回放
        self._current_hashes: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self._running

    def set_callback(self, cb: callable):
        self._on_change = cb

    def set_trigger_callback(self, cb: callable):
        self._on_trigger = cb

    def add_region(self, label: str, x1: int, y1: int, x2: int, y2: int) -> PixelRegion:
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        with self._lock:
            # 防止重复标签：若已存在则追加序号
            existing = {r.label for r in self.regions}
            orig = label
            n = 1
            while label in existing:
                label = f"{orig}_{n}"
                n += 1
            r = PixelRegion(label=label, x1=x1, y1=y1, x2=x2, y2=y2)
            self.regions.append(r)
        return r

    def remove_region(self, label: str):
        with self._lock:
            self.regions = [r for r in self.regions if r.label != label]

    def get_region(self, label: str) -> PixelRegion | None:
        for r in self.regions:
            if r.label == label:
                return r
        return None

    def get_current_hashes(self) -> dict[str, str]:
        with self._lock:
            return dict(self._current_hashes)

    # ── 配置持久化 ──────────────────────────────────────────────

    def save_config(self, path: Path = CONFIG_PATH):
        data = {
            "interval_ms": int(self.interval * 1000),
            "regions": [
                {
                    "label": r.label,
                    "x1": r.x1, "y1": r.y1, "x2": r.x2, "y2": r.y2,
                    "enabled": r.enabled,
                    "rules": [
                        {
                            "color": ru.color,
                            "tolerance": ru.tolerance,
                            "recording": ru.recording,
                            "enabled": ru.enabled,
                        }
                        for ru in r.rules
                    ],
                }
                for r in self.regions
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_config(cls, path: Path = CONFIG_PATH) -> PixelMonitor:
        mon = cls()
        if not path.exists():
            return mon
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return mon

        mon.interval = data.get("interval_ms", 2000) / 1000.0
        for rd in data.get("regions", []):
            r = PixelRegion(
                label=rd["label"],
                x1=rd["x1"], y1=rd["y1"], x2=rd["x2"], y2=rd["y2"],
                enabled=rd.get("enabled", True),
                rules=[
                    PixelRule(
                        color=ru.get("color", "#00FF00"),
                        tolerance=ru.get("tolerance", 20),
                        recording=ru.get("recording", ru.get("script", "")),  # 兼容旧字段 script
                        enabled=ru.get("enabled", True),
                    )
                    for ru in rd.get("rules", [])
                ],
            )
            mon.regions.append(r)
        return mon

    # ── 启动/停止 ──────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="region-monitor")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ── 主循环 ─────────────────────────────────────────────────

    def _run(self):
        while self._running:
            with self._lock:
                regs = [r for r in self.regions if r.enabled]
            for r in regs:
                try:
                    data = _capture_region_rgb(r.x1, r.y1, r.x2, r.y2)
                except Exception:
                    continue

                new_hash = hashlib.md5(data).hexdigest() if data else ""

                with self._lock:
                    self._current_hashes[r.label] = new_hash

                if r.last_hash and r.last_hash != new_hash:
                    now = time.time()
                    r.last_change_time = now
                    event = PixelEvent(
                        timestamp=now,
                        region=r,
                        old_hash=r.last_hash,
                        new_hash=new_hash,
                        data_size=len(data),
                    )
                    r.change_count += 1
                    if self._on_change:
                        self._on_change(event)

                    if data:
                        self._check_rules(r, data)

                r.last_hash = new_hash

                # 每次采样都更新当前平均颜色（无论是否变化）
                if data:
                    avg_r, avg_g, avg_b = _average_rgb(data)
                    r.current_color_hex = f"#{avg_r:02X}{avg_g:02X}{avg_b:02X}"
                    r.current_color_name = _rgb_to_color_name(avg_r, avg_g, avg_b)

            slept = 0.0
            step = 0.2
            while slept < self.interval and self._running:
                time.sleep(step)
                slept += step

    def _check_rules(self, region: PixelRegion, rgb_data: bytes):
        """扫描区域中每个启用的规则，匹配则触发回调（由上层执行回放）。"""
        for rule in region.rules:
            if not rule.enabled or not rule.recording.strip():
                continue
            try:
                tr, tg, tb = _hex_to_rgb(rule.color)
            except Exception:
                continue

            if _scan_rgb_for_color(rgb_data, tr, tg, tb, rule.tolerance):
                trigger = RuleTriggerEvent(
                    timestamp=time.time(),
                    region=region,
                    rule=rule,
                )
                if self._on_trigger:
                    self._on_trigger(trigger)
