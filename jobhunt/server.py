"""로컬 대시보드 서버 — 127.0.0.1 전용. 요청마다 DB 최신 상태로 그려서 응답.

매일 배치(run daily)가 DB 를 갱신하면 새로고침만으로 반영된다.
열어둔 페이지는 /api/version 을 주기적으로 비교해 새 데이터 안내를 띄운다.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import dashboard

DEFAULT_PORT = 8787


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._send(200, dashboard.render(), "text/html; charset=utf-8")
            elif path == "/api/version":
                self._send(200, dashboard.version(), "text/plain; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as e:  # 배치가 DB 를 쓰는 중이라 잠깐 실패해도 서버는 살아 있게
            self._send(500, f"error: {e}", "text/plain; charset=utf-8")

    def _send(self, code: int, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def serve(port: int = DEFAULT_PORT) -> None:
    # 개인 데이터라 외부 인터페이스에 열지 않는다
    ThreadingHTTPServer(("127.0.0.1", port), _Handler).serve_forever()
