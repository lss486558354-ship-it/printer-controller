#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Setup 钩子系统 — 等价于 Claude Code 的 Setup 钩子事件。"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("setup_hooks")

HOOKS_DIR = Path(__file__).parent / "setup_hooks"


# ── 数据结构 ────────────────────────────────────────────────────────

@dataclass
class SetupHook:
    """一条 Setup 钩子定义。"""
    name: str = ""
    description: str = ""
    command: str = ""                    # shell 命令
    enabled: bool = True
    trigger: str = "init"                # "init" | "maintenance" | "both"
    run_once: bool = False               # 仅在首次 init 时运行
    timeout_seconds: int = 60
    order: int = 0                       # 执行顺序（越小越先）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "command": self.command,
            "enabled": self.enabled,
            "trigger": self.trigger,
            "run_once": self.run_once,
            "timeout_seconds": self.timeout_seconds,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SetupHook":
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            command=d.get("command", ""),
            enabled=d.get("enabled", True),
            trigger=d.get("trigger", "init"),
            run_once=d.get("run_once", False),
            timeout_seconds=d.get("timeout_seconds", 60),
            order=d.get("order", 0),
        )


# ── 加载 ────────────────────────────────────────────────────────────

def load_hooks(trigger: str, is_first_init: bool = False) -> list[SetupHook]:
    """加载匹配触发条件的钩子。"""
    if not HOOKS_DIR.exists():
        return []

    hooks: list[SetupHook] = []
    for path in sorted(HOOKS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hook = SetupHook.from_dict(data)
        except Exception:
            logger.warning("加载钩子失败: %s", path)
            continue

        if not hook.enabled or not hook.command.strip():
            continue

        # 过滤触发条件
        if hook.trigger == "maintenance" and trigger != "maintenance":
            continue
        if hook.trigger == "init" and trigger not in ("init", "init_only"):
            continue
        if hook.run_once and not is_first_init:
            continue

        hooks.append(hook)

    hooks.sort(key=lambda h: h.order)
    return hooks


# ── 执行 ────────────────────────────────────────────────────────────

class HookResult:
    """单条钩子的执行结果。"""
    def __init__(self, hook: SetupHook):
        self.hook = hook
        self.success: bool = False
        self.output: str = ""
        self.duration_ms: float = 0.0
        self.error: str = ""


def run_hooks(hooks: list[SetupHook], stop_on_error: bool = False) -> list[HookResult]:
    """顺序执行钩子列表，返回结果列表。"""
    results: list[HookResult] = []

    for i, hook in enumerate(hooks):
        logger.info("[%d/%d] 执行: %s — %s", i + 1, len(hooks), hook.name, hook.description)
        result = HookResult(hook)
        started = time.time()

        try:
            proc = subprocess.run(
                hook.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=hook.timeout_seconds,
                cwd=str(Path(__file__).parent),
            )
            result.output = (proc.stdout + proc.stderr).strip()
            result.duration_ms = (time.time() - started) * 1000

            if proc.returncode == 0:
                result.success = True
                logger.info("  [OK] (%.0fms)", result.duration_ms)
            else:
                result.error = f"退出码 {proc.returncode}"
                logger.warning("  [FAIL] %s", result.error)
                if stop_on_error:
                    results.append(result)
                    break
        except subprocess.TimeoutExpired:
            result.duration_ms = hook.timeout_seconds * 1000
            result.error = f"超时 ({hook.timeout_seconds}s)"
            logger.warning("  [FAIL] %s", result.error)
            if stop_on_error:
                results.append(result)
                break
        except Exception as e:
            result.error = str(e)
            logger.warning("  [FAIL] %s", e)
            if stop_on_error:
                results.append(result)
                break

        results.append(result)

    return results


def print_results(results: list[HookResult]):
    """打印钩子执行摘要。"""
    ok = sum(1 for r in results if r.success)
    fail = len(results) - ok

    print(f"\n{'=' * 50}")
    print(f"Setup 钩子执行完成: {ok} 成功, {fail} 失败")
    print(f"{'=' * 50}")

    for r in results:
        status = "[OK]" if r.success else "[FAIL]"
        print(f"  {status} {r.hook.name} ({r.duration_ms:.0f}ms)")
        if r.output:
            for line in r.output.splitlines()[:5]:
                print(f"    │ {line}")
        if r.error:
            print(f"    │ 错误: {r.error}")
