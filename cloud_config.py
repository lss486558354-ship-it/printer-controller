#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端配置管理 — 加载/保存/验证 cloud_config.json
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional


CONFIG_PATH = Path(__file__).parent / "cloud_config.json"

DEFAULT_CONFIG = {
    "enabled": False,
    "server_url": "",
    "api_key": "",
    "poll_interval_seconds": 10,
    "history_dir": "./cloud_history",
    "exec_dir": "./cloud_exec",
}


@dataclass
class CloudConfig:
    enabled: bool = False
    server_url: str = ""
    api_key: str = ""
    poll_interval_seconds: int = 10
    history_dir: str = "./cloud_history"
    exec_dir: str = "./cloud_exec"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "CloudConfig":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cfg = data.get("cloud", data)
                return cls(
                    enabled=bool(cfg.get("enabled", False)),
                    server_url=str(cfg.get("server_url", "")),
                    api_key=str(cfg.get("api_key", "")),
                    poll_interval_seconds=int(cfg.get("poll_interval_seconds", 10)),
                    history_dir=str(cfg.get("history_dir", "./cloud_history")),
                    exec_dir=str(cfg.get("exec_dir", "./cloud_exec")),
                )
            except Exception:
                pass
        return cls()

    def save(self, path: Path = CONFIG_PATH) -> None:
        data = {"cloud": asdict(self)}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def validate(self) -> Optional[str]:
        """返回 None 表示通过，否则返回错误描述。"""
        if self.enabled:
            if not self.server_url.strip():
                return "服务器地址不能为空"
            if not self.server_url.startswith(("http://", "https://")):
                return "服务器地址必须以 http:// 或 https:// 开头"
            if self.poll_interval_seconds < 1:
                return "轮询间隔不能小于 1 秒"
            if self.poll_interval_seconds > 3600:
                return "轮询间隔不能大于 3600 秒"
        return None
