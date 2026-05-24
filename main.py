#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3D 打印机自动化控制器 — 主入口
================================
启动生命周期:
  阶段0: 加载设备状态 → 检测首次运行
  阶段1: 引导流程（首次运行: 欢迎 → 依赖检查 → 信任确认）
  阶段2: 4 层配置加载（默认 → managed → 用户 → 本地 → 环境变量）
  阶段3: Setup 钩子执行（--init / --init-only / --maintenance）
  阶段4: 初始化组件 → 启动 BambuLab Studio → 连接云端 → 主状态机

主状态机:
  空闲 ── 接收云端消息（打印任务A/B、取消、重启等）
    ├─ 任务忙 → 返回错误
    └─ 空闲 → 启动打印 + 后台计时 → 主线程立即切回空闲

CLI 标志:
  --init          执行 Setup 钩子后进入正常运行
  --init-only     仅执行 Setup 钩子，然后退出
  --maintenance   执行维护类 Setup 钩子后进入正常运行
  --skip-onboarding  跳过首次引导流程

平台: Ubuntu 22.04 + Xorg (xdotool + pynput)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import tarfile
import time
import threading
import urllib.request
from pathlib import Path

# ── 项目路径 ──
PROJECT_DIR = Path(__file__).parent

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PROJECT_DIR / "controller.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


# ── 导入项目模块 ──
from config import AppConfig, MCUSettings

# 设备状态
from device_state import DeviceState, VERSION as APP_VERSION

# Setup 钩子
from setup_hooks import load_hooks, run_hooks, print_results

# 鼠标录制回放引擎（跨平台）
from mouse_recorder import play_back, DEFAULT_START_DELAY, RECORDINGS_DIR, IS_LINUX

# 云端服务
from cloud_service import CloudService, CloudMessage

# 心跳服务
from heartbeat import HeartbeatService, DeviceStatus

# 打印任务管理器
from print_manager import PrintManager, PrintTask, TaskStatus

# BambuLab Studio 启动器
from app_launcher import AppLauncher

# MCU 通信桥
from mcu_bridge import MCUBridge

# 像素监测
from pixel_monitor import PixelMonitor, PixelRegion, _capture_region_rgb, _scan_rgb_for_color, _hex_to_rgb

# 状态机引擎
from state_machine import StateMachineEngine, SMConfig, SMCloudTrigger, load_all_jump_rules


# ══════════════════════════════════════════════════════════════════
# 引导流程
# ══════════════════════════════════════════════════════════════════

def _run_onboarding(device: DeviceState) -> bool:
    """首次运行引导：欢迎 → 依赖检查 → 信任确认。返回是否继续。"""
    print(f"\n{'=' * 60}")
    print(f"  3D 打印机自动化控制系统 v{APP_VERSION}")
    print(f"  设备 ID: {device.device_id}")
    print(f"{'=' * 60}")

    if not device.is_first_run:
        return True

    print("\n  这是首次运行，将进行初始配置...\n")

    # 步骤 1: 依赖检查
    print("[1/3] 检查依赖...")
    try:
        from pixel_monitor import _capture_region_rgb
        from pynput import mouse
        print("  [OK] pynput")
        from PIL import ImageGrab
        print("  [OK] pillow")
        if sys.platform == "linux":
            import shutil as _shutil
            if _shutil.which("xdotool"):
                print("  [OK] xdotool")
            else:
                print("  [WARN] xdotool: sudo apt install xdotool")
        print("  依赖检查通过\n")
    except ImportError as e:
        print(f"  [FAIL] Missing: {e}")
        print("  请运行: pip install pynput pillow")
        print("  安装后重新启动。\n")
        return False

    # 步骤 2: 信任确认
    print("[2/3] 使用协议确认...")
    print("  本软件将控制鼠标键盘操作 BambuLab Studio 进行自动化打印。")
    print("  请确保已了解自动化操作的风险。\n")

    if not sys.stdin.isatty():
        # 非交互模式，自动接受（用于 systemd 服务）
        print("  非交互终端，自动接受使用协议。\n")
    else:
        try:
            answer = input("  是否接受并继续? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("  已取消。\n")
                return False
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。\n")
            return False

    device.accept_trust()

    # 步骤 3: 设备名称
    print("\n[3/3] 设备标识...")
    device_id = device.device_id
    print(f"  自动生成的设备 ID: {device_id}")
    if sys.stdin.isatty():
        try:
            custom = input(f"  输入设备名称 (回车使用默认): ").strip()
            if custom:
                device.device_name = custom
        except (EOFError, KeyboardInterrupt):
            pass

    device.complete_onboarding()
    device.save()
    print(f"\n  引导完成! 设备名称: {device.device_name}\n")
    return True


# ══════════════════════════════════════════════════════════════════
# 控制器主类
# ══════════════════════════════════════════════════════════════════

class PrinterController:
    """3D 打印机自动化控制器。"""

    _UPDATE_HANDLERS = {
        "update_software": "_upgrade_software",
        "update_config": "_upgrade_config",
        "update_appimage": "_upgrade_appimage",
        "update_recording": "_upgrade_recording",
    }

    def __init__(self, config: AppConfig = None, device_state: DeviceState = None):
        self._cfg = config or AppConfig.load()
        self._device = device_state or DeviceState.load()
        self._running = False
        self._lock = threading.Lock()
        self._startup_time: float = 0.0

        # 组件
        self.cloud: CloudService = None
        self.heartbeat: HeartbeatService = None
        self.print_mgr: PrintManager = None
        self.app_launcher: AppLauncher = None
        self.mcu: MCUBridge = None
        self.pixel_monitor: PixelMonitor = None
        self._sm_engine: StateMachineEngine = None

    # ── 启动 / 停止 ───────────────────────────────────────────

    def start(self) -> bool:
        """启动控制器: 初始化 → 启动 Studio → 连接云端 → 进入状态机。"""
        logger.info("=" * 60)
        logger.info("打印机控制器 v2.0 启动中...")
        logger.info("  平台: %s", "Linux" if IS_LINUX else "Windows")
        logger.info("  设备ID: %s", self._cfg.device_id or "(未设置)")
        logger.info("=" * 60)

        self._startup_time = time.time()
        self._running = True

        # 记录启动
        self._device.on_startup()
        self._device.save()
        logger.info("会话 ID: %s (第 %d 次启动)",
                    self._device.last_session_id, self._device.num_startups)

        # ── 阶段1: 初始化子组件 ──
        self._init_components()

        # ── 阶段2: 启动 BambuLab Studio ──
        if not self._launch_studio():
            logger.error("BambuLab Studio 启动失败，退出")
            self.stop()
            return False

        # ── 阶段3: 连接云端 ──
        self._connect_cloud()

        # ── 阶段4: 进入主状态机 ──
        self._run_main_loop()

        return True

    def stop(self):
        """停止控制器，清理所有资源。"""
        logger.info("正在停止控制器...")
        self._running = False

        if self._sm_engine:
            self._sm_engine.stop()
        if self.heartbeat:
            self.heartbeat.stop()
        if self.print_mgr:
            if self.print_mgr.is_busy:
                self.print_mgr.cancel()
        if self.cloud:
            self.cloud.send_status("offline", message="设备离线")
            self.cloud.stop()
        if self.mcu:
            self.mcu.disconnect()
        if self.pixel_monitor:
            self.pixel_monitor.stop()

        # 记录正常退出
        duration = time.time() - self._startup_time if self._startup_time > 0 else 0
        self._device.on_shutdown(duration)
        logger.info("控制器已停止 (会话时长: %.0fs)", duration)

    # ── 阶段1: 初始化组件 ─────────────────────────────────────

    def _init_components(self):
        """初始化所有子组件并建立回调连接。"""
        logger.info("─ 初始化子组件 ─")

        # 像素监测器
        self.pixel_monitor = PixelMonitor()

        # 云端服务
        self.cloud = CloudService(self._cfg.cloud)
        self.cloud.set_status_callback(lambda msg: logger.info("[云端] %s", msg))
        self.cloud.set_message_callback(self._on_cloud_message)

        # 心跳服务
        self.heartbeat = HeartbeatService(self._cfg)
        self.heartbeat.set_sender(self.cloud.send_heartbeat)

        # 打印任务管理器
        self.print_mgr = PrintManager(self._cfg.print)
        self.print_mgr.set_status_callback(self._on_print_status_change)
        self.print_mgr.set_complete_callback(self._on_print_complete)
        self.print_mgr.set_cancel_callback(self._on_print_cancelled)
        self.print_mgr.set_timeout_callback(self._on_print_timeout)
        self.print_mgr.set_replay_callback(self._replay_recording)
        self.print_mgr.set_mcu_callback(self._send_mcu_complete)
        self.print_mgr.set_pixel_monitor(self.pixel_monitor)

        # BambuLab Studio 启动器
        self.app_launcher = AppLauncher(self._cfg.bambu)
        self.app_launcher.set_replay_callback(self._replay_recording)

        # MCU
        self.mcu = MCUBridge(self._cfg.mcu)
        if self._cfg.mcu.enabled:
            self.mcu.connect()

        # ── 状态机引擎 ──
        sm_path = PROJECT_DIR / "state_machine.json"
        if sm_path.exists():
            try:
                self._sm_engine = StateMachineEngine.load_config(sm_path)
                self._sm_engine.set_replay_callback(self._replay_recording)
                self._sm_engine.set_cloud_sender(self._sm_send_cloud_status)
                self._sm_engine.set_cloud_guard(self._sm_cloud_guard)
                if self.pixel_monitor:
                    self._sm_engine.set_pixel_monitor(self.pixel_monitor)
                logger.info("  状态机已加载: %s (%d 个状态)",
                            self._sm_engine.config.name,
                            len(self._sm_engine.config.states))
            except Exception as e:
                logger.warning("状态机加载失败，跳过: %s", e)
                self._sm_engine = None
        else:
            logger.info("  状态机配置不存在，跳过")

        logger.info("  所有组件初始化完成")

    # ── 阶段2: 启动 BambuLab Studio ────────────────────────────

    def _launch_studio(self) -> bool:
        """启动 BambuLab Studio 并通过像素确认启动成功。"""
        logger.info("─ 启动 BambuLab Studio ─")

        # 加载像素检测区域
        self._load_pixel_regions()

        # 启动并确认
        ok = self.app_launcher.launch_and_confirm()
        if ok:
            logger.info("  BambuLab Studio 启动成功")
            return True
        else:
            logger.error("  BambuLab Studio 启动失败")
            return False

    def _load_pixel_regions(self):
        """从 pixel_config.json 加载像素监测区域到启动器。"""
        config_path = PROJECT_DIR / "pixel_config.json"
        if not config_path.exists():
            logger.warning("pixel_config.json 不存在，像素检测可能不可用")
            return
        try:
            mon = PixelMonitor.load_config(config_path)
            for region in mon.regions:
                self.app_launcher.add_pixel_region(region)
                self.pixel_monitor.add_region(
                    region.label, region.x1, region.y1, region.x2, region.y2
                )
            logger.info("  已加载 %d 个像素区域", len(mon.regions))
        except Exception as e:
            logger.error("加载像素区域失败: %s", e)

    # ── 阶段3: 连接云端 ───────────────────────────────────────

    def _connect_cloud(self):
        """连接云端、启动状态机、发送空闲状态。"""
        logger.info("─ 连接云端 ─")

        # 启动状态机（控制启动流程：上电 → ... → 空闲）
        if self._sm_engine:
            self._sm_engine.set_jump_rules(load_all_jump_rules())
            self._sm_engine.start()
            logger.info("  状态机已启动: 初始状态=%s", self._sm_engine.current_state)
            # 状态机的 "空闲" 状态 on_enter 会自动发送空闲状态
        elif self._cfg.cloud.enabled:
            # 回退：没有状态机时直接发送空闲状态
            self.cloud.start()
            self.heartbeat.set_status(DeviceStatus.IDLE)
            self.heartbeat.start()
            self.cloud.send_status("idle", message="设备就绪，等待任务")
            logger.info("  已发送空闲状态（直接模式）")
            return

        if not self._cfg.cloud.enabled:
            logger.warning("云服务未启用，跳过云端连接")
            return

        self.cloud.start()
        self.heartbeat.set_status(DeviceStatus.IDLE)
        self.heartbeat.start()

    # ── 阶段4: 主状态机循环 ────────────────────────────────────

    def _run_main_loop(self):
        """主状态机循环: 空闲等待云端消息。"""
        logger.info("─ 进入主状态机 (空闲) ─")
        logger.info("  等待云端打印任务...")

        while self._running:
            time.sleep(0.5)

    # ── 云端消息处理 ──────────────────────────────────────────

    def _on_cloud_message(self, msg: CloudMessage):
        """处理云端消息（主线程安全）。"""
        logger.info("处理云端消息: type=%s data=%s", msg.type, msg.data)

        # ── 优先路由到状态机（如果运行中且有匹配的 cloud_trigger）──
        if self._sm_engine and self._sm_engine.running:
            self._sm_engine.handle_cloud_message(msg.type, msg.data)

        if msg.type in ("print_task", "print_task_a", "print_task_b"):
            self._handle_print_task(msg)
        elif msg.type == "cancel":
            self._handle_cancel(msg)
        elif msg.type == "restart":
            self._handle_restart(msg)
        elif msg.type == "ping":
            self.cloud.send_status("idle", message="pong")
        elif msg.type.startswith("update_"):
            self._handle_update(msg)
        else:
            logger.warning("未知消息类型: %s", msg.type)

    # ── 状态机回调 ──────────────────────────────────────────

    def _sm_send_cloud_status(self, status: str, message: str) -> bool:
        """状态机云端操作回调：发送状态。"""
        if not self.cloud:
            return False
        return self.cloud.send_status(status, message=message)

    def _sm_cloud_guard(self, msg_type: str, data: dict) -> bool:
        """状态机云端转移守卫：检查前置条件。
        对于 print_task 消息，检查打印管理器是否空闲。"""
        if msg_type in ("print_task_a", "print_task_b"):
            if self.print_mgr.is_busy:
                logger.warning("守卫阻止: 打印管理器忙，拒绝 %s", msg_type)
                self.cloud.send_error("TASK_BUSY", "当前有打印任务正在运行")
                return False
        return True

    @staticmethod
    def _extract_task_type(msg_type: str) -> str:
        """从消息类型提取任务分支标识 (A/B)。"""
        if "print_task_a" in msg_type:
            return "A"
        if "print_task_b" in msg_type:
            return "B"
        return "A"  # 默认分支 A

    def _handle_print_task(self, msg: CloudMessage):
        """处理打印任务消息。"""
        task_data = msg.data
        task_id = task_data.get("task_id", f"task_{int(time.time())}")
        task_type = self._extract_task_type(msg.type)

        estimated_minutes = float(task_data.get("estimated_minutes", 60))
        buffer_minutes = float(task_data.get("buffer_minutes", 5))

        logger.info("收到打印任务: id=%s type=%s 预估=%.0fmin 缓冲=%.0fmin",
                    task_id, task_type, estimated_minutes, buffer_minutes)

        # 检查是否忙
        if self.print_mgr.is_busy:
            logger.warning("设备忙，拒绝任务 %s", task_id)
            self.cloud.send_error("TASK_BUSY", "当前有打印任务正在运行")
            return

        # 确认接收
        self.cloud.send_status("accepted", task_id=task_id,
                               message=f"任务 {task_id} 已接收")

        # 创建并提交任务
        task = PrintTask(
            task_id=task_id,
            task_type=task_type,
            estimated_minutes=estimated_minutes,
            buffer_minutes=buffer_minutes,
        )

        result = self.print_mgr.submit(task)
        if result == "busy":
            self.cloud.send_error("TASK_BUSY", "任务提交时检测到冲突")
            return

        # 更新心跳状态
        self.heartbeat.set_status(DeviceStatus.PRINTING)
        self.heartbeat.set_task(task_id, 0.0)

        # 告知云端任务已开始
        self.cloud.send_status("printing", task_id=task_id,
                               message=f"任务 {task_id} 已开始打印")

    def _handle_cancel(self, msg: CloudMessage):
        """处理取消指令。"""
        task_id = msg.data.get("task_id", "")
        logger.info("收到取消指令: task_id=%s", task_id)

        if self.print_mgr.is_busy:
            self.print_mgr.cancel()
            self.cloud.send_status("idle", message="任务已取消")
            self.heartbeat.set_status(DeviceStatus.IDLE)
            self.heartbeat.clear_task()
        else:
            self.cloud.send_error("NO_TASK", "没有正在运行的任务可取消")

    def _handle_restart(self, msg: CloudMessage):
        """处理重启指令。"""
        logger.info("收到重启指令，正在重启...")
        self.cloud.send_status("restarting", message="设备重启中")
        self.stop()
        time.sleep(2)
        self.start()

    # ── 云端升级处理 ─────────────────────────────────────────

    def _handle_update(self, msg: CloudMessage):
        """分派云端升级消息到对应的升级处理器。"""
        update_type = msg.type  # update_software / update_config / update_appimage / update_recording
        logger.info("收到升级指令: %s", update_type)

        if self.print_mgr.is_busy:
            self.cloud.send_error("TASK_BUSY", "设备忙，无法执行升级")
            return

        handler_name = self._UPDATE_HANDLERS.get(update_type)
        if handler_name:
            handler = getattr(self, handler_name)
            try:
                handler(msg.data)
            except Exception as e:
                logger.error("升级失败 (%s): %s", update_type, e)
                self.cloud.send_error("UPDATE_FAILED", str(e))
        else:
            logger.warning("未知升级类型: %s", update_type)
            self.cloud.send_error("UNKNOWN_UPDATE_TYPE", update_type)

    def _upgrade_software(self, data: dict):
        """软件更新: 下载 → SHA256校验 → 备份 → 解压 → 重启。"""
        file_url = data.get("file_url", "")
        sha256 = data.get("sha256", "")
        version = data.get("version", "unknown")
        restart_after = data.get("restart_after", True)

        if not file_url:
            raise ValueError("缺少 file_url")

        logger.info("软件更新: 版本=%s url=%s", version, file_url)
        self.cloud.send_status("updating", message=f"软件更新中: {version}")

        archive_path = self._download_update(file_url, "software_update.tar.gz")
        self._verify_sha256(archive_path, sha256)
        self._backup_current()
        self._extract_archive(archive_path, PROJECT_DIR)

        archive_path.unlink(missing_ok=True)
        logger.info("软件更新完成: %s", version)
        self.cloud.send_status("updated", message=f"软件已更新到 {version}")

        if restart_after:
            logger.info("软件更新后重启...")
            self.cloud.send_status("restarting", message="更新后重启")
            self.stop()
            time.sleep(2)
            self.start()

    def _upgrade_config(self, data: dict):
        """配置更新: 下载 → SHA256校验 → 备份 → 替换配置文件。"""
        file_url = data.get("file_url", "")
        sha256 = data.get("sha256", "")
        config_type = data.get("config_type", "app")  # app / state_machine / pixel

        if not file_url:
            raise ValueError("缺少 file_url")

        target_map = {
            "app": "config.json",
            "state_machine": "state_machine.json",
            "pixel": "pixel_config.json",
        }
        target_file = target_map.get(config_type)
        if not target_file:
            raise ValueError(f"未知配置类型: {config_type}")

        logger.info("配置更新: type=%s url=%s", config_type, file_url)
        self.cloud.send_status("updating", message=f"配置更新中: {config_type}")

        temp_path = self._download_update(file_url, f"update_{target_file}")
        self._verify_sha256(temp_path, sha256)

        target_path = PROJECT_DIR / target_file
        self._backup_file(target_path)
        shutil.copy2(temp_path, target_path)
        temp_path.unlink(missing_ok=True)

        # 重新加载配置
        if config_type == "app":
            self._cfg = AppConfig.load()
        elif config_type == "pixel":
            self._load_pixel_regions()

        logger.info("配置更新完成: %s → %s", config_type, target_file)
        self.cloud.send_status("updated", message=f"配置 {config_type} 已更新")

    def _upgrade_appimage(self, data: dict):
        """AppImage 更新: 下载 → SHA256校验 → 替换。"""
        file_url = data.get("file_url", "")
        sha256 = data.get("sha256", "")
        version = data.get("version", "unknown")

        if not file_url:
            raise ValueError("缺少 file_url")

        logger.info("AppImage 更新: 版本=%s url=%s", version, file_url)
        self.cloud.send_status("updating", message=f"AppImage 更新中: {version}")

        appimage_path = Path(self._cfg.bambu.appimage_path)
        self._backup_file(appimage_path)

        temp_path = self._download_update(file_url, "update.AppImage")
        self._verify_sha256(temp_path, sha256)

        shutil.copy2(temp_path, appimage_path)
        temp_path.unlink(missing_ok=True)
        os.chmod(appimage_path, 0o755)

        logger.info("AppImage 更新完成: %s", version)
        self.cloud.send_status("updated", message=f"AppImage 已更新到 {version}")

    def _upgrade_recording(self, data: dict):
        """录制更新: 下载 → SHA256校验 → 替换录制文件。"""
        file_url = data.get("file_url", "")
        sha256 = data.get("sha256", "")
        name = data.get("name", "")

        if not file_url or not name:
            raise ValueError("缺少 file_url 或 name")

        logger.info("录制更新: 名称=%s url=%s", name, file_url)
        self.cloud.send_status("updating", message=f"录制更新中: {name}")

        recording_path = RECORDINGS_DIR / f"{name}.json"
        self._backup_file(recording_path)

        temp_path = self._download_update(file_url, f"update_{name}.json")
        self._verify_sha256(temp_path, sha256)

        shutil.copy2(temp_path, recording_path)
        temp_path.unlink(missing_ok=True)

        logger.info("录制更新完成: %s", name)
        self.cloud.send_status("updated", message=f"录制 {name} 已更新")

    # ── 升级辅助方法 ─────────────────────────────────────────

    _SHA256_CHUNK = 64 * 1024

    def _download_update(self, url: str, filename: str) -> Path:
        """下载更新文件到临时目录（带超时）。"""
        dest = PROJECT_DIR / "cloud_exec" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        logger.info("下载更新: %s → %s", url, dest)
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(self._SHA256_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
        except urllib.error.URLError as e:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"下载失败: {url} — {e}") from e
        except Exception as e:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"下载超时或网络错误: {url} — {e}") from e

        if not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(f"下载文件为空或不存在: {url}")
        return dest

    def _verify_sha256(self, file_path: Path, expected: str):
        """校验文件 SHA256。缺少 SHA256 时拒绝安装（防御纵深）。"""
        if not expected:
            file_path.unlink(missing_ok=True)
            raise RuntimeError("拒绝安装未经完整性校验的更新：缺少 SHA256")
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(self._SHA256_CHUNK), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != expected:
            file_path.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 校验失败: 期望={expected[:16]}... 实际={actual[:16]}...")

    def _backup_current(self):
        """备份当前项目关键文件。"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = PROJECT_DIR / f"backup_{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        patterns = ["*.py", "*.json", "*.txt", "*.md"]
        for pat in patterns:
            for f in PROJECT_DIR.glob(pat):
                shutil.copy2(f, backup_dir / f.name)
        logger.info("已备份到: %s", backup_dir)

    def _backup_file(self, file_path: Path):
        """备份单个文件。"""
        if not file_path.exists():
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = PROJECT_DIR / f"backup_{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, backup_dir / file_path.name)

    def _extract_archive(self, archive_path: Path, dest: Path):
        """解压 tar.gz 归档，带路径遍历防护。"""
        if not tarfile.is_tarfile(archive_path):
            raise RuntimeError(f"不是有效的 tar.gz 文件: {archive_path}")
        dest = dest.resolve()
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                member_path = (dest / member.name).resolve()
                if not str(member_path).startswith(str(dest) + os.sep) and member_path != dest:
                    raise RuntimeError(f"检测到路径遍历攻击: {member.name}")
                tar.extract(member, dest)
            logger.info("已解压: %s → %s", archive_path, dest)

    # ── 打印任务状态回调 ──────────────────────────────────────

    def _on_print_status_change(self, task: PrintTask):
        logger.info("任务状态: %s → %s (进度: %.1f%%)",
                    task.task_id, task.status.value, task.progress_pct)

    def _on_print_complete(self, task: PrintTask):
        logger.info("*** 打印完成: %s ***", task.task_id)
        self.cloud.send_status("completed", task_id=task.task_id,
                               message=f"任务 {task.task_id} 已完成")
        self.heartbeat.set_status(DeviceStatus.IDLE)
        self.heartbeat.clear_task()

    def _on_print_cancelled(self, task: PrintTask):
        logger.info("*** 打印取消: %s ***", task.task_id)
        self.cloud.send_status("cancelled", task_id=task.task_id,
                               message=f"任务 {task.task_id} 已取消")
        self.heartbeat.set_status(DeviceStatus.IDLE)
        self.heartbeat.clear_task()

    def _on_print_timeout(self, task: PrintTask):
        logger.warning("*** 打印超时: %s (已追加额外时间) ***", task.task_id)
        self.cloud.send_status("timeout_warning", task_id=task.task_id,
                               message=f"任务 {task.task_id} 超时，追加等待中")

    # ── 录制回放 ──────────────────────────────────────────────

    def _replay_recording(self, name: str, speed: float = 1.0,
                          delay: float = None) -> bool:
        """回放指定名称的录制。"""
        if delay is None:
            delay = DEFAULT_START_DELAY

        json_path = RECORDINGS_DIR / f"{name}.json"
        if not json_path.exists():
            logger.error("录制不存在: %s (%s)", name, json_path)
            return False

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error("读取录制失败: %s — %s", name, e)
            return False

        logger.info("▶ 回放录制: %s (%.1fx speed, %.1fs delay, %.1fs duration)",
                    name, speed, delay, data.get("duration_ms", 0) / 1000)

        try:
            play_back(data, speed, delay)
            return True
        except Exception as e:
            logger.error("回放失败: %s — %s", name, e)
            return False

    def _send_mcu_complete(self) -> bool:
        """发送打印完成指令到 MCU。"""
        if not self._cfg.mcu.enabled:
            logger.info("MCU 未启用，跳过发送完成指令")
            return False
        return self.mcu.send_complete()


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

def main():
    """主入口 — 启动生命周期编排。"""
    # ── CLI 解析 ───────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="3D 打印机自动化控制器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 main.py                    # 正常运行
  python3 main.py --init             # 执行 Setup 钩子后运行
  python3 main.py --init-only        # 仅执行 Setup 钩子
  python3 main.py --maintenance      # 执行维护钩子后运行
  python3 main.py --skip-onboarding  # 跳过首次引导
  python3 main.py --setup            # --init 的别名
        """,
    )
    parser.add_argument("--init", action="store_true",
                        help="执行 Setup 钩子后进入正常运行")
    parser.add_argument("--init-only", action="store_true",
                        help="仅执行 Setup 钩子，然后退出")
    parser.add_argument("--maintenance", action="store_true",
                        help="执行维护类 Setup 钩子后进入正常运行")
    parser.add_argument("--setup", action="store_true",
                        help="等价于 --init")
    parser.add_argument("--skip-onboarding", action="store_true",
                        help="跳过首次引导流程")
    parser.add_argument("--debug", action="store_true",
                        help="启用调试日志")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # 合并 --setup → --init
    if args.setup:
        args.init = True
    init_only = args.init_only
    run_init = args.init or init_only

    # ═══════════════════════════════════════════════════════════
    # 阶段 0: 加载设备状态
    # ═══════════════════════════════════════════════════════════
    logger.info("═ 阶段 0: 加载设备状态 ═")
    device = DeviceState.load()
    is_first_run = device.is_first_run

    if is_first_run:
        logger.info("检测到首次运行，设备 ID: %s", device.device_id)
    else:
        logger.info("设备: %s (第 %d 次启动)", device.device_name or device.device_id,
                    device.num_startups + 1)
        if not device.was_graceful_shutdown:
            logger.warning("上次会话异常退出，请检查日志")

    # ═══════════════════════════════════════════════════════════
    # 阶段 1: 引导流程（首次运行或未完成引导）
    # ═══════════════════════════════════════════════════════════
    if device.needs_onboarding and not args.skip_onboarding:
        logger.info("═ 阶段 1: 引导流程 ═")
        ok = _run_onboarding(device)
        if not ok:
            logger.error("引导流程未完成，退出")
            return 1
    elif device.needs_onboarding and args.skip_onboarding:
        logger.warning("跳过引导流程 (--skip-onboarding)")
    else:
        logger.info("═ 阶段 1: 引导已完成，跳过 ═")

    # ═══════════════════════════════════════════════════════════
    # 阶段 2: 加载配置（4 层优先级）
    # ═══════════════════════════════════════════════════════════
    logger.info("═ 阶段 2: 加载配置 ═")
    config = AppConfig.load()

    # 设备 ID 同步：device_state → config
    if device.device_id and not config.device_id:
        config.device_id = device.device_id
        logger.info("已同步设备 ID 到配置: %s", device.device_id)

    err = config.validate()
    if err:
        logger.error("配置无效: %s", err)
        return 1

    logger.info("配置加载完成 (cloud=%s, mcu=%s)",
                "enabled" if config.cloud.enabled else "disabled",
                "enabled" if config.mcu.enabled else "disabled")

    # ═══════════════════════════════════════════════════════════
    # 阶段 3: Setup 钩子
    # ═══════════════════════════════════════════════════════════
    if run_init or args.maintenance:
        trigger = "maintenance" if args.maintenance else "init"
        logger.info("═ 阶段 3: Setup 钩子 (trigger=%s) ═", trigger)
        hooks = load_hooks(trigger, is_first_init=is_first_run)

        if hooks:
            logger.info("已加载 %d 个钩子", len(hooks))
            results = run_hooks(hooks, stop_on_error=False)
            print_results(results)

            fail_count = sum(1 for r in results if not r.success)
            if fail_count > 0:
                logger.warning("%d 个钩子执行失败", fail_count)
        else:
            logger.info("没有匹配的 Setup 钩子")

        if init_only:
            logger.info("--init-only 模式，Setup 完成，退出")
            return 0
    else:
        logger.info("═ 阶段 3: Setup 钩子未触发 (使用 --init 启用) ═")

    # ═══════════════════════════════════════════════════════════
    # 阶段 4: 启动控制器
    # ═══════════════════════════════════════════════════════════
    logger.info("═ 阶段 4: 启动控制器 ═")
    controller = PrinterController(config, device)

    def _shutdown(signum=None, frame=None):
        logger.info("收到信号 %s, 正在安全退出...", signum)
        controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        ok = controller.start()
        if not ok:
            return 1
        # 保持主线程运行
        while controller._running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
