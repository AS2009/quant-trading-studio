#!/usr/bin/env bash
# QuantTrading Studio 一键启动脚本（Linux / macOS）
# 用法: ./start.sh                （默认读取真实行情：新浪实时 + 东方财富K线）
#      PORT=9000 ./start.sh
#      QUANTSTUDIO_OFFLINE=1 ./start.sh          # 离线模式（只用缓存/CSV/示例数据）
#      QUANTSTUDIO_DATA_SOURCE=csv ./start.sh    # 使用本地 CSV 数据
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
# 导出给 backend/app.py 使用
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"

# 0. 校验 Python 解释器是否存在（否则会把「没有 python3」误报成「缺少 python3-venv」）
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[错误] 未找到 Python 命令: $PYTHON"
  echo "       请先安装 Python 3.9+ 后重试，或用 PYTHON=/path/to/python3 ./start.sh 指定解释器。"
  exit 1
fi

# 1. 准备运行环境（虚拟环境优先，失败则降级系统 Python）
if [ ! -d ".venv" ]; then
  echo "[1/3] 创建 Python 虚拟环境 .venv ..."
  if ! "$PYTHON" -m venv .venv 2>/dev/null; then
    echo "     虚拟环境创建失败（系统可能缺少 python3-venv），改用系统 Python 运行。"
    rm -rf .venv
  fi
fi
if [ -x ".venv/bin/python" ]; then
  RUN_PY=".venv/bin/python"
  echo "     使用虚拟环境 .venv"
else
  RUN_PY="$PYTHON"
  echo "     使用系统 Python: $("$RUN_PY" --version 2>&1)"
fi

# 2. 依赖（仅 Flask）
echo "[2/3] 检查依赖 ..."
if ! "$RUN_PY" -c "import flask" >/dev/null 2>&1; then
  echo "     正在安装依赖（首次运行需联网，请稍候）..."
  "$RUN_PY" -m pip install -r requirements.txt --quiet --disable-pip-version-check || true
fi
if ! "$RUN_PY" -c "import flask" >/dev/null 2>&1; then
  echo "[错误] 依赖安装失败：当前环境无法导入 flask。"
  echo "       可手动安装后重试："
  echo "         $RUN_PY -m pip install -r requirements.txt"
  echo "         $RUN_PY -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
  exit 1
fi

# 3. 启动（前端由 Flask 一并托管）
SOURCE="${QUANTSTUDIO_DATA_SOURCE:-auto}"
if [ "${QUANTSTUDIO_OFFLINE:-0}" = "1" ]; then
  MODE="离线模式（仅用缓存/CSV/示例数据）"
else
  MODE="在线模式（数据源: ${SOURCE}）"
fi
if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
  echo "[3/3] 启动服务 · ${MODE} · 本机访问: http://127.0.0.1:${PORT}（已监听 ${HOST}，局域网设备可用本机 IP 访问）"
else
  echo "[3/3] 启动服务 · ${MODE} · 请用浏览器访问: http://${HOST}:${PORT}"
fi
echo "      环境自检: $RUN_PY scripts/check_env.py    按 Ctrl+C 停止服务"
cd backend
exec "$RUN_PY" app.py
