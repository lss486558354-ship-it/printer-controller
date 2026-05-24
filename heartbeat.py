#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""心跳服务 — 独立线程，定时向云端发送完整状态。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Callable

from config import AppConfig

logger = logging.getLogger("heartbeat")


class DeviceStatus:
    """设备状态枚举。"""
    IDLE = "idle"
    PRINTING = "printing"
    FAULT = "fault"
    BUSY = "busy"


class HeartbeatPayload:
    """心跳数据包。"""

    def __init__(self):
        self.status: str = DeviceStatus.IDLE
        self.current_task_id: str = ""
        self.print_progress_pct: float = 0.0   # 0.0 ~ 100.0
        self.uptime_seconds: int = 0
        self.timestamp: int = 0
        self.extra: dict = {}

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current_task_id": self.current_task_id,
            "print_progress": round(self.print_progress_pct, 1),
            "uptime_seconds": self.uptime_seconds,
            "timestamp": self.timestamp,
            **self.extra,
        }


class HeartbeatService:
    """心跳发送服务，运行于独立线程。"""

    def __init__(self, config: AppConfig):
        self._cfg = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._start_time: float = 0.0
        self._payload = HeartbeatPayload()
        self._sender: Optional[Callable[[dict], bool]] = None

    def set_sender(self, fn: Callable[[dict], bool]):
        """设置心跳发送函数 fn(payload_dict) -> bool。"""
        self._sender = fn

    def update(self, **kwargs):
        """更新心跳数据（线程安全）。"""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._payload, key):
                    setattr(self._payload, key, value)

    def set_status(self, status: str):
        self.update(status=status)

    def set_task(self, task_id: str, progress: float = 0.0):
        self.update(current_task_id=task_id, print_progress_pct=progress)

    def clear_task(self):
        self.update(current_task_id="", print_progress_pct=0.0)

    def start(self):
        if self._running:
            return
        if not self._sender:
            logger.warning("心跳发送回调未设置，无法启动")
            return
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self._thread.start()
        logger.info("心跳服务已启动 (间隔 %ds)", self._cfg.cloud.heartbeat_interval_seconds)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("心跳服务已停止")

    def _run(self):
        interval = self._cfg.cloud.heartbeat_interval_seconds
        while self._running:
            try:
                with self._lock:
                    self._payload.uptime_seconds = int(time.time() - self._start_time)
                    self._payload.timestamp = int(time.time())
                    data = self._payload.to_dict()

                if self._sender:
                    self._sender(data)
                    logger.debug("心跳已发送: status=%s task=%s",
                                 data["status"], data["current_task_id"])
            except Exception as e:
                logger.error("心跳发送异常: %s", e)

            slept = 0.0
            while slept < interval and self._running:
                time.sleep(0.5)
                slept += 0.5
