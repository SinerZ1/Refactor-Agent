# -*- coding: utf-8 -*-
"""Refactor-Agent 桌面入口。

【教学与理论关联 - 单进程静态网关与动态端口租约】
桌面应用不应依赖 Vite 开发服务器。这里由 Uvicorn 直接托管 Vue ``dist``，
并通过 ``Config.bind_socket`` 先取得操作系统分配的空闲端口，再把同一个已监听
socket 交给后台线程。这相当于持有一份端口租约，消除了“先探测、后绑定”之间的
TOCTOU 竞争，也让被系统保留的 5173/8000 不再阻断 GUI。
"""

import socket
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
import webview

from app import app

BACKEND_HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 30.0
FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "dist" / "index.html"


def build_backend_server() -> tuple[uvicorn.Server, socket.socket, int]:
    """创建 Uvicorn 与预绑定 socket，返回真实动态端口。"""

    config = uvicorn.Config(
        app,
        host=BACKEND_HOST,
        port=0,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_socket = config.bind_socket()
    port = int(server_socket.getsockname()[1])
    return server, server_socket, port


def run_backend(server: uvicorn.Server, server_socket: socket.socket) -> None:
    try:
        server.run(sockets=[server_socket])
    except Exception as exc:
        print(f"[GUI-Backend] 启动失败: {exc}")


def wait_until_ready(base_url: str, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    """以健康端点作为就绪信号，替代与机器性能相关的固定 sleep。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{base_url}/api/health", timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            time.sleep(0.1)
    return False


def main() -> None:
    if not FRONTEND_INDEX.is_file():
        raise RuntimeError(
            "未找到 frontend/dist/index.html，请先在 frontend 目录执行 npm run build"
        )

    server, server_socket, port = build_backend_server()
    backend_thread = threading.Thread(
        target=run_backend,
        args=(server, server_socket),
        daemon=True,
        name="refactor-agent-uvicorn",
    )
    backend_thread.start()
    base_url = f"http://{BACKEND_HOST}:{port}"

    try:
        if not wait_until_ready(base_url):
            raise RuntimeError("FastAPI 服务未在规定时间内就绪")

        webview.create_window(
            title="Refactor Agent - 多智能体协同重构工作台",
            url=base_url,
            width=1920,
            height=1080,
            resizable=True,
            min_size=(1280, 720),
        )
        webview.start(debug=False)
    finally:
        server.should_exit = True
        backend_thread.join(timeout=5.0)
        if backend_thread.is_alive():
            print("[GUI-Cleanup] Uvicorn 未在 5 秒内退出，将随守护线程结束。")
