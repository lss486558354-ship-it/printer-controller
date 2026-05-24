#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备持久化状态 — 等价于 Claude Code 的 .claude.json。"""
from __future__ import annotations

import json
import hashlib
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("device_state")

STATE_PATH = Path(__file__).parent / ".device_state.json"
VERSION = "2.0.0"


def _machine_id() -> str:
    """生成设备唯一标识（基于 hostname + MAC 地址的稳定哈希）。"""
    raw = socket.gethostname() + str(uuid.getnode())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class DeviceState:
    """设备级持久化状态，跨会话保留。"""

    # ── 生命周期 ──
    num_startups: int = 0
    first_start_time: str = ""          # ISO 时间戳
    install_method: str = ""            # "manual" | "pip" | "image"
    migration_version: int = 1

    # ── 引导 ──
    has_completed_onboarding: bool = False
    last_onboarding_version: str = ""

    # ── 信任 ──
    has_trust_accepted: bool = False

    # ── 会话 ──
    last_session_id: str = ""
    last_session_start: str = ""
    last_session_duration: float = 0.0
    last_graceful_shutdown: bool = True

    # ── 设备 ──
    device_id: str = ""
    device_name: str = ""

    # ── 内部（不持久化） ──
    _dirty: bool = field(default=False, repr=False)

    # ── 生命周期方法 ────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "DeviceState":
        """加载设备状态，不存在则创建默认实例。"""
        if not path.exists():
            state = cls()
            state.first_start_time = time.strftime("%Y-%m-%dT%H:%M:%S")
            state.device_id = _machine_id()
            state.install_method = _detect_install_method()
            state._dirty = True
            return state

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("设备状态文件损坏，创建新实例")
            return cls()

        return cls(
            num_startups=data.get("num_startups", 0),
            first_start_time=data.get("first_start_time", ""),
            install_method=data.get("install_method", ""),
            migration_version=data.get("migration_version", 1),
            has_completed_onboarding=data.get("has_completed_onboarding", False),
            last_onboarding_version=data.get("last_onboarding_version", ""),
            has_trust_accepted=data.get("has_trust_accepted", False),
            last_session_id=data.get("last_session_id", ""),
            last_session_start=data.get("last_session_start", ""),
            last_session_duration=data.get("last_session_duration", 0.0),
            last_graceful_shutdown=data.get("last_graceful_shutdown", True),
            device_id=data.get("device_id", _machine_id()),
            device_name=data.get("device_name", socket.gethostname()),
        )

    def save(self, path: Path = STATE_PATH):
        """持久化到磁盘。"""
        data = {
            "num_startups": self.num_startups,
            "first_start_time": self.first_start_time,
            "install_method": self.install_method,
            "migration_version": self.migration_version,
            "has_completed_onboarding": self.has_completed_onboarding,
            "last_onboarding_version": self.last_onboarding_version,
            "has_trust_accepted": self.has_trust_accepted,
            "last_session_id": self.last_session_id,
            "last_session_start": self.last_session_start,
            "last_session_duration": self.last_session_duration,
            "last_graceful_shutdown": self.last_graceful_shutdown,
            "device_id": self.device_id,
            "device_name": self.device_name,
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._dirty = False

    # ── 启动钩子 ────────────────────────────────────────────────

    def on_startup(self) -> str:
        """记录一次启动。返回会话 ID。"""
        self.num_startups += 1
        self.last_session_id = _make_session_id()
        self.last_session_start = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._dirty = True
        return self.last_session_id

    def on_shutdown(self, duration: float):
        """记录正常退出。"""
        self.last_session_duration = duration
        self.last_graceful_shutdown = True
        self._dirty = True
        self.save()

    def on_crash(self):
        """记录异常退出（下次启动可检测）。"""
        self.last_graceful_shutdown = False
        self._dirty = True
        self.save()

    def complete_onboarding(self):
        """标记引导完成。"""
        self.has_completed_onboarding = True
        self.last_onboarding_version = VERSION
        self._dirty = True

    def accept_trust(self):
        """标记信任已接受。"""
        self.has_trust_accepted = True
        self._dirty = True

    # ── 查询 ────────────────────────────────────────────────────

    @property
    def is_first_run(self) -> bool:
        return self.num_startups == 0

    @property
    def needs_onboarding(self) -> bool:
        return not self.has_completed_onboarding

    @property
    def needs_trust(self) -> bool:
        return not self.has_trust_accepted

    @property
    def was_graceful_shutdown(self) -> bool:
        return self.last_graceful_shutdown


def _make_session_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _detect_install_method() -> str:
    """检测安装方式。"""
    if (Path(__file__).parent / ".git").exists():
        return "git"
    if (Path(__file__).parent / "install_service.sh").exists():
        return "manual"
    return "unknown"
