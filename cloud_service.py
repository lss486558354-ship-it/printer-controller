#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端通信服务 — 双向消息：接收打印任务 + 发送状态。"""
from __future__ import annotations

import logging
import socket
import time
import threading
from pathlib import Path
from typing import Optional, Callable

from config import CloudSettings

logger = logging.getLogger("cloud_service")

VERSION = "2.0.0"


class CloudMessage:
    """云端消息。"""
    def __init__(self, msg_type: str, data: dict = None):
        self.type = msg_type  # print_task/print_task_a/print_task_b, cancel, restart, ping, update_software, update_config, update_appimage, update_recording
        self.data = data or {}
        self.raw: dict = {}

    @classmethod
    def from_dict(cls, raw: dict) -> "CloudMessage":
        msg = cls(msg_type=raw.get("type", ""), data=raw.get("data", {}))
        msg.raw = raw
        return msg


class CloudService:
    """云端后台服务 — 命令轮询 + 状态上报 + 心跳。"""

    def __init__(self, settings: CloudSettings):
        self._cfg = settings
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._hostname = socket.gethostname()
        self._status_callback: Optional[Callable[[str], None]] = None
        self._message_callback: Optional[Callable[[CloudMessage], None]] = None

    @property
    def running(self) -> bool:
        return self._running

    def set_status_callback(self, callback: Callable[[str], None]):
        self._status_callback = callback

    def set_message_callback(self, callback: Callable[[CloudMessage], None]):
        """设置消息回调 — 收到打印任务/取消等消息时调用。"""
        self._message_callback = callback

    def _report(self, msg: str):
        logger.info(msg)
        if self._status_callback:
            self._status_callback(msg)

    # ── 启动 / 停止 ────────────────────────────────────────────

    def start(self):
        if not self._cfg.enabled:
            self._report("云服务未启用")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="cloud-service")
        self._thread.start()
        self._report("云端服务已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._report("云端服务已停止")

    # ── 主循环 ─────────────────────────────────────────────────

    def _run(self):
        self._send_ready()

        while self._running:
            try:
                self._poll_and_dispatch()
            except Exception as e:
                logger.error("云服务轮询异常: %s", e)

            slept = 0.0
            while slept < self._cfg.poll_interval_seconds and self._running:
                time.sleep(0.5)
                slept += 0.5

    def _poll_and_dispatch(self):
        """拉取并分发云端命令。"""
        commands = self._fetch_commands()
        if not commands:
            return

        for cmd in commands:
            if not self._running:
                break
            msg = CloudMessage.from_dict(cmd)
            logger.info("收到云端消息: type=%s data=%s", msg.type, msg.data)

            if self._message_callback:
                self._message_callback(msg)

    def _fetch_commands(self) -> list[dict]:
        """从云端拉取待处理命令。使用简单 HTTP。"""
        import urllib.request
        import urllib.error
        import json

        url = f"{self._cfg.server_url.rstrip('/')}/api/v1/commands?status=pending"
        headers = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("commands", [])
        except Exception:
            return []

    def _send_ready(self):
        """发送就绪/空闲状态到云端。"""
        import urllib.request
        import urllib.error
        import json

        url = f"{self._cfg.server_url.rstrip('/')}/api/v1/agent/ready"
        payload = json.dumps({
            "hostname": self._hostname,
            "version": VERSION,
            "status": "idle",
            "timestamp": int(time.time()),
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=10)
            logger.info("已发送就绪/空闲状态")
        except Exception as e:
            logger.warning("发送就绪状态失败: %s", e)

    # ── 发送方法（供外部调用）─────────────────────────────────

    def send_status(self, status: str, task_id: str = "",
                    message: str = "", extra: dict = None) -> bool:
        """发送设备状态到云端。"""
        import urllib.request
        import urllib.error
        import json

        url = f"{self._cfg.server_url.rstrip('/')}/api/v1/agent/status"
        payload = {
            "hostname": self._hostname,
            "status": status,
            "task_id": task_id,
            "message": message,
            "timestamp": int(time.time()),
        }
        if extra:
            payload.update(extra)

        headers = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception as e:
            logger.error("发送状态失败: %s", e)
            return False

    def send_heartbeat(self, heartbeat_data: dict) -> bool:
        """发送完整心跳包。"""
        import urllib.request
        import urllib.error
        import json

        url = f"{self._cfg.server_url.rstrip('/')}/api/v1/agent/heartbeat"
        payload = {
            "hostname": self._hostname,
            **heartbeat_data,
        }

        headers = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            return False

    def send_error(self, error_code: str, message: str = "") -> bool:
        """发送错误到云端。"""
        return self.send_status("error", message=f"[{error_code}] {message}")
