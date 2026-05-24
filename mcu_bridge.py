#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCU 通信桥 — 通过串口向单片机发送指令。"""
from __future__ import annotations

import logging
import time
from typing import Optional

from config import MCUSettings

logger = logging.getLogger("mcu_bridge")


class MCUBridge:
    """串口通信桥，连接单片机。"""

    def __init__(self, settings: MCUSettings):
        self._cfg = settings
        self._serial: Optional[object] = None

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def connect(self) -> bool:
        """打开串口连接。返回是否成功。"""
        if not self._cfg.enabled:
            logger.info("MCU 未启用，跳过连接")
            return False
        try:
            import serial  # type: ignore
            self._serial = serial.Serial(
                port=self._cfg.port,
                baudrate=self._cfg.baudrate,
                timeout=self._cfg.timeout_s,
            )
            logger.info("MCU 已连接: %s @ %d", self._cfg.port, self._cfg.baudrate)
            return True
        except ImportError:
            logger.warning("pyserial 未安装，MCU 功能不可用。安装: pip install pyserial")
            return False
        except Exception as e:
            logger.error("MCU 连接失败 (%s): %s", self._cfg.port, e)
            return False

    def disconnect(self):
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            logger.info("MCU 已断开")

    def send(self, data: str) -> bool:
        """发送字符串指令到 MCU。返回是否成功。"""
        if not self._serial or not self._serial.is_open:
            logger.warning("MCU 未连接，无法发送")
            return False
        try:
            self._serial.write(data.encode("utf-8"))
            self._serial.flush()
            logger.info("MCU 发送: %r", data.strip())
            return True
        except Exception as e:
            logger.error("MCU 发送失败: %s", e)
            return False

    def send_complete(self) -> bool:
        """发送打印完成指令。"""
        return self.send(self._cfg.complete_command)

    def read_line(self) -> Optional[str]:
        """读取一行 MCU 响应（非阻塞）。"""
        if not self._serial or not self._serial.is_open:
            return None
        try:
            if self._serial.in_waiting > 0:
                line = self._serial.readline().decode("utf-8", errors="replace").strip()
                if line:
                    logger.info("MCU 收到: %s", line)
                    return line
        except Exception:
            pass
        return None
