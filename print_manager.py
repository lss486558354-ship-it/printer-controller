#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印任务管理器 — 打印生命周期管理 + 后台计时线程。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

from config import PrintSettings

logger = logging.getLogger("print_manager")


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    FAULTED = "faulted"


@dataclass
class PrintTask:
    """一个打印任务。"""
    task_id: str
    task_type: str = ""            # "A" 或 "B"，对应不同打印分支
    estimated_minutes: float = 60.0  # 预估时长(分钟)
    buffer_minutes: float = 5.0     # 缓冲时间(分钟)
    status: TaskStatus = TaskStatus.PENDING
    started_at: float = 0.0
    progress_pct: float = 0.0
    extra: dict = None

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}

    @property
    def deadline(self) -> float:
        """预计完成时间戳。"""
        if self.started_at <= 0:
            return 0
        return self.started_at + (self.estimated_minutes + self.buffer_minutes) * 60.0

    @property
    def elapsed_minutes(self) -> float:
        if self.started_at <= 0:
            return 0.0
        return (time.time() - self.started_at) / 60.0


class PrintManager:
    """打印任务管理器：并发控制、后台计时、状态上报。"""

    def __init__(self, settings: PrintSettings):
        self._cfg = settings
        self._active_task: Optional[PrintTask] = None
        self._timer_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cancel_flag = threading.Event()
        self._pixel_monitor = None

        # 回调
        self._on_status_change: Optional[Callable[[PrintTask], None]] = None
        self._on_complete: Optional[Callable[[PrintTask], None]] = None
        self._on_cancel: Optional[Callable[[PrintTask], None]] = None
        self._on_timeout: Optional[Callable[[PrintTask], None]] = None
        self._on_progress: Optional[Callable[[PrintTask, float], None]] = None
        self._replay_fn: Optional[Callable[[str, float, float], bool]] = None
        self._mcu_send_fn: Optional[Callable[[], bool]] = None

    # ── 回调设置 ───────────────────────────────────────────────

    def set_status_callback(self, fn: Callable[[PrintTask], None]):
        self._on_status_change = fn

    def set_complete_callback(self, fn: Callable[[PrintTask], None]):
        self._on_complete = fn

    def set_cancel_callback(self, fn: Callable[[PrintTask], None]):
        self._on_cancel = fn

    def set_timeout_callback(self, fn: Callable[[PrintTask], None]):
        self._on_timeout = fn

    def set_progress_callback(self, fn: Callable[[PrintTask, float], None]):
        self._on_progress = fn

    def set_replay_callback(self, fn: Callable[[str, float, float], bool]):
        self._replay_fn = fn

    def set_mcu_callback(self, fn: Callable[[], bool]):
        self._mcu_send_fn = fn

    def set_pixel_monitor(self, monitor):
        """注入像素监测器，用于检测打印完成状态。"""
        self._pixel_monitor = monitor

    # ── 状态查询 ───────────────────────────────────────────────

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._active_task is not None and \
                   self._active_task.status == TaskStatus.RUNNING

    @property
    def active_task(self) -> Optional[PrintTask]:
        with self._lock:
            return self._active_task

    # ── 任务调度 ───────────────────────────────────────────────

    def submit(self, task: PrintTask) -> str:
        """
        提交打印任务。返回 "ok" / "busy"。
        - "ok": 任务已启动
        - "busy": 当前有任务正在运行
        """
        with self._lock:
            if self._active_task and self._active_task.status == TaskStatus.RUNNING:
                logger.warning("任务忙，拒绝新任务: %s", task.task_id)
                return "busy"
            self._active_task = task

        # 启动任务
        self._start_task(task)
        return "ok"

    def cancel(self) -> bool:
        """取消当前打印任务。返回是否成功。"""
        with self._lock:
            if not self._active_task or \
               self._active_task.status != TaskStatus.RUNNING:
                return False
            self._cancel_flag.set()
            self._active_task.status = TaskStatus.CANCELLED
            task = self._active_task
        logger.info("任务已取消: %s", task.task_id)
        if self._on_cancel:
            self._on_cancel(task)
        if self._on_status_change:
            self._on_status_change(task)
        return True

    # ── 内部 ───────────────────────────────────────────────────

    def _start_task(self, task: PrintTask):
        """启动任务：回放录制 + 启动后台计时线程。"""
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self._cancel_flag.clear()

        logger.info("开始执行打印任务: %s (类型=%s, 预估 %.0fmin)",
                    task.task_id, task.task_type, task.estimated_minutes)

        if self._on_status_change:
            self._on_status_change(task)

        # 回放启动打印的录制
        self._replay_start_print(task)

        # 启动后台计时线程
        self._timer_thread = threading.Thread(
            target=self._timer_loop, args=(task,), daemon=True, name="print-timer"
        )
        self._timer_thread.start()

    def _get_branch_config(self, task: PrintTask):
        """根据 task_type 获取对应分支配置，无效/空类型回退到默认配置。"""
        if task.task_type == "A":
            return (self._cfg.task_branch_a if hasattr(self._cfg, "task_branch_a")
                    else None)
        elif task.task_type == "B":
            return (self._cfg.task_branch_b if hasattr(self._cfg, "task_branch_b")
                    else None)
        return None

    def _replay_start_print(self, task: PrintTask):
        """回放打印相关的操作录制（根据分支选择不同录制）。"""
        if not self._replay_fn:
            return

        branch = self._get_branch_config(task)

        if branch and (branch.upload_task_recording or branch.start_print_recording):
            # 使用分支专属录制
            upload_rec = branch.upload_task_recording
            start_rec = branch.start_print_recording
            logger.info("使用分支 %s 配置", task.task_type)
        else:
            # 回退到通用录制
            upload_rec = self._cfg.upload_task_recording
            start_rec = self._cfg.start_print_recording

        if upload_rec:
            logger.info("回放: %s", upload_rec)
            self._replay_fn(upload_rec, speed=1.0, delay=1.0)

        if start_rec:
            logger.info("回放: %s", start_rec)
            self._replay_fn(start_rec, speed=1.0, delay=1.0)

    def _timer_loop(self, task: PrintTask):
        """后台计时线程：监控打印进度，处理超时和取消。"""
        check_interval = self._cfg.status_check_interval_s

        # 分支特定参数（超时、额外超时）
        branch = self._get_branch_config(task)
        if branch:
            extra_timeout_minutes = branch.extra_timeout_minutes
        else:
            extra_timeout_minutes = self._cfg.extra_timeout_minutes

        total_timeout = (task.estimated_minutes + task.buffer_minutes) * 60.0
        extra_timeout = extra_timeout_minutes * 60.0
        extra_count = 0
        max_extra_rounds = 3  # 最多追加 3 次额外超时

        deadline = time.time() + total_timeout

        while task.status == TaskStatus.RUNNING:
            if self._cancel_flag.is_set():
                task.status = TaskStatus.CANCELLED
                logger.info("任务 %s 已被取消", task.task_id)
                if self._on_cancel:
                    self._on_cancel(task)
                return

            # 检查打印完成（像素检测）— 支持分支专属完成区域
            if self._check_print_complete(task):
                self._handle_complete(task)
                return

            # 检查超时
            now = time.time()
            if now >= deadline:
                if extra_count < max_extra_rounds:
                    extra_count += 1
                    deadline = now + extra_timeout
                    logger.info("任务 %s 超时，追加 %d 分钟 (第 %d 次)",
                                task.task_id, extra_timeout_minutes, extra_count)
                    if self._on_timeout:
                        self._on_timeout(task)
                else:
                    logger.error("任务 %s 超时且已达最大追加次数", task.task_id)
                    task.status = TaskStatus.TIMEOUT
                    if self._on_timeout:
                        self._on_timeout(task)
                    if self._on_status_change:
                        self._on_status_change(task)
                    return

            # 更新进度
            if task.estimated_minutes > 0 and task.started_at > 0:
                elapsed = (now - task.started_at) / 60.0
                task.progress_pct = min(99.0, (elapsed / task.estimated_minutes) * 100.0)
                if self._on_progress:
                    self._on_progress(task, task.progress_pct)

            time.sleep(check_interval)

    def _check_print_complete(self, task: PrintTask = None) -> bool:
        """通过像素检测判断打印是否完成。优先使用分支专属配置。"""
        # 选择完成检测区域和颜色：分支 > 通用
        branch = self._get_branch_config(task) if task else None
        if branch and branch.complete_region:
            region_label = branch.complete_region
            target_color = branch.complete_color
        else:
            region_label = self._cfg.complete_region
            target_color = self._cfg.complete_color

        if not region_label or not self._pixel_monitor:
            return False
        try:
            from pixel_monitor import _capture_region_rgb, _hex_to_rgb, _scan_rgb_for_color
            region = self._pixel_monitor.get_region(region_label)
            if not region:
                return False
            data = _capture_region_rgb(region.x1, region.y1, region.x2, region.y2)
            if not data:
                return False
            tr, tg, tb = _hex_to_rgb(target_color)
            return _scan_rgb_for_color(data, tr, tg, tb, tolerance=20)
        except Exception:
            return False

    def _handle_complete(self, task: PrintTask):
        """处理打印完成。"""
        task.status = TaskStatus.COMPLETED
        task.progress_pct = 100.0
        logger.info("打印任务完成: %s", task.task_id)

        # 向 MCU 发送完成指令
        if self._mcu_send_fn:
            self._mcu_send_fn()

        if self._on_complete:
            self._on_complete(task)
        if self._on_status_change:
            self._on_status_change(task)
