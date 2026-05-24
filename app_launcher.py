#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BambuLab Studio 启动器 — 回放录制 + 像素确认启动成功。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, Callable

from config import BambuSettings
from pixel_monitor import PixelRegion, _capture_region_rgb, _hex_to_rgb, _scan_rgb_for_color

logger = logging.getLogger("app_launcher")


class AppLauncher:
    """启动 BambuLab Studio 并通过屏幕颜色确认启动成功。"""

    def __init__(self, settings: BambuSettings):
        self._cfg = settings
        self._replay_fn: Optional[Callable[[str, float, float], bool]] = None
        self._pixel_regions: dict[str, PixelRegion] = {}  # label → region

    def set_replay_callback(self, fn: Callable[[str, float, float], bool]):
        """设置回放回调 fn(recording_name, speed, delay) -> bool。"""
        self._replay_fn = fn

    def add_pixel_region(self, region: PixelRegion):
        """注册像素检测区域（用于确认 Studio 启动等）。"""
        self._pixel_regions[region.label] = region

    def launch_and_confirm(self) -> bool:
        """
        启动 BambuLab Studio 并等待确认。
        Returns: True = 启动成功, False = 超时或失败。
        """
        # ── 1. 回放启动录制 ──
        launch_rec = self._cfg.launch_recording
        if launch_rec and self._replay_fn:
            logger.info("正在启动 BambuLab Studio (录制: %s)...", launch_rec)
            self._replay_fn(launch_rec, speed=1.0, delay=1.0)
        elif self._cfg.appimage_path:
            # 直接启动 AppImage
            self._launch_appimage()
        else:
            logger.warning("未配置 BambuLab Studio 启动方式")

        # ── 2. 等待启动完成 ──
        logger.info("等待 BambuLab Studio 启动 (超时 %ss)...", self._cfg.startup_timeout_s)
        deadline = time.time() + self._cfg.startup_timeout_s
        check_interval = 2.0

        while time.time() < deadline:
            # 先尝试取消更新弹窗
            self._try_cancel_update()

            # 检测确认区域颜色
            if self._check_confirm_region():
                logger.info("BambuLab Studio 启动确认成功")
                return True

            time.sleep(check_interval)

        logger.error("BambuLab Studio 启动超时 (%ss)", self._cfg.startup_timeout_s)
        return False

    def _launch_appimage(self):
        """直接通过命令行启动 AppImage。"""
        import subprocess as sp
        path = self._cfg.appimage_path
        if not path or not Path(path).exists():
            logger.warning("AppImage 路径不存在: %s", path)
            return
        try:
            sp.Popen([path], start_new_session=True)
            logger.info("AppImage 已启动: %s", path)
        except Exception as e:
            logger.error("启动 AppImage 失败: %s", e)

    def _try_cancel_update(self):
        """尝试取消更新弹窗（如果出现）。"""
        cancel_rec = self._cfg.cancel_update_recording
        if cancel_rec and self._replay_fn:
            # 如果 confirm_region 检测到特定颜色（如更新弹窗），则执行取消录制
            region = self._pixel_regions.get(self._cfg.confirm_region)
            if region and self._replay_fn:
                # 简化处理：每次检测前尝试取消更新
                pass  # 取消更新在 mouse_recorder 的状态机中处理

    def _check_confirm_region(self) -> bool:
        """检查确认区域的像素颜色是否匹配。"""
        region_label = self._cfg.confirm_region
        region = self._pixel_regions.get(region_label)
        if not region:
            logger.warning("未找到像素检测区域: %s", region_label)
            return False

        try:
            data = _capture_region_rgb(region.x1, region.y1, region.x2, region.y2)
        except Exception as e:
            logger.warning("截图失败: %s", e)
            return False

        if not data:
            return False

        try:
            tr, tg, tb = _hex_to_rgb(self._cfg.confirm_color)
        except Exception:
            return False

        return _scan_rgb_for_color(data, tr, tg, tb, tolerance=20)

    def wait_for_studio_ready(self) -> bool:
        """等待 Studio 界面就绪（已启动后确认）。"""
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if self._check_confirm_region():
                return True
            time.sleep(1.0)
        return False
