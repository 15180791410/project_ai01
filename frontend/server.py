"""独立前端静态服务器。

默认使用 8080 端口托管本目录，API 仍访问 8000 的 FastAPI。这样后端重启
或故障时，前端页面本身仍能打开，并给出明确的接口不可用提示。主部署方式
仍保留 FastAPI 同源托管，8080 可作为演示环境的解耦入口。
"""
from __future__ import annotations

import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.getenv("YUEJIAO_FRONTEND_HOST", "127.0.0.1")
PORT = int(os.getenv("YUEJIAO_FRONTEND_PORT", "8080"))
ROOT = Path(__file__).resolve().parent


class FrontendHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # HTML/JS 更新后避免浏览器继续使用旧版本。
        if self.path.endswith((".html", ".js", ".css")) or self.path == "/":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    print(f"Frontend: http://{HOST}:{PORT}/  API: http://127.0.0.1:8000")
    ThreadingHTTPServer((HOST, PORT), FrontendHandler).serve_forever()
