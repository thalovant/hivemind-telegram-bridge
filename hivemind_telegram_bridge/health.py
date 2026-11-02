"""A minimal HTTP health endpoint for container platforms.

``GET /healthz`` answers 200 for as long as the process can answer at all
(liveness). ``GET /readyz`` answers 200 only while the bridge can carry a
message end to end: the HiveMind session is up and Telegram is being
polled (readiness). Anything else is 404. It serves no data and accepts no
input, so it can listen on every interface without exposing anything.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


class HealthServer:
    """Serve ``/healthz`` and ``/readyz`` on ``host:port`` from a daemon thread."""

    def __init__(self, port: int, ready: Callable[[], bool], host: str = "0.0.0.0"):
        def answer(handler: BaseHTTPRequestHandler) -> None:
            if handler.path == "/healthz":
                status, body = 200, b"ok\n"
            elif handler.path == "/readyz":
                ok = False
                try:
                    ok = bool(ready())
                except Exception:  # a failing check is "not ready", never a crash
                    ok = False
                status, body = (200, b"ready\n") if ok else (503, b"not ready\n")
            else:
                status, body = 404, b"not found\n"
            handler.send_response(status)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                answer(self)

            def log_message(self, format, *args):  # probes every few seconds; stay quiet
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        """The port actually bound (useful when 0 asked for any free one)."""
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="bridge-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
