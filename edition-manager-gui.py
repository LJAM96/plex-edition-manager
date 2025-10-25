#!/usr/bin/env python3
"""Edition Manager - Web front-end for headless environments."""

from __future__ import annotations

import argparse
import configparser
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Optional
import subprocess

from flask import Flask, jsonify, redirect, render_template, request, url_for

APP_TITLE = "Edition Manager"
APP_VERSION = "v2.0 - Web"
PROJECT_ROOT = Path(__file__).parent.resolve()
PRIMARY_SCRIPT = PROJECT_ROOT / "edition-manager.py"
CONFIG_FILE = PROJECT_ROOT / "config" / "config.ini"
LOG_LIMIT = 1500

ACTIONS: Dict[str, str] = {
    "all": "--all",
    "reset": "--reset",
    "backup": "--backup",
    "restore": "--restore",
}

DEFAULT_CONFIG_TEMPLATE = """[server]
address = http://localhost:32400
token =
skip_libraries =

[modules]
order = Resolution;AudioCodec;DynamicRange

[language]
excluded_languages =
skip_multiple_audio_tracks = no

[rating]
source = imdb
rotten_tomatoes_type = critic
tmdb_api_key =

[performance]
max_workers = 6
batch_size = 20

[appearance]
primary_color = #6750A4
"""


class TaskRunner:
    """Background worker that executes the CLI and collects logs."""

    def __init__(self, script_path: Path) -> None:
        self.script_path = script_path
        self.project_root = script_path.parent
        self._lock = threading.Lock()
        self._logs: Deque[str] = deque(maxlen=LOG_LIMIT)
        self._progress = 0
        self._current_flag: Optional[str] = None
        self._running = False
        self._exit_code: Optional[int] = None
        self._proc: Optional[subprocess.Popen[str]] = None
        self._started_at: Optional[str] = None

    def start(self, flag: str) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("Another operation is already running.")
            self._running = True
            self._progress = 0
            self._exit_code = None
            self._logs.clear()
            self._current_flag = flag
            self._started_at = datetime.utcnow().isoformat()

        worker = threading.Thread(target=self._run_process, args=(flag,), daemon=True)
        worker.start()

    def cancel(self) -> bool:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            self._append_log("Termination requested, stopping process...")
            proc.terminate()
            return True
        return False

    def status(self) -> Dict[str, Optional[str]]:
        with self._lock:
            return {
                "running": self._running,
                "progress": self._progress,
                "current_flag": self._current_flag,
                "exit_code": self._exit_code,
                "logs": list(self._logs),
                "started_at": self._started_at,
            }

    # Internal helpers -------------------------------------------------
    def _run_process(self, flag: str) -> None:
        if not self.script_path.exists():
            self._append_log(f"Error: '{self.script_path.name}' not found next to the web UI.")
            self._finish(flag, 1)
            return

        python_exe = sys.executable or "python3"
        cmd = [python_exe, str(self.script_path), flag]
        self._append_log(f"Running: {' '.join(cmd)}")

        try:
            with subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            ) as proc:
                with self._lock:
                    self._proc = proc
                for raw_line in proc.stdout or []:
                    line = raw_line.rstrip("\n")
                    self._handle_output(line)
                proc.wait()
                exit_code = proc.returncode
        except FileNotFoundError:
            self._append_log("Error: Python interpreter not found.")
            exit_code = 1
        except Exception as exc:  # pragma: no cover - defensive
            self._append_log(f"Unexpected error: {exc}")
            exit_code = 1
        finally:
            with self._lock:
                self._proc = None

        self._finish(flag, exit_code)

    def _handle_output(self, line: str) -> None:
        self._append_log(line)
        if line.startswith("PROGRESS "):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                pct = int(parts[1])
                pct = max(0, min(100, pct))
                with self._lock:
                    self._progress = pct

    def _finish(self, flag: str, exit_code: Optional[int]) -> None:
        if exit_code == 0:
            self._append_log("Job completed successfully.")
        else:
            self._append_log(f"Job finished with exit code {exit_code}.")
        with self._lock:
            self._running = False
            self._progress = 100 if exit_code == 0 else self._progress
            self._exit_code = exit_code
            self._current_flag = flag

    def _append_log(self, message: str) -> None:
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append(f"[{stamp}] {message}")


task_runner = TaskRunner(PRIMARY_SCRIPT)
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False


def load_config_text() -> str:
    if CONFIG_FILE.exists():
        return CONFIG_FILE.read_text(encoding="utf-8")
    return DEFAULT_CONFIG_TEMPLATE.strip()


def validate_config_text(text: str) -> Optional[str]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
        return None
    except configparser.Error as exc:
        return f"Invalid config: {exc}"


def save_config_text(text: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(text, encoding="utf-8")


@app.route("/", methods=["GET"])
def index():
    config_text = load_config_text()
    status = task_runner.status()
    message = request.args.get("message")
    error = request.args.get("error")
    return render_template(
        "index.html",
        app_title=APP_TITLE,
        app_version=APP_VERSION,
        config_text=config_text,
        status=status,
        message=message,
        error=error,
    )


@app.post("/config")
def update_config():
    config_text = request.form.get("config_text", "")
    error = validate_config_text(config_text)
    if error:
        return redirect(url_for("index", error=error))
    save_config_text(config_text)
    return redirect(url_for("index", message="Configuration saved."))


@app.post("/api/run")
def run_action():
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if action not in ACTIONS:
        return jsonify({"error": "Unknown action."}), 400
    flag = ACTIONS[action]
    try:
        task_runner.start(flag)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"status": "started", "flag": flag})


@app.post("/api/cancel")
def cancel_action():
    if task_runner.cancel():
        return jsonify({"status": "terminating"})
    return jsonify({"error": "No running process to cancel."}), 400


@app.get("/api/status")
def get_status():
    return jsonify(task_runner.status())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edition Manager web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to serve the web UI (default: 8000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser when starting")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.no_browser and args.host in {"127.0.0.1", "localhost"}:
        threading.Thread(
            target=lambda: (time.sleep(1.2), webbrowser.open(f"http://{args.host}:{args.port}")),
            daemon=True,
        ).start()
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
