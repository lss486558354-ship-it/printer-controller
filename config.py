#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一配置管理 — 所有模块的配置中心，支持 4 层优先级合并。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).parent / "config.json"
CONFIG_DEFAULTS_PATH = Path(__file__).parent / "config.defaults.json"
CONFIG_LOCAL_PATH = Path(__file__).parent / "config.local.json"


# ── 深度合并工具 ──────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 中的值覆盖 base（创建新 dict）。"""
    result = {**base}
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_json(path: Path) -> dict:
    """安全加载 JSON 文件，失败返回空 dict。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _apply_env_overrides(data: dict, prefix: str = "PRINTER_") -> dict:
    """环境变量覆盖：PRINTER_CLOUD_ENABLED=true → data["cloud"]["enabled"] = True。"""
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("__")
        if len(parts) < 1:
            continue
        # 支持双下划线嵌套：PRINTER_CLOUD__ENABLED → cloud.enabled
        target = data
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        last = parts[-1]
        # 类型推断
        v = value.lower()
        if v in ("true", "yes", "1"):
            target[last] = True
        elif v in ("false", "no", "0"):
            target[last] = False
        elif v.isdigit():
            target[last] = int(v)
        elif _is_float(v):
            target[last] = float(v)
        else:
            target[last] = value
    return data


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ── 云服务配置 ────────────────────────────────────────────────────────

@dataclass
class CloudSettings:
    enabled: bool = False
    server_url: str = ""
    api_key: str = ""
    poll_interval_seconds: int = 10
    heartbeat_interval_seconds: int = 30
    history_dir: str = "./cloud_history"
    exec_dir: str = "./cloud_exec"


# ── BambuLab Studio 配置 ──────────────────────────────────────────────

@dataclass
class BambuSettings:
    appimage_path: str = ""          # BambuStudio AppImage 路径
    launch_recording: str = "打开bambu stdio"  # 启动录制的名称
    cancel_update_recording: str = "取消更新"   # 取消更新弹窗的录制名称
    confirm_region: str = "启动确认"           # 像素检测区域: 确认 Studio 启动成功
    confirm_color: str = "#00AE42"             # 确认颜色 (绿色 logo/success indicator)
    startup_timeout_s: float = 120.0           # 启动超时时间


# ── 任务分支配置 ──────────────────────────────────────────────────────

@dataclass
class TaskBranchConfig:
    """单个任务分支的录制和检测配置。"""
    upload_task_recording: str = ""     # 上传打印任务的录制名称
    start_print_recording: str = ""     # 开始打印的录制名称
    complete_region: str = ""           # 打印完成检测区域(像素监测区域名)
    complete_color: str = "#00FF00"     # 完成指示颜色
    default_timeout_minutes: int = 120  # 默认打印超时(分钟)
    extra_timeout_minutes: int = 10     # 超时后额外等待(分钟)


# ── 打印任务配置 ──────────────────────────────────────────────────────

@dataclass
class PrintSettings:
    upload_task_recording: str = "上传打印任务"  # 上传任务的录制名称
    start_print_recording: str = "daying"        # 开始打印的录制名称
    default_timeout_minutes: int = 120            # 默认打印超时(分钟)
    extra_timeout_minutes: int = 10               # 超时后额外等待(分钟)
    status_check_interval_s: float = 5.0          # 状态检查间隔(秒)
    complete_region: str = ""                     # 打印完成检测区域(可选)
    complete_color: str = "#00FF00"               # 完成指示颜色
    task_branch_a: TaskBranchConfig = field(default_factory=TaskBranchConfig)  # 任务分支A
    task_branch_b: TaskBranchConfig = field(default_factory=TaskBranchConfig)  # 任务分支B


# ── MCU 配置 ──────────────────────────────────────────────────────────

@dataclass
class MCUSettings:
    enabled: bool = False
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    timeout_s: float = 5.0
    complete_command: str = "PRINT_COMPLETE\n"   # 打印完成时发送的命令


# ── 统一配置 ──────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    cloud: CloudSettings = field(default_factory=CloudSettings)
    bambu: BambuSettings = field(default_factory=BambuSettings)
    print: PrintSettings = field(default_factory=PrintSettings)
    mcu: MCUSettings = field(default_factory=MCUSettings)
    device_id: str = ""
    debug: bool = False

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        """4 层优先级加载: 默认值 → managed-defaults → 用户配置 → 本地覆盖 → 环境变量。"""
        # Layer 1: 从 managed defaults 加载（如存在）
        managed_data = _load_json(CONFIG_DEFAULTS_PATH)

        # Layer 2: 用户配置
        user_data = _load_json(path)

        # Layer 3: 本地覆盖（gitignore）
        local_data = _load_json(CONFIG_LOCAL_PATH)

        # 合并: L1 → L2 → L3
        merged = _deep_merge(managed_data, user_data)
        merged = _deep_merge(merged, local_data)

        # Layer 4: 环境变量（最高优先级）
        merged = _apply_env_overrides(merged)

        return cls._from_dict(merged)

    @classmethod
    def _from_dict(cls, data: dict) -> "AppConfig":
        """从字典构建 AppConfig。"""
        cfg = cls()

        # 云服务
        cloud_data = data.get("cloud", {})
        cfg.cloud = CloudSettings(
            enabled=bool(cloud_data.get("enabled", False)),
            server_url=str(cloud_data.get("server_url", "")),
            api_key=str(cloud_data.get("api_key", "")),
            poll_interval_seconds=int(cloud_data.get("poll_interval_seconds", 10)),
            heartbeat_interval_seconds=int(cloud_data.get("heartbeat_interval_seconds", 30)),
            history_dir=str(cloud_data.get("history_dir", "./cloud_history")),
            exec_dir=str(cloud_data.get("exec_dir", "./cloud_exec")),
        )

        # BambuLab Studio
        bambu_data = data.get("bambu", {})
        cfg.bambu = BambuSettings(
            appimage_path=str(bambu_data.get("appimage_path", "")),
            launch_recording=str(bambu_data.get("launch_recording", "打开bambu stdio")),
            cancel_update_recording=str(bambu_data.get("cancel_update_recording", "取消更新")),
            confirm_region=str(bambu_data.get("confirm_region", "启动确认")),
            confirm_color=str(bambu_data.get("confirm_color", "#00AE42")),
            startup_timeout_s=float(bambu_data.get("startup_timeout_s", 120.0)),
        )

        # 打印任务
        print_data = data.get("print", {})
        cfg.print = PrintSettings(
            upload_task_recording=str(print_data.get("upload_task_recording", "上传打印任务")),
            start_print_recording=str(print_data.get("start_print_recording", "daying")),
            default_timeout_minutes=int(print_data.get("default_timeout_minutes", 120)),
            extra_timeout_minutes=int(print_data.get("extra_timeout_minutes", 10)),
            status_check_interval_s=float(print_data.get("status_check_interval_s", 5.0)),
            complete_region=str(print_data.get("complete_region", "")),
            complete_color=str(print_data.get("complete_color", "#00FF00")),
            task_branch_a=TaskBranchConfig(
                upload_task_recording=str(print_data.get("task_branch_a", {}).get("upload_task_recording", "")),
                start_print_recording=str(print_data.get("task_branch_a", {}).get("start_print_recording", "")),
                complete_region=str(print_data.get("task_branch_a", {}).get("complete_region", "")),
                complete_color=str(print_data.get("task_branch_a", {}).get("complete_color", "#00FF00")),
                default_timeout_minutes=int(print_data.get("task_branch_a", {}).get("default_timeout_minutes", 120)),
                extra_timeout_minutes=int(print_data.get("task_branch_a", {}).get("extra_timeout_minutes", 10)),
            ),
            task_branch_b=TaskBranchConfig(
                upload_task_recording=str(print_data.get("task_branch_b", {}).get("upload_task_recording", "")),
                start_print_recording=str(print_data.get("task_branch_b", {}).get("start_print_recording", "")),
                complete_region=str(print_data.get("task_branch_b", {}).get("complete_region", "")),
                complete_color=str(print_data.get("task_branch_b", {}).get("complete_color", "#00FF00")),
                default_timeout_minutes=int(print_data.get("task_branch_b", {}).get("default_timeout_minutes", 120)),
                extra_timeout_minutes=int(print_data.get("task_branch_b", {}).get("extra_timeout_minutes", 10)),
            ),
        )

        # MCU
        mcu_data = data.get("mcu", {})
        cfg.mcu = MCUSettings(
            enabled=bool(mcu_data.get("enabled", False)),
            port=str(mcu_data.get("port", "/dev/ttyUSB0")),
            baudrate=int(mcu_data.get("baudrate", 115200)),
            timeout_s=float(mcu_data.get("timeout_s", 5.0)),
            complete_command=str(mcu_data.get("complete_command", "PRINT_COMPLETE\n")),
        )

        cfg.device_id = str(data.get("device_id", ""))
        cfg.debug = bool(data.get("debug", False))
        return cfg

    def save(self, path: Path = CONFIG_PATH):
        data = {
            "device_id": self.device_id,
            "debug": self.debug,
            "cloud": {
                "enabled": self.cloud.enabled,
                "server_url": self.cloud.server_url,
                "api_key": self.cloud.api_key,
                "poll_interval_seconds": self.cloud.poll_interval_seconds,
                "heartbeat_interval_seconds": self.cloud.heartbeat_interval_seconds,
                "history_dir": self.cloud.history_dir,
                "exec_dir": self.cloud.exec_dir,
            },
            "bambu": {
                "appimage_path": self.bambu.appimage_path,
                "launch_recording": self.bambu.launch_recording,
                "cancel_update_recording": self.bambu.cancel_update_recording,
                "confirm_region": self.bambu.confirm_region,
                "confirm_color": self.bambu.confirm_color,
                "startup_timeout_s": self.bambu.startup_timeout_s,
            },
            "print": {
                "upload_task_recording": self.print.upload_task_recording,
                "start_print_recording": self.print.start_print_recording,
                "default_timeout_minutes": self.print.default_timeout_minutes,
                "extra_timeout_minutes": self.print.extra_timeout_minutes,
                "status_check_interval_s": self.print.status_check_interval_s,
                "complete_region": self.print.complete_region,
                "complete_color": self.print.complete_color,
                "task_branch_a": {
                    "upload_task_recording": self.print.task_branch_a.upload_task_recording,
                    "start_print_recording": self.print.task_branch_a.start_print_recording,
                    "complete_region": self.print.task_branch_a.complete_region,
                    "complete_color": self.print.task_branch_a.complete_color,
                    "default_timeout_minutes": self.print.task_branch_a.default_timeout_minutes,
                    "extra_timeout_minutes": self.print.task_branch_a.extra_timeout_minutes,
                },
                "task_branch_b": {
                    "upload_task_recording": self.print.task_branch_b.upload_task_recording,
                    "start_print_recording": self.print.task_branch_b.start_print_recording,
                    "complete_region": self.print.task_branch_b.complete_region,
                    "complete_color": self.print.task_branch_b.complete_color,
                    "default_timeout_minutes": self.print.task_branch_b.default_timeout_minutes,
                    "extra_timeout_minutes": self.print.task_branch_b.extra_timeout_minutes,
                },
            },
            "mcu": {
                "enabled": self.mcu.enabled,
                "port": self.mcu.port,
                "baudrate": self.mcu.baudrate,
                "timeout_s": self.mcu.timeout_s,
                "complete_command": self.mcu.complete_command,
            },
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def validate(self) -> Optional[str]:
        if self.cloud.enabled:
            if not self.cloud.server_url.strip():
                return "云服务已启用但 server_url 为空"
            if not self.cloud.server_url.startswith(("http://", "https://")):
                return "server_url 必须以 http:// 或 https:// 开头"
            if self.cloud.poll_interval_seconds < 1:
                return "poll_interval_seconds 不能小于 1 秒"
        if self.mcu.enabled:
            if not self.mcu.port.strip():
                return "MCU 已启用但 port 为空"
        return None
