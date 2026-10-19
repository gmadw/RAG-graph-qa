#!/usr/bin/env bash
# RAG Graph QA 一键启动脚本
# 用法：
#   ./start.sh              # 主服务（默认 8000 端口）
#   ./start.sh --mock       # 同时启动 Mock 模型服务（无 API Key 体验模式）
#   ./start.sh --mock-only  # 只启动 Mock 模型服务
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
MOCK_PORT="${MOCK_PORT:-9999}"

# 端口占用检查：返回 0 表示已被占用
port_busy() {
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"
  else
    netstat -an 2>/dev/null | grep -qE "[:.]${p}[[:space:]]+.*LISTEN"
  fi
}

require_port() {
  local p="$1" name="$2" hint="$3"
  if port_busy "$p"; then
    echo "[x] ${name}端口 ${p} 已被占用，可能是上一次的实例还没退出。"
    echo "    1) 先用浏览器打开 http://127.0.0.1:${p} ，也许它本来就在正常服务"
    echo "    2) 或者结束旧进程：lsof -nP -iTCP:${p} -sTCP:LISTEN   （Windows 用 netstat -ano | findstr :${p}）"
    echo "    3) 或者换端口启动：${hint}"
    exit 1
  fi
}

# 依赖检查
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "[*] 首次运行：安装依赖中..."
  python3 -m pip install -r backend/requirements.txt
fi

start_backend() {
  echo "[*] 启动主服务: http://127.0.0.1:${PORT}"
  python3 backend/run.py --port "$PORT"
}

start_mock_bg() {
  export RAG_MOCK=1
  echo "[*] 启动 Mock 模型服务: http://127.0.0.1:${MOCK_PORT}/v1"
  python3 mock_server.py --port "$MOCK_PORT" &
  MOCK_PID=$!
  sleep 1
  echo "    （无 Key 体验：主服务已自动将 LLM/Embedding/Rerank 默认指向 Mock，"
  echo "      打开网页即可直接用，无需手动填写 Base URL）"
}

case "${1:-}" in
  --mock)
    require_port "$MOCK_PORT" "Mock 模型服务" "MOCK_PORT=9998 ./start.sh --mock"
    require_port "$PORT" "主服务" "PORT=8010 ./start.sh"
    start_mock_bg
    start_backend
    ;;
  --mock-only)
    require_port "$MOCK_PORT" "Mock 模型服务" "MOCK_PORT=9998 ./start.sh --mock-only"
    python3 mock_server.py --port "$MOCK_PORT"
    ;;
  *)
    require_port "$PORT" "主服务" "PORT=8010 ./start.sh"
    start_backend
    ;;
esac
