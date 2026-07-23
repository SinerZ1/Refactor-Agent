# -*- coding: utf-8 -*-
"""
Refactor-Agent GUI Entry point.
============================================================
【教学与理论关联 - 进程隔离与多线程套壳容器】
本模块基于 Pywebview 库实现了一个跨平台桌面应用壳（Desktop Wrapper）。
为了保证系统的平滑流式响应 (SSE) 与全双工长连接 (WebSocket) 在桌面端能与
FastAPI 完美协同，本项目采用 “多线程守护进程模式”：
1. 启动独立后台守护线程运行 Uvicorn 服务，监听 127.0.0.1:8000。
2. 前端经过 Vite 编译成静态 HTML/JS/CSS 并由 FastAPI `/` 路由统一代理与挂载。
3. 主线程初始化 Pywebview 容器，利用系统内置原生 WebView2 (Windows 平台)
   或 WebKit (macOS/Linux) 加载本地环回服务，保持了完全一致的渲染体验与调试友好度。
============================================================
"""

import os
import subprocess
import sys
import threading
import time

import uvicorn
import webview

# 将当前 backend 目录添加到 Python 搜索路径，确保导入正确
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from app import app  # noqa: E402


def run_backend():
    """
    在守护线程中运行 FastAPI/Uvicorn 后端服务器。
    """
    try:
        print("[GUI-Backend] 正在后台线程中启动 FastAPI 服务...")
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
    except Exception as e:
        print(f"[GUI-Backend] 启动失败: {e}", file=sys.stderr)


def kill_process_tree(process):
    """
    【教学与理论关联 - Windows 进程树与优雅级联终止】
    在 Windows 下使用 shell=True 启动 npm 脚本时，Python 的 `Popen` 实质上是
    在 cmd.exe 包装器下执行命令。常规的 `process.terminate()` 只能杀死 `cmd.exe`
    父进程，而由其衍生出的 `node.exe` (Vite) 进程会成为孤儿进程（Zombie Process），
    进而永久独占 5173 端口。
    此处使用 Windows 的 `taskkill /F /T` 指令，通过 PID 级联强行清理整个进程树，
    保证 5173 端口在应用退出时能够被完美、及时地释放，展现防御性编程的卓越实践。
    """
    if not process:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process.terminate()
            process.wait(timeout=3)
    except Exception as e:
        print(f"[GUI-Cleanup] 清理进程树失败: {e}", file=sys.stderr)


def run_frontend():
    """
    【教学与理论关联 - 启动静默开发服务器与防输出污染】
    在桌面端套壳启动时，为了给用户最纯净的体验，通过启动 `npm run dev` 调起 Vite，
    并将 stdout 和 stderr 全部定向至 `DEVNULL`。
    这样做既能保证 5173 端口正常承载前端单页应用（SPA），又彻底规避了 Vite 及其 DevTools
    在控制台中打印的随机调试端口与服务 URL（如 `http://localhost:5173/__devtools__/` 等），
    完美契合“不用显示Devtool端口”的要求。
    """
    try:
        print("[GUI-Frontend] 正在启动前端 Vite 开发服务器 (npm run dev)...")
        # 定位到 frontend 目录
        frontend_dir = os.path.abspath(os.path.join(current_dir, "..", "frontend"))

        # 在 Windows 环境下，npm 属于 cmd.exe 中的批处理脚本，必须开启 shell=True
        process = subprocess.Popen(
            "npm run dev",
            cwd=frontend_dir,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return process
    except Exception as e:
        print(f"[GUI-Frontend] 启动前端开发服务器失败: {e}", file=sys.stderr)
        return None


def main():
    # 1. 启动前端 Vite 调试服务器 (npm run dev)
    frontend_process = run_frontend()

    # 2. 启动后台 FastAPI 服务线程
    backend_thread = threading.Thread(target=run_backend, daemon=True)
    backend_thread.start()

    # 3. 等待前后端服务初始化，确保环回网络就绪
    # Vite 启动和端口监听需要微小延迟，等待 2 秒确保可用
    time.sleep(2.0)

    print("[GUI-Window] 正在初始化 Pywebview 窗口并加载应用主页...")

    # 4. 创建 Pywebview 窗口
    # 尺寸设定为宽1920像素、高1080像素，支持自适应调整，且最小分辨率限定在 1600x900。
    webview.create_window(
        title="Refactor Agent - 多智能体协同重构工作台",
        url="http://127.0.0.1:5173/",
        width=1920,
        height=1080,
        resizable=True,
        min_size=(1600, 900),
    )

    # 5. 启动 GUI 引擎主事件循环并自动清理
    try:
        # 不用显示 Devtools 端口，故设 debug=False
        webview.start(debug=False)
    finally:
        print("[GUI-Cleanup] 正在清理后台进程...")
        if frontend_process:
            kill_process_tree(frontend_process)


if __name__ == "__main__":
    main()
