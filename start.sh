#!/usr/bin/env bash
# RAG Graph QA 一键启动脚本
# 用法：
#   ./start.sh                      # 主服务（默认 8000 端口）
#   ./start.sh --port 8010          # 指定主服务端口
#   ./start.sh --mock               # 同时启动 Mock 模型服务
#   ./start.sh --mock --port 8010   # 参数可任意组合
#   ./start.sh --mock-only          # 只启动 Mock 模型服务
set -e
cd "$(dirname "$0")"

MODE=main
ARG_PORT=""
ARG_MOCK_PORT=""

usage() {
  cat <<'EOF'
Usage:
  ./start.sh                      start main service on port 8000
  ./start.sh --port 8010          start main service on a custom port
  ./start.sh -p 8010              same as --port
  ./start.sh --mock               start main service + mock model service
  ./start.sh --mock --port 8010   combine options in any order
  ./start.sh --mock-only          start mock model service only
  ./start.sh --mock-port 9998     custom mock service port
  ./start.sh --help               show this help

Environment variables (CLI arguments take precedence):
  PORT        main service port, default 8000
  MOCK_PORT   mock service port, default 9999
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --mock) MODE=mock ;;
    --mock-only) MODE=mock-only ;;
    --port|-p)
      if [ -z "${2:-}" ]; then echo "[x] --port requires a value, e.g. --port 8010"; exit 1; fi
      ARG_PORT="$2"; shift ;;
    --mock-port)
      if [ -z "${2:-}" ]; then echo "[x] --mock-port requires a value, e.g. --mock-port 9998"; exit 1; fi
      ARG_MOCK_PORT="$2"; shift ;;
    *) echo "[x] Unknown option: $1"; echo; usage; exit 1 ;;
  esac
  shift
done

PORT="${ARG_PORT:-${PORT:-8000}}"
MOCK_PORT="${ARG_MOCK_PORT:-${MOCK_PORT:-9999}}"
export MOCK_PORT  # 传给 mock_server.py 与后端（RAG_MOCK 默认地址会跟随它）

is_port() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
is_port "$PORT" || { echo "[x] Invalid port: $PORT - use a number like 8010"; exit 1; }
is_port "$MOCK_PORT" || { echo "[x] Invalid mock port: $MOCK_PORT - use a number like 9998"; exit 1; }

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

case "$MODE" in
  mock)
    require_port "$MOCK_PORT" "Mock 模型服务" "./start.sh --mock --mock-port 9998"
    require_port "$PORT" "主服务" "./start.sh --port 8010"
    start_mock_bg
    start_backend
    ;;
  mock-only)
    require_port "$MOCK_PORT" "Mock 模型服务" "./start.sh --mock-only --mock-port 9998"
    python3 mock_server.py --port "$MOCK_PORT"
    ;;
  *)
    require_port "$PORT" "主服务" "./start.sh --port 8010"
    start_backend
    ;;
esac
