"""HTTP server for the collector's report.

GET /         the report plus ``_meta`` and ``summary`` (200, or 503 if stale)
GET /status   alias of /
GET /healthz  liveness: 200 while the report is fresh, else 503
GET /metrics  Prometheus text exposition of the summary scalars
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import threading
import time
from typing import Any

from .config import Config
from .summary import compute_summary

logger = logging.getLogger("zfs-monitor")


class Report:
    """Parsed-report cache keyed on (mtime, size); the file only changes on the timer."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._data: dict | None = None

    def load(self) -> tuple[dict, float, bool]:
        """Return (document, age, stale). Raises FileNotFoundError/JSONDecodeError."""
        path = self.cfg.status_path
        st = os.stat(path)
        age = time.time() - st.st_mtime
        with self._lock:
            if self._key != (st.st_mtime, st.st_size) or self._data is None:
                with open(path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                self._key = (st.st_mtime, st.st_size)
            data = self._data
        stale = age > self.cfg.thresholds.max_stale_seconds
        doc = dict(data)  # never mutate the cached document
        doc["_meta"] = {"file_age_seconds": round(age, 1), "stale": stale}
        doc["summary"] = compute_summary(doc, age, stale, self.cfg.thresholds)
        return doc, age, stale


def metrics_text(summary: dict[str, Any]) -> str:
    lines = []
    for key, val in summary.items():
        if isinstance(val, bool):
            val = int(val)
        if isinstance(val, (int, float)):
            name = f"zfs_monitor_{key}"
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {val}")
    return "\n".join(lines) + "\n"


def make_handler(report: Report):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, error: str, detail: str | None = None) -> None:
            body = {"error": error}
            if detail:
                body["detail"] = detail
            self._send(code, json.dumps(body).encode())

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/status", "/healthz", "/metrics"):
                return self._err(404, "not found")
            try:
                doc, _, stale = report.load()
            except FileNotFoundError:
                logger.warning("Status file not found: %s", report.cfg.status_path)
                return self._err(503, "ZFS status file not found",
                                 "The collector may not be running.")
            except json.JSONDecodeError as e:
                logger.error("Invalid JSON in status file: %s", e)
                return self._err(500, "Invalid JSON in status file", str(e))
            code = 503 if stale else 200
            if path == "/healthz":
                return self._send(code, json.dumps({"ok": not stale}).encode())
            if path == "/metrics":
                return self._send(200, metrics_text(doc["summary"]).encode(),
                                  "text/plain; version=0.0.4")
            self._send(code, json.dumps(doc, indent=2).encode())

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s %s", self.client_address[0], fmt % args)

    return Handler


def serve(cfg: Config) -> None:
    server = http.server.ThreadingHTTPServer(
        (cfg.server.listen_address, cfg.server.port), make_handler(Report(cfg)))
    logger.info("listening on %s:%s, reading %s", cfg.server.listen_address,
                cfg.server.port, cfg.status_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
