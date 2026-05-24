#!/bin/bash
# ==========================================================================
# 云端命令服务 — 安装脚本 (Ubuntu 20.04)
# ==========================================================================
# 用法:
#   chmod +x install_service.sh
#   ./install_service.sh          # 安装到当前用户
#   ./install_service.sh --user pi  # 安装到指定用户
# ==========================================================================

set -euo pipefail

TARGET_USER="${1:-${USER}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "  云端命令服务 - 安装向导"
echo "========================================"
echo ""
echo "  目标用户:   ${TARGET_USER}"
echo "  项目目录:   ${SCRIPT_DIR}"
echo ""

# ── 1. 检查 Python 环境 ──────────────────────────────────────
echo "[1/4] 检查 Python 环境..."

PYTHON_BIN=""
if [ -f "${SCRIPT_DIR}/venv/bin/python" ]; then
    PYTHON_BIN="${SCRIPT_DIR}/venv/bin/python"
    echo "  发现虚拟环境: ${PYTHON_BIN}"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
    echo "  使用系统 Python: ${PYTHON_BIN}"
else
    echo "  错误: 未找到 Python3，请先安装"
    exit 1
fi

# ── 2. 生成 systemd unit 文件 ────────────────────────────────
echo "[2/4] 生成 systemd unit..."

UNIT_DST="/etc/systemd/system/mouse-cloud.service"

sudo tee "${UNIT_DST}" > /dev/null << UNITEOF
[Unit]
Description=Mouse Cloud Service — 云端命令轮询与文件分发
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${TARGET_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON_BIN} ${SCRIPT_DIR}/cloud_service.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mouse-cloud
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNITEOF

echo "  已写入: ${UNIT_DST}"

# ── 3. 启用并启动服务 ────────────────────────────────────────
echo "[3/4] 启用并启动服务..."

sudo systemctl daemon-reload
sudo systemctl enable mouse-cloud.service
sudo systemctl restart mouse-cloud.service

sleep 2
SERVICE_STATUS=$(systemctl is-active mouse-cloud.service || echo "inactive")
echo "  服务状态: ${SERVICE_STATUS}"

# ── 4. 验证 ──────────────────────────────────────────────────
echo "[4/4] 检查运行日志..."
sudo journalctl -u mouse-cloud.service --no-pager -n 10 2>/dev/null || echo "  (无法读取日志)"

echo ""
echo "========================================"
echo "  安装完成!"
echo "========================================"
echo ""
echo "  常用命令:"
echo "    sudo systemctl status mouse-cloud   # 查看状态"
echo "    sudo systemctl restart mouse-cloud  # 重启服务"
echo "    sudo systemctl stop mouse-cloud     # 停止服务"
echo "    sudo journalctl -u mouse-cloud -f   # 实时日志"
echo ""
echo "  配置文件: ${SCRIPT_DIR}/cloud_config.json"
echo ""
