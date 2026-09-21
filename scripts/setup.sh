#!/usr/bin/env bash
# 单条命令完成依赖安装 + 数据库初始化：./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
echo ">> installing dependencies ..."
"$PYTHON" -m pip install -r requirements.txt

echo ">> initializing database ..."
"$PYTHON" -m app.database migrate

echo ">> done. start the API with:"
echo "   uvicorn app.main:app --host 0.0.0.0 --port 8000"
