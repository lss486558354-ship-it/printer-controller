#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端文件管理器 — 历史目录 + 执行目录
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cloud_file_manager")


class CloudFileManager:
    """管理云端下载文件的双目录策略。

    - history_dir: 保留所有历史文件，同名文件加 _x 计数后缀防止覆盖
    - exec_dir:   只保留当前任务文件，处理前清空旧文件
    """

    def __init__(self, history_dir: str, exec_dir: str):
        self._history = Path(history_dir).resolve()
        self._exec = Path(exec_dir).resolve()

    @property
    def history_dir(self) -> Path:
        return self._history

    @property
    def exec_dir(self) -> Path:
        return self._exec

    def init_dirs(self):
        """初始化目录结构。"""
        self._history.mkdir(parents=True, exist_ok=True)
        self._exec.mkdir(parents=True, exist_ok=True)

    def save_to_history(self, file_path: Path, command_id: str = "") -> Path:
        """
        将文件存入历史目录，自动添加 _x 计数后缀避免重名。
        返回历史路径。
        """
        self._history.mkdir(parents=True, exist_ok=True)

        stem = file_path.stem
        suffix = file_path.suffix

        # 计算下一个可用的 _x 编号
        existing = list(self._history.glob(f"{stem}*{suffix}"))
        max_idx = 0
        pattern = re.compile(rf"^{re.escape(stem)}_(\d+){re.escape(suffix)}$")
        for p in existing:
            m = pattern.match(p.name)
            if m:
                max_idx = max(max_idx, int(m.group(1)))

        next_idx = max_idx + 1
        dest_name = f"{stem}_{next_idx}{suffix}"
        dest = self._history / dest_name

        shutil.copy2(file_path, dest)
        logger.info("历史存档: %s", dest)
        return dest

    def clean_exec_dir(self):
        """清空执行目录。"""
        if self._exec.exists():
            for item in self._exec.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            logger.info("执行目录已清空: %s", self._exec)

    def place_in_exec(self, file_path: Path) -> Path:
        """
        将文件放入执行目录（同名覆盖，先清空目录中其他文件）。
        返回执行目录中的文件路径。
        """
        self.clean_exec_dir()
        self._exec.mkdir(parents=True, exist_ok=True)

        dest = self._exec / file_path.name
        shutil.copy2(file_path, dest)
        logger.info("已放入执行目录: %s", dest)
        return dest
