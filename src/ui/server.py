"""
Agent Execution Visualisation Server
=====================================
Standalone::

    python server.py
    python server.py --log-file /path/to/log.json --port 9090

Programmatic (called by ``loom ui``)::

    from src.ui.server import start_server
    start_server(port=8080, auto_browser=True, log_file=None, agent_root=None)
"""

from __future__ import annotations

import argparse
import json
import time
import threading
import webbrowser
from pathlib import Path
from typing import Optional

try:
    from flask import Flask, jsonify, send_file, send_from_directory, request, Response, stream_with_context
except ImportError:
    print("Missing Flask. Run: pip install flask")
    raise SystemExit(1)

# ══════════════════════════════════════════════
#  ★ Runtime config (set by start_server) ★
# ══════════════════════════════════════════════

LOG_FILE: Path = Path("visualization.json")  # overridden by start_server()
PORT: int = 8080
AUTO_OPEN_BROWSER: bool = True
AGENT_ROOT: Path = Path(__file__).resolve().parents[2]   # default: AgentLoom/
INITIAL_LOG: Optional[str] = None      # runtime JSON log (absolute path)

# ══════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


# ─────────────────────────────────────────────
# 主页
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return send_file(str(BASE_DIR / "agent_visualizer.html"))


# ─────────────────────────────────────────────
# 静态资源（css / js / picture）
# ─────────────────────────────────────────────
@app.route("/css/<path:filename>")
def serve_css(filename):
    return send_from_directory(str(BASE_DIR / "css"), filename)

@app.route("/js/<path:filename>")
def serve_js(filename):
    return send_from_directory(str(BASE_DIR / "js"), filename)

@app.route("/picture/<path:filename>")
def serve_picture(filename):
    return send_from_directory(str(BASE_DIR / "picture"), filename)


# ─────────────────────────────────────────────
# Frontend bootstrap config
# ─────────────────────────────────────────────
@app.route("/api/config")
def get_config():
    return jsonify({
        "mode":      "realtime",
        "log_file":  str(LOG_FILE),
        "has_log":   INITIAL_LOG is not None,
    })


# ─────────────────────────────────────────────
# 读取当前日志文件（用于初始加载）
# ─────────────────────────────────────────────
@app.route("/api/latest")
def get_latest():
    if not LOG_FILE.exists():
        return jsonify({"error": f"文件不存在: {LOG_FILE}"}), 404
    try:
        data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        return jsonify({"file": str(LOG_FILE.resolve()), "data": data})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON 解析失败: {e}"}), 400


# ─────────────────────────────────────────────
# 日志文件列表（服务器下拉面板用）
# ─────────────────────────────────────────────
@app.route("/api/logs")
def list_logs():
    files = sorted(
        list(BASE_DIR.glob("*.json")) + list(BASE_DIR.glob("*.log")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return jsonify({
        "logs": [
            {
                "name":      p.name,
                "path":      str(p.resolve()),
                "size":      p.stat().st_size,
                "mtime":     p.stat().st_mtime,
                "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
            }
            for p in files
        ]
    })


# ─────────────────────────────────────────────
# 读取指定文件
# ─────────────────────────────────────────────
@app.route("/api/log")
def get_log():
    path = request.args.get("path") or str(LOG_FILE)
    fp = Path(path)
    if not fp.exists():
        return jsonify({"error": f"文件不存在: {path}"}), 404
    try:
        return jsonify(json.loads(fp.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        return jsonify({"error": str(e)}), 400


# ─────────────────────────────────────────────
# SSE 实时推送 —— 每秒检测文件变化并推送
# ─────────────────────────────────────────────
@app.route("/api/stream")
def stream():
    watch_path = Path(request.args.get("path") or str(LOG_FILE))

    def generate():
        last_mtime: float = 0.0
        last_data: Optional[dict] = None

        # 连接建立确认
        yield 'event: connected\ndata: {"status":"ok"}\n\n'

        while True:
            try:
                if watch_path.exists():
                    mtime = watch_path.stat().st_mtime
                    if mtime != last_mtime:
                        last_mtime = mtime
                        try:
                            data = json.loads(watch_path.read_text(encoding="utf-8"))
                            if data != last_data:
                                last_data = data
                                payload = json.dumps(
                                    {"file": str(watch_path.resolve()), "data": data},
                                    ensure_ascii=False,
                                )
                                yield f"event: update\ndata: {payload}\n\n"
                        except json.JSONDecodeError:
                            pass   # 文件正在写入，跳过这次

                # 心跳，防止连接超时
                yield "event: heartbeat\ndata: {}\n\n"
                time.sleep(0.5)   # 每 0.5s 检测一次

            except GeneratorExit:
                break
            except Exception as e:
                yield f'event: error\ndata: {json.dumps({"error": str(e)})}\n\n'
                time.sleep(2.0)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/initial")
def get_initial():
    """Return the initial data the user selected at startup."""
    result: dict = {"log": None}

    if INITIAL_LOG:
        log_path = Path(INITIAL_LOG)
        if log_path.exists():
            try:
                data = json.loads(log_path.read_text(encoding="utf-8"))
                result["log"] = {"file": str(log_path.resolve()), "data": data}
            except Exception as exc:
                result["log"] = {"error": str(exc)}
        else:
            result["log"] = {"pending": True, "path": str(log_path)}

    return jsonify(result)


# ─────────────────────────────────────────────
# Programmatic entry point (called by loom ui)
# ─────────────────────────────────────────────
def start_server(
    port: int = 8080,
    auto_browser: bool = True,
    log_file: Optional[str] = None,
    agent_root: Optional[str] = None,
) -> None:
    """Start the visualisation server.

    Args:
        port: HTTP port to listen on.
        auto_browser: Whether to auto-open a browser tab.
        log_file: Absolute path to a JSON log for real-time monitoring.
        agent_root: Project root directory for resolving relative paths.
    """
    global LOG_FILE, PORT, AUTO_OPEN_BROWSER, AGENT_ROOT, INITIAL_LOG

    PORT = port
    AUTO_OPEN_BROWSER = auto_browser
    if agent_root:
        AGENT_ROOT = Path(agent_root).resolve()
    if log_file:
        lf = Path(log_file)
        LOG_FILE = lf
        INITIAL_LOG = str(lf.resolve()) if lf.is_absolute() else str((AGENT_ROOT / lf).resolve())

    # Banner
    log_label = str(LOG_FILE) if LOG_FILE.exists() else f"{LOG_FILE}  ⚠ waiting for file..."
    print(f"""
╔══════════════════════════════════════════════╗
║         Agent Visualisation Server            ║
╚══════════════════════════════════════════════╝
  URL   : http://localhost:{PORT}
  Log   : {log_label}
  Press Ctrl+C to stop
""")

    if AUTO_OPEN_BROWSER:
        def _open():
            time.sleep(1.0)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=_open, daemon=True).start()

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


# ─────────────────────────────────────────────
# CLI entry point (standalone usage)
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Agent Visualisation Server")
    parser.add_argument(
        "--log-file", "-l",
        type=str, default=None,
        help="JSON log file to monitor",
    )
    parser.add_argument(
        "--port", "-p",
        type=int, default=8080,
        help="HTTP port (default: 8080)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser",
    )
    args = parser.parse_args()

    start_server(
        port=args.port,
        auto_browser=not args.no_browser,
        log_file=args.log_file,
    )


if __name__ == "__main__":
    main()
