#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端 HTTP 客户端 — 拉取命令、下载文件、上报状态
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from cloud_config import CloudConfig

logger = logging.getLogger("cloud_client")


class CloudError(Exception):
    pass


class CloudClient:
    """云端 API 客户端，含重试和超时。"""

    def __init__(self, config: CloudConfig):
        self._cfg = config
        self._hostname = socket.gethostname()

    @property
    def _base_url(self) -> str:
        return self._cfg.server_url.rstrip("/")

    @property
    def _auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"
        return headers

    # ── HTTP 基础请求 ──────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict = None, timeout: int = 30) -> dict:
        url = f"{self._base_url}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=self._auth_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise CloudError(f"HTTP {e.code} {method} {path}: {body_text[:200]}")
        except urllib.error.URLError as e:
            raise CloudError(f"连接失败 {method} {path}: {e.reason}")
        except json.JSONDecodeError:
            raise CloudError(f"响应 JSON 解析失败 {method} {path}")

    # ── 业务接口 ───────────────────────────────────────────────

    def send_ready(
        self,
        version: str = "1.0.0",
        extra: dict = None,
    ) -> bool:
        """启动时发送就绪信号，返回 True 表示云端已确认。"""
        payload = {
            "hostname": self._hostname,
            "version": version,
            "started_at": int(time.time()),
        }
        if extra:
            payload.update(extra)

        try:
            self._request("POST", "/api/v1/agent/ready", body=payload, timeout=10)
            logger.info("就绪信号已发送 — %s", self._hostname)
            return True
        except CloudError as e:
            logger.warning("就绪信号发送失败: %s", e)
            return False

    def send_heartbeat(self) -> bool:
        """发送心跳，返回是否成功。"""
        payload = {
            "hostname": self._hostname,
            "timestamp": int(time.time()),
        }
        try:
            self._request("POST", "/api/v1/agent/heartbeat", body=payload, timeout=10)
            return True
        except CloudError:
            return False

    def poll_commands(self) -> list[dict]:
        """轮询待处理的命令。返回命令列表。"""
        resp = self._request("GET", "/api/v1/commands?status=pending", timeout=15)
        return resp.get("commands", [])

    def report_command_status(
        self,
        command_id: str,
        status: str,  # "acknowledged", "downloaded", "completed", "failed"
        message: str = "",
    ) -> bool:
        """上报命令执行状态。"""
        self._request(
            "POST",
            f"/api/v1/commands/{command_id}/status",
            body={"status": status, "message": message, "hostname": self._hostname},
            timeout=10,
        )
        return True

    def download_file(self, file_url: str, dest_path: Path, expected_sha256: str = "") -> Path:
        """
        下载文件到 dest_path（自动创建父目录）。
        如提供 expected_sha256，下载后校验。
        返回下载完成的路径。
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        req = urllib.request.Request(file_url)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                content = resp.read()
        except urllib.error.URLError as e:
            raise CloudError(f"文件下载失败 {file_url}: {e.reason}")

        # 可选 SHA-256 校验
        if expected_sha256:
            actual = hashlib.sha256(content).hexdigest()
            if actual != expected_sha256:
                raise CloudError(
                    f"SHA-256 校验不匹配 — 期望 {expected_sha256[:16]}..., 实际 {actual[:16]}..."
                )

        dest_path.write_bytes(content)
        logger.info("文件已下载: %s (%d bytes)", dest_path, len(content))
        return dest_path
