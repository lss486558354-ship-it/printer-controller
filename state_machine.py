#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PLC 风格状态机 — 像素条件驱动状态转移 + 录制回放动作。"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from pixel_monitor import (
    PixelMonitor, PixelRegion, _capture_region_rgb,
    _hex_to_rgb, _scan_rgb_for_color, _rgb_to_color_name, _average_rgb,
)

logger = logging.getLogger("state_machine")

SM_CONFIG_PATH = Path(__file__).parent / "state_machine.json"
JUMP_RULES_DIR = Path(__file__).parent / "jump_rules"


# ── 数据结构 ────────────────────────────────────────────────────────

@dataclass
class SMCloudTrigger:
    """云端消息触发状态转移。当前状态收到匹配消息时自动跳转到目标状态。"""
    message_type: str = ""      # 匹配的消息类型: print_task_a, print_task_b, cancel, restart
    target_state: str = ""      # 匹配后跳转的目标状态名
    enabled: bool = True


@dataclass
class SMCloudAction:
    """进入/退出状态时执行的云端操作。"""
    action_type: str = "send_status"  # send_status
    status: str = "idle"              # 发送的状态值
    message: str = ""                 # 附带消息文本
    enabled: bool = True


@dataclass
class SMCondition:
    """转移条件：检查指定区域是否出现目标颜色（AND 逻辑，区域内任一像素匹配即满足）。"""
    region_label: str = ""
    color: str = "#00FF00"
    tolerance: int = 20
    enabled: bool = True
    mode: str = "match"  # "match"=颜色一致即达成, "changed"=颜色变化后一致才达成（边沿触发）


@dataclass
class SMTransition:
    """一条转移规则：多个条件（AND）→ 满足后跳转到目标状态。"""
    conditions: list[SMCondition] = field(default_factory=list)
    target: str = ""   # 目标状态名, "STOP"=停止
    enabled: bool = True


@dataclass
class SMAction:
    """状态动作：进入/退出状态时回放的录制。"""
    recording: str = ""
    delay_before: float = 0.0
    delay_after: float = 0.0
    enabled: bool = True


@dataclass
class JumpRule:
    """独立跳转规则（规则池中的一条），不绑定具体状态。"""
    name: str = ""              # 规则名称（唯一标识）
    conditions: list[SMCondition] = field(default_factory=list)
    target_sm: str = ""         # 目标状态机名称
    target_state: str = ""      # 目标状态名
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target_sm": self.target_sm,
            "target_state": self.target_state,
            "enabled": self.enabled,
            "conditions": [
                {
                    "region_label": c.region_label,
                    "color": c.color,
                    "tolerance": c.tolerance,
                    "enabled": c.enabled,
                    "mode": c.mode,
                }
                for c in self.conditions
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "JumpRule":
        return cls(
            name=d.get("name", ""),
            target_sm=d.get("target_sm", ""),
            target_state=d.get("target_state", ""),
            enabled=d.get("enabled", True),
            conditions=[
                SMCondition(
                    region_label=c.get("region_label", ""),
                    color=c.get("color", "#00FF00"),
                    tolerance=c.get("tolerance", 20),
                    enabled=c.get("enabled", True),
                    mode=c.get("mode", "match"),
                )
                for c in d.get("conditions", [])
            ],
        )


# ── 规则池 CRUD ──────────────────────────────────────────────────

def load_all_jump_rules() -> dict[str, JumpRule]:
    """加载所有跳转规则（返回 {name: JumpRule} 字典）。"""
    JUMP_RULES_DIR.mkdir(exist_ok=True)
    rules: dict[str, JumpRule] = {}
    for path in sorted(JUMP_RULES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rule = JumpRule.from_dict(data)
            if rule.name:
                rules[rule.name] = rule
        except Exception:
            logger.warning("加载跳转规则失败: %s", path)
    return rules


def save_jump_rule(rule: JumpRule):
    """保存一条跳转规则到文件。"""
    JUMP_RULES_DIR.mkdir(exist_ok=True)
    path = JUMP_RULES_DIR / f"{rule.name}.json"
    path.write_text(
        json.dumps(rule.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8")


def delete_jump_rule(name: str):
    """删除一条跳转规则。"""
    path = JUMP_RULES_DIR / f"{name}.json"
    if path.exists():
        path.unlink()


@dataclass
class SMState:
    """状态机的一个状态。转移规则（transitions）按顺序检查，首个全满足的规则触发跳转。
    jump_rules 引用规则池中的规则名，运行时按名称查表评估。
    cloud_triggers 定义云端消息驱动的状态转移。"""
    name: str = ""
    conditions: list[SMCondition] = field(default_factory=list)   # 兼容旧版：单转移条件
    transitions: list[SMTransition] = field(default_factory=list) # 新版：多转移规则
    jump_rules: list[str] = field(default_factory=list)           # 规则池引用（名称多选）
    cloud_triggers: list[SMCloudTrigger] = field(default_factory=list)  # 云端消息触发
    cloud_actions_on_enter: list[SMCloudAction] = field(default_factory=list)  # 进入时云端操作
    cloud_actions_on_exit: list[SMCloudAction] = field(default_factory=list)   # 退出时云端操作
    on_enter: list[SMAction] = field(default_factory=list)
    on_exit: list[SMAction] = field(default_factory=list)
    timeout_s: float = 0.0
    timeout_jump: str = ""
    next_state: str = ""   # 单转移目标（空=顺序下一个, "STOP"=停止）


@dataclass
class SMConfig:
    """完整状态机配置。"""
    name: str = "未命名状态机"
    states: list[SMState] = field(default_factory=list)
    initial_state: str = ""
    interval_ms: int = 500
    loop: bool = False
    enabled: bool = True

    def get_state(self, name: str) -> SMState | None:
        for s in self.states:
            if s.name == name:
                return s
        return None

    def get_state_index(self, name: str) -> int:
        for i, s in enumerate(self.states):
            if s.name == name:
                return i
        return -1

    def next_state(self, current: str) -> str:
        """返回当前状态的目标状态。优先使用 next_state 字段。"""
        state = self.get_state(current)
        if not state:
            return ""
        # 用户指定了目标状态
        if state.next_state:
            if state.next_state == "STOP":
                return ""  # 终止
            if self.get_state(state.next_state):
                return state.next_state
        # 默认顺序下一个
        idx = self.get_state_index(current)
        if idx < 0:
            return ""
        next_idx = idx + 1
        if next_idx >= len(self.states):
            return self.states[0].name if self.loop else ""
        return self.states[next_idx].name


# ── 引擎 ────────────────────────────────────────────────────────────

class StateMachineEvent:
    """状态机事件。"""
    def __init__(self, event_type: str, state: SMState, message: str = ""):
        self.type = event_type          # "enter", "exit", "transition", "timeout", "idle"
        self.state = state
        self.message = message
        self.timestamp = time.time()


class StateMachineEngine:
    """状态机执行引擎：轮询像素条件 → 触发状态转移 → 执行动作。"""

    def __init__(self, config: SMConfig):
        self.config = config
        self._current_state: str = ""
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._on_event: Optional[Callable[[StateMachineEvent], None]] = None
        self._replay_callback: Optional[Callable[[str], None]] = None
        self._pixel_monitor: Optional[PixelMonitor] = None
        self._state_enter_time: float = 0.0
        self._step_mode: bool = False
        self._step_trigger: bool = False
        self._cond_prev: dict[str, bool] = {}  # 条件上一次检测结果（用于边沿触发）
        self._jump_rules: dict[str, JumpRule] = {}  # 规则池引用
        self._cloud_sender: Optional[Callable[[str, str], bool]] = None  # 云端发送回调 (status, msg) -> bool
        self._cloud_guard: Optional[Callable[[str, dict], bool]] = None   # 云端转移守卫 (msg_type, data) -> bool
        self._cloud_msg_queue: list[tuple[str, dict]] = []  # 待处理的云端消息
        self._wake_event = threading.Event()                 # 唤醒主循环

    @property
    def current_state(self) -> str:
        return self._current_state

    @property
    def running(self) -> bool:
        return self._running

    def set_event_callback(self, cb: Callable[[StateMachineEvent], None]):
        self._on_event = cb

    def set_replay_callback(self, cb: Callable[[str], None]):
        self._replay_callback = cb

    def set_pixel_monitor(self, mon: PixelMonitor):
        self._pixel_monitor = mon

    def set_jump_rules(self, rules: dict[str, JumpRule]):
        """设置规则池引用。"""
        self._jump_rules = rules

    def set_cloud_sender(self, callback: Callable[[str, str], bool]):
        """设置云端消息发送回调。callback(status, message) -> 是否成功。"""
        self._cloud_sender = callback

    def set_cloud_guard(self, callback: Callable[[str, dict], bool]):
        """设置云端转移守卫。callback(msg_type, data) -> True=允许转移, False=阻止。
        用于检查设备忙/idle状态等前置条件。"""
        self._cloud_guard = callback

    def handle_cloud_message(self, msg_type: str, data: dict = None):
        """接收云端消息。在引擎线程中处理，唤醒主循环立即检查。"""
        with self._lock:
            self._cloud_msg_queue.append((msg_type, data or {}))
        self._wake_event.set()  # 唤醒主循环

    def start(self):
        if self._running:
            return
        self._running = True
        self._paused = False
        self._step_mode = False
        initial = self.config.initial_state
        if not initial and self.config.states:
            initial = self.config.states[0].name
        self._current_state = initial
        self._state_enter_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sm-engine")
        self._thread.start()
        if initial:
            self._enter_state(initial)

    def stop(self):
        self._running = False
        self._paused = False
        self._step_mode = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def step_once(self):
        """单步模式：执行一步后暂停。"""
        self._step_mode = True
        self._paused = False
        self._step_trigger = True

    # ── 主循环 ─────────────────────────────────────────────────

    def _run(self):
        while self._running:
            if self._paused and not self._step_trigger:
                time.sleep(0.1)
                continue

            self._step_trigger = False

            # 处理积压的云端消息
            self._drain_cloud_messages()

            current = self._current_state
            if not current:
                self._wake_event.wait(self.config.interval_ms / 1000.0)
                self._wake_event.clear()
                continue

            state = self.config.get_state(current)
            if not state:
                self._wake_event.wait(self.config.interval_ms / 1000.0)
                self._wake_event.clear()
                continue

            # 检查超时
            elapsed = time.time() - self._state_enter_time
            if state.timeout_s > 0 and elapsed >= state.timeout_s:
                self._on_timeout(state)
                if self._step_mode:
                    self._paused = True
                continue

            # 检查转移条件（返回目标状态名或 ""）
            target = self._check_conditions(state)
            if target:
                self._transition(state, target)

            if self._step_mode:
                self._paused = True

            self._wake_event.wait(self.config.interval_ms / 1000.0)
            self._wake_event.clear()

    def _drain_cloud_messages(self):
        """处理队列中的云端消息，检查当前状态的 cloud_triggers。"""
        with self._lock:
            if not self._cloud_msg_queue:
                return
            messages = self._cloud_msg_queue[:]
            self._cloud_msg_queue.clear()

        for msg_type, data in messages:
            self._process_cloud_message(msg_type, data)

    def _process_cloud_message(self, msg_type: str, data: dict):
        """处理单条云端消息：检查当前状态是否有匹配的 cloud_trigger。"""
        current = self._current_state
        if not current:
            return

        state = self.config.get_state(current)
        if not state or not state.cloud_triggers:
            # 状态没有云触发器 → 保存到队列稍后重试（状态可能正在切换）
            return

        for trigger in state.cloud_triggers:
            if not trigger.enabled:
                continue
            if trigger.message_type == msg_type:
                # 守卫检查
                if self._cloud_guard:
                    if not self._cloud_guard(msg_type, data):
                        logger.info("云端守卫阻止转移: %s → %s", msg_type, trigger.target_state)
                        continue

                logger.info("云端触发转移: %s → %s (msg=%s)",
                            current, trigger.target_state, msg_type)
                self._transition(state, trigger.target_state)
                return

    def _check_conditions(self, state: SMState) -> str:
        """检查转移规则，返回首个满足的目标状态名（""=无满足）。"""

        # ── 规则池引用：按名称查找并评估 ──
        if state.jump_rules:
            for rule_name in state.jump_rules:
                rule = self._jump_rules.get(rule_name)
                if not rule or not rule.enabled:
                    continue
                if not rule.conditions:
                    return self._resolve_target(rule.target_state)
                if not self._pixel_monitor:
                    continue
                if self._eval_conditions(state.name, rule.conditions):
                    return self._resolve_target(rule.target_state)
            return ""

        # ── 新版：多转移规则（transitions）──
        if state.transitions:
            for tr in state.transitions:
                if not tr.enabled:
                    continue
                if not tr.conditions:
                    return self._resolve_target(tr.target)  # 空条件 = 无条件转移
                if not self._pixel_monitor:
                    continue
                if self._eval_conditions(state.name, tr.conditions):
                    return self._resolve_target(tr.target)
            return ""

        # ── 旧版：单条件列表 → next_state ──
        if state.conditions:
            enabled_conds = [c for c in state.conditions if c.enabled]
            if not enabled_conds:
                return self._resolve_target(state.next_state)  # 空条件 = 无条件转移
            if not self._pixel_monitor:
                return ""
            if self._eval_conditions(state.name, enabled_conds):
                return self._resolve_target(state.next_state)
            return ""

        # 无任何条件但有 next_state → 无条件顺序转移
        if state.next_state:
            return self._resolve_target(state.next_state)

        return ""

    def _eval_conditions(self, state_name: str, conditions: list[SMCondition]) -> bool:
        """评估一组条件是否全部满足（AND 逻辑）。"""
        for cond in conditions:
            if not cond.enabled:
                continue
            region = self._pixel_monitor.get_region(cond.region_label)
            if not region or not region.enabled:
                return False
            try:
                data = _capture_region_rgb(region.x1, region.y1, region.x2, region.y2)
            except Exception:
                return False
            if not data:
                return False
            tr_c, tg_c, tb_c = _hex_to_rgb(cond.color)
            now_matches = _scan_rgb_for_color(data, tr_c, tg_c, tb_c, cond.tolerance)

            key = f"{state_name}|{cond.region_label}|{cond.color}"
            prev = self._cond_prev.get(key)

            if cond.mode == "changed":
                if prev is None:
                    self._cond_prev[key] = now_matches
                    return False
                self._cond_prev[key] = now_matches
                if not (prev == False and now_matches == True):
                    return False
            else:
                self._cond_prev[key] = now_matches
                if not now_matches:
                    return False
        return True

    def _resolve_target(self, target: str) -> str:
        """解析目标状态名：空→按顺序下一, STOP→空。"""
        if target == "STOP":
            return ""
        if target:
            return target
        # 空 → 使用顺序下一状态
        return self.config.next_state(self._current_state) if self._current_state else ""

    def _transition(self, state: SMState, target: str = ""):
        """执行状态转移。target 为空时使用顺序下一状态。"""
        self._execute_actions(state.on_exit, f"退出 {state.name}")
        self._execute_cloud_actions(state.cloud_actions_on_exit, f"退出 {state.name}")
        self._emit("exit", state)

        next_name = target or self.config.next_state(state.name)
        if not next_name:
            self._emit("idle", state, "已是最后一个状态，无下一状态")
            if self._step_mode:
                self._paused = True
            return

        next_state = self.config.get_state(next_name)
        if not next_state:
            return

        self._emit("transition", state, f"{state.name} → {next_name}")

        self._current_state = next_name
        self._state_enter_time = time.time()
        self._enter_state(next_name)

    def _enter_state(self, state_name: str):
        """进入一个状态：执行进入动作。"""
        state = self.config.get_state(state_name)
        if not state:
            return
        # 初始化本状态所有边沿触发条件的上一轮状态
        all_conds = list(state.conditions)
        for tr in state.transitions:
            all_conds.extend(tr.conditions)
        for rule_name in state.jump_rules:
            rule = self._jump_rules.get(rule_name)
            if rule:
                all_conds.extend(rule.conditions)
        for c in all_conds:
            if c.mode == "changed":
                key = f"{state_name}|{c.region_label}|{c.color}"
                self._cond_prev.setdefault(key)
        self._emit("enter", state)
        self._execute_actions(state.on_enter, f"进入 {state_name}")
        self._execute_cloud_actions(state.cloud_actions_on_enter, f"进入 {state_name}")

    def _on_timeout(self, state: SMState):
        """处理超时。"""
        self._emit("timeout", state, f"{state.name} 超时 ({state.timeout_s}s)")

        if state.timeout_jump:
            target = self.config.get_state(state.timeout_jump)
            if target:
                self._emit("transition", state, f"超时跳转: {state.name} → {state.timeout_jump}")
                self._current_state = state.timeout_jump
                self._state_enter_time = time.time()
                self._enter_state(state.timeout_jump)
                return

        # 默认：跳到下一个状态
        self._transition(state)

    def _execute_actions(self, actions: list[SMAction], context: str):
        """执行一组动作（顺序执行）。"""
        for action in actions:
            if not action.enabled or not action.recording.strip():
                continue
            if action.delay_before > 0:
                time.sleep(action.delay_before)
            logger.info("[%s] 回放: %s", context, action.recording)
            if self._replay_callback:
                self._replay_callback(action.recording)
            if action.delay_after > 0:
                time.sleep(action.delay_after)

    def _execute_cloud_actions(self, actions: list[SMCloudAction], context: str):
        """执行一组云端操作（send_status 等）。"""
        for action in actions:
            if not action.enabled:
                continue
            if action.action_type == "send_status":
                logger.info("[%s] 发送云端状态: %s — %s", context, action.status, action.message)
                if self._cloud_sender:
                    self._cloud_sender(action.status, action.message)

    def _emit(self, event_type: str, state: SMState, message: str = ""):
        if self._on_event:
            self._on_event(StateMachineEvent(event_type, state, message))

    # ── 当前状态快照 ──────────────────────────────────────────

    def get_status_snapshot(self) -> dict:
        """返回当前运行状态快照（供 GUI 刷新）。"""
        current = self._current_state
        state = self.config.get_state(current) if current else None
        cond_status: dict[str, bool] = {}
        if state and self._pixel_monitor:
            # 收集所有需要检查的条件（state.conditions + jump_rules引用的规则条件）
            all_conds = list(state.conditions)
            for rule_name in state.jump_rules:
                rule = self._jump_rules.get(rule_name)
                if rule:
                    all_conds.extend(rule.conditions)
            for c in all_conds:
                if not c.enabled:
                    cond_status[c.region_label] = False
                    continue
                region = self._pixel_monitor.get_region(c.region_label)
                if not region or not region.enabled:
                    cond_status[c.region_label] = False
                    continue
                try:
                    data = _capture_region_rgb(region.x1, region.y1, region.x2, region.y2)
                except Exception:
                    cond_status[c.region_label] = False
                    continue
                if data:
                    tr, tg, tb = _hex_to_rgb(c.color)
                    cond_status[c.region_label] = _scan_rgb_for_color(data, tr, tg, tb, c.tolerance)
                else:
                    cond_status[c.region_label] = False
        return {
            "current_state": current,
            "running": self._running,
            "paused": self._paused,
            "conditions": cond_status,
            "elapsed": time.time() - self._state_enter_time if self._state_enter_time > 0 else 0,
        }

    # ── 配置持久化 ────────────────────────────────────────────

    def save_config(self, path: Path = SM_CONFIG_PATH):
        data = {
            "name": self.config.name,
            "initial_state": self.config.initial_state,
            "interval_ms": self.config.interval_ms,
            "loop": self.config.loop,
            "enabled": self.config.enabled,
            "states": [
                {
                    "name": s.name,
                    "timeout_s": s.timeout_s,
                    "timeout_jump": s.timeout_jump,
                    "next_state": s.next_state,
                    "jump_rules": s.jump_rules,
                    "cloud_triggers": [
                        {
                            "message_type": ct.message_type,
                            "target_state": ct.target_state,
                            "enabled": ct.enabled,
                        }
                        for ct in s.cloud_triggers
                    ],
                    "cloud_actions_on_enter": [
                        {
                            "action_type": ca.action_type,
                            "status": ca.status,
                            "message": ca.message,
                            "enabled": ca.enabled,
                        }
                        for ca in s.cloud_actions_on_enter
                    ],
                    "cloud_actions_on_exit": [
                        {
                            "action_type": ca.action_type,
                            "status": ca.status,
                            "message": ca.message,
                            "enabled": ca.enabled,
                        }
                        for ca in s.cloud_actions_on_exit
                    ],
                    "conditions": [
                        {
                            "region_label": c.region_label,
                            "color": c.color,
                            "tolerance": c.tolerance,
                            "enabled": c.enabled,
                            "mode": c.mode,
                        }
                        for c in s.conditions
                    ],
                    "transitions": [
                        {
                            "target": tr.target,
                            "enabled": tr.enabled,
                            "conditions": [
                                {
                                    "region_label": c.region_label,
                                    "color": c.color,
                                    "tolerance": c.tolerance,
                                    "enabled": c.enabled,
                                    "mode": c.mode,
                                }
                                for c in tr.conditions
                            ],
                        }
                        for tr in s.transitions
                    ],
                    "on_enter": [
                        {
                            "recording": a.recording,
                            "delay_before": a.delay_before,
                            "delay_after": a.delay_after,
                            "enabled": a.enabled,
                        }
                        for a in s.on_enter
                    ],
                    "on_exit": [
                        {
                            "recording": a.recording,
                            "delay_before": a.delay_before,
                            "delay_after": a.delay_after,
                            "enabled": a.enabled,
                        }
                        for a in s.on_exit
                    ],
                }
                for s in self.config.states
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_config(cls, path: Path = SM_CONFIG_PATH) -> StateMachineEngine:
        cfg = SMConfig()
        if not path.exists():
            return cls(cfg)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls(cfg)

        cfg.name = data.get("name", "未命名状态机")
        cfg.initial_state = data.get("initial_state", "")
        cfg.interval_ms = data.get("interval_ms", 500)
        cfg.loop = data.get("loop", False)
        cfg.enabled = data.get("enabled", True)

        for sd in data.get("states", []):
            state = SMState(
                name=sd.get("name", ""),
                timeout_s=sd.get("timeout_s", 0.0),
                timeout_jump=sd.get("timeout_jump", ""),
                next_state=sd.get("next_state", ""),
                jump_rules=sd.get("jump_rules", []),
                cloud_triggers=[
                    SMCloudTrigger(
                        message_type=ct.get("message_type", ""),
                        target_state=ct.get("target_state", ""),
                        enabled=ct.get("enabled", True),
                    )
                    for ct in sd.get("cloud_triggers", [])
                ],
                cloud_actions_on_enter=[
                    SMCloudAction(
                        action_type=ca.get("action_type", "send_status"),
                        status=ca.get("status", "idle"),
                        message=ca.get("message", ""),
                        enabled=ca.get("enabled", True),
                    )
                    for ca in sd.get("cloud_actions_on_enter", [])
                ],
                cloud_actions_on_exit=[
                    SMCloudAction(
                        action_type=ca.get("action_type", "send_status"),
                        status=ca.get("status", "idle"),
                        message=ca.get("message", ""),
                        enabled=ca.get("enabled", True),
                    )
                    for ca in sd.get("cloud_actions_on_exit", [])
                ],
                conditions=[
                    SMCondition(
                        region_label=c.get("region_label", ""),
                        color=c.get("color", "#00FF00"),
                        tolerance=c.get("tolerance", 20),
                        enabled=c.get("enabled", True),
                        mode=c.get("mode", "match"),
                    )
                    for c in sd.get("conditions", [])
                ],
                transitions=[
                    SMTransition(
                        target=td.get("target", ""),
                        enabled=td.get("enabled", True),
                        conditions=[
                            SMCondition(
                                region_label=c.get("region_label", ""),
                                color=c.get("color", "#00FF00"),
                                tolerance=c.get("tolerance", 20),
                                enabled=c.get("enabled", True),
                                mode=c.get("mode", "match"),
                            )
                            for c in td.get("conditions", [])
                        ],
                    )
                    for td in sd.get("transitions", [])
                ],
                on_enter=[
                    SMAction(
                        recording=a.get("recording", ""),
                        delay_before=a.get("delay_before", 0.0),
                        delay_after=a.get("delay_after", 0.0),
                        enabled=a.get("enabled", True),
                    )
                    for a in sd.get("on_enter", [])
                ],
                on_exit=[
                    SMAction(
                        recording=a.get("recording", ""),
                        delay_before=a.get("delay_before", 0.0),
                        delay_after=a.get("delay_after", 0.0),
                        enabled=a.get("enabled", True),
                    )
                    for a in sd.get("on_exit", [])
                ],
            )
            cfg.states.append(state)

        return cls(cfg)
