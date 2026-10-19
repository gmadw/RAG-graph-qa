#!/usr/bin/env python3
"""启动脚本：python run.py [--host 127.0.0.1] [--port 8000]"""
import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="监听地址：默认 127.0.0.1（仅本机可访问）；需局域网/远程访问时用 --host 0.0.0.0",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"[*] 启动后请在浏览器打开: http://localhost:{args.port}")
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")
