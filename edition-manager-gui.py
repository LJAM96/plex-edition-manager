#!/usr/bin/env python3
"""Edition Manager - Web front-end for headless environments."""

from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from croniter import croniter
from flask import Flask, jsonify, redirect, render_template, request, url_for

APP_TITLE = "Edition Manager"
APP_VERSION = "v2.0 - Web"
PROJECT_ROOT = Path(__file__).parent.resolve()
PRIMARY_SCRIPT = PROJECT_ROOT / "edition-manager.py"
CONFIG_FILE = PROJECT_ROOT / "config" / "config.ini"
LOG_LIMIT = 1500
AUTO_RUN_CRON = os.getenv("EM_AUTO_RUN_CRON", "").strip()
_HEX_DIGITS = set("0123456789abcdefABCDEF")

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
http_timeout = 30

[appearance]
primary_color = #6750A4
"""

MODULE_DEFAULT_ORDER: List[str] = [
    "Resolution",
    "Duration",
    "Rating",
    "Cut",
    "Release",
    "DynamicRange",
    "Country",
    "ContentRating",
    "Language",
    "AudioChannels",
    "Director",
    "Genre",
    "SpecialFeatures",
    "Studio",
    "AudioCodec",
    "Bitrate",
    "FrameRate",
    "Size",
    "Source",
    "VideoCodec",
]


class TaskRunner:
    """Background worker that executes the CLI and collects logs."""

    def __init__(self, script_path: Path) -> None:
        self.script_path = script_path
        self.project_root = script_path.parent
        self._lock = threading.Lock()
        self._logs: Deque[str] = deque(maxlen=LOG_LIMIT)
        self._progress = 0
        self._progress_counts: Dict[str, int] = {"done": 0, "total": 0}
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
            self._progress_counts = {"done": 0, "total": 0}
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
                "progress_counts": self._progress_counts.copy(),
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
            pct = None
            done = None
            total = None
            if len(parts) >= 2:
                try:
                    pct = float(parts[1])
                except ValueError:
                    pct = None
            if len(parts) >= 4:
                try:
                    done = int(parts[2])
                    total = int(parts[3])
                except ValueError:
                    done = total = None
            if pct is not None:
                pct = max(0.0, min(100.0, pct))
                with self._lock:
                    self._progress = pct
                    if done is not None and total is not None:
                        self._progress_counts = {"done": done, "total": total}

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

    def add_log_entry(self, message: str) -> None:
        self._append_log(message)


task_runner = TaskRunner(PRIMARY_SCRIPT)


def start_auto_run_scheduler() -> None:
    expression = AUTO_RUN_CRON
    if not expression:
        return
    try:
        iterator = croniter(expression, datetime.now())
    except (ValueError, KeyError) as exc:
        task_runner.add_log_entry(
            f"Auto-run disabled: invalid cron expression '{expression}': {exc}"
        )
        return

    def _worker() -> None:
        next_run = iterator.get_next(datetime)
        task_runner.add_log_entry(
            f"Auto-run enabled with cron '{expression}'. Next run at {next_run.strftime('%Y-%m-%d %H:%M')}"
        )
        while True:
            now = datetime.now()
            wait_seconds = (next_run - now).total_seconds()
            if wait_seconds > 1:
                time.sleep(min(wait_seconds, 30))
                continue
            if task_runner.status().get("running"):
                task_runner.add_log_entry("Auto-run skipped because a task is already running.")
            else:
                try:
                    task_runner.add_log_entry(
                        f"Auto-run triggered at {datetime.now().strftime('%H:%M:%S')}"
                    )
                    task_runner.start(ACTIONS["all"])
                except RuntimeError as exc:
                    task_runner.add_log_entry(f"Auto-run failed: {exc}")
            next_run = iterator.get_next(datetime)

    threading.Thread(target=_worker, daemon=True).start()


start_auto_run_scheduler()


def read_config_parser() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    else:
        cfg.read_string(DEFAULT_CONFIG_TEMPLATE)
    return cfg


def save_config_parser(cfg: configparser.ConfigParser) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        cfg.write(fh)


def ensure_section(cfg: configparser.ConfigParser, section: str) -> None:
    if not cfg.has_section(section):
        cfg.add_section(section)


def _looks_like_unbracketed_ipv6(value: str) -> bool:
    if not value or "[" in value or "]" in value:
        return False
    if value.count(":") < 2:
        return False
    stripped = value.replace(":", "")
    return bool(stripped) and all(char in _HEX_DIGITS for char in stripped)


def _split_host_port(netloc: str) -> Tuple[str, Optional[int]]:
    raw = (netloc or "").strip()
    if not raw:
        return "", None
    if "@" in raw:
        raw = raw.split("@", 1)[1]
    if raw.startswith("["):
        closing = raw.find("]")
        host = raw[1:closing] if closing != -1 else raw.lstrip("[")
        port: Optional[int] = None
        if closing != -1:
            remainder = raw[closing + 1 :]
            if remainder.startswith(":") and remainder[1:].isdigit():
                port = int(remainder[1:])
        return host, port
    if _looks_like_unbracketed_ipv6(raw):
        return raw, None
    if ":" not in raw:
        return raw, None
    parts = raw.split(":")
    tail = parts[-1]
    if not tail.isdigit():
        return raw, None
    port = int(tail)
    host = ":".join(parts[:-1]).strip()
    while host and ":" in host:
        head, sep, last = host.rpartition(":")
        if last.isdigit():
            host = head
            continue
        break
    if not host and parts[:-1]:
        host = parts[0]
    return host, port


def parse_server_address(address: str) -> Tuple[str, str, int]:
    if not address:
        return "http", "localhost", 32400
    normalized = address.strip() or "http://localhost:32400"
    normalized = normalized if "://" in normalized else f"http://{normalized}"
    parsed = urlparse(normalized)
    scheme = (parsed.scheme or "http").lower()
    netloc = parsed.netloc or ""
    if not netloc and parsed.path and "/" not in parsed.path:
        netloc = parsed.path
    host, port = _split_host_port(netloc)
    if not host:
        host = "localhost"
    if port is None:
        port = 443 if scheme == "https" else 32400
    return scheme, host, port


def build_server_address(scheme: str, host: str, port: int) -> str:
    if not host:
        return "http://localhost:32400"
    return f"{scheme}://{host}:{port}"


def get_available_modules() -> List[str]:
    modules_dir = PROJECT_ROOT / "modules"
    names: List[str] = []
    if modules_dir.exists():
        for path in sorted(modules_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            names.append(path.stem)
    if not names:
        names = MODULE_DEFAULT_ORDER.copy()
    return names


def get_selected_module_order(cfg: configparser.ConfigParser) -> List[str]:
    raw = cfg.get("modules", "order", fallback=";".join(MODULE_DEFAULT_ORDER))
    selected = [part.strip() for part in raw.split(";") if part.strip()]
    available = get_available_modules()
    cleaned = [m for m in selected if m in available]
    if not cleaned:
        cleaned = MODULE_DEFAULT_ORDER.copy()
    return cleaned


def build_module_items(cfg: configparser.ConfigParser) -> List[Dict[str, object]]:
    available = get_available_modules()
    selected = get_selected_module_order(cfg)
    ordered = selected + [m for m in available if m not in selected]
    selected_set = set(selected)
    return [{"name": name, "enabled": name in selected_set} for name in ordered]


def get_settings_snapshot() -> Dict[str, object]:
    cfg = read_config_parser()
    scheme, host, port = parse_server_address(cfg.get("server", "address", fallback="http://localhost:32400"))
    settings = {
        "server": {
            "scheme": scheme,
            "host": host,
            "port": port,
            "token": cfg.get("server", "token", fallback=""),
            "skip_libraries": cfg.get("server", "skip_libraries", fallback="").strip(),
        },
        "performance": {
            "max_workers": cfg.getint("performance", "max_workers", fallback=6),
            "batch_size": cfg.getint("performance", "batch_size", fallback=20),
            "http_timeout": cfg.getint("performance", "http_timeout", fallback=30),
        },
        "modules": {
            "selected": get_selected_module_order(cfg),
            "list": build_module_items(cfg),
        },
        "raw_text": load_config_text(),
    }
    return settings


def sanitize_module_order(order_raw: str) -> List[str]:
    available = get_available_modules()
    entries = [part.strip() for part in order_raw.split(";") if part.strip()]
    cleaned = [m for m in entries if m in available]
    if not cleaned:
        cleaned = MODULE_DEFAULT_ORDER.copy()
    return cleaned


def normalize_skip_list(raw_value: str) -> str:
    parts: List[str] = []
    for chunk in raw_value.replace(",", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return ";".join(parts)


def test_server_connection(scheme: str, host: str, port: int, token: str, timeout: int = 10) -> Tuple[str, Optional[str]]:
    if not host:
        raise RuntimeError("Server host cannot be empty.")
    if not token:
        raise RuntimeError("Plex token is required.")
    address = build_server_address(scheme, host, port)
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    url = f"{address}/library/sections"
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Unable to reach Plex server: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    friendly = data.get("MediaContainer", {}).get("friendlyName")
    return address, friendly


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
    settings = get_settings_snapshot()
    status = task_runner.status()
    message = request.args.get("message")
    error = request.args.get("error")
    return render_template(
        "index.html",
        app_title=APP_TITLE,
        app_version=APP_VERSION,
        settings=settings,
        config_text=settings["raw_text"],
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


@app.post("/settings")
def save_settings():
    form = request.form
    scheme = form.get("server_scheme", "http").lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    host = form.get("server_host", "").strip()
    if not host:
        return redirect(url_for("index", error="Server host cannot be empty."))
    try:
        port = int(form.get("server_port", "").strip() or ("443" if scheme == "https" else "32400"))
    except ValueError:
        port = 32400
    token = form.get("server_token", "").strip()
    skip_libraries = normalize_skip_list(form.get("skip_libraries", ""))
    module_order_input = form.get("module_order", "")
    module_order = sanitize_module_order(module_order_input)

    def _parse_int(name: str, default: int, minimum: int = 1) -> int:
        try:
            value = int(form.get(name, default))
            return max(minimum, value)
        except (TypeError, ValueError):
            return default

    max_workers = _parse_int("max_workers", 6, 1)
    batch_size = _parse_int("batch_size", 20, 1)
    http_timeout = _parse_int("http_timeout", 30, 5)

    cfg = read_config_parser()
    ensure_section(cfg, "server")
    ensure_section(cfg, "modules")
    ensure_section(cfg, "performance")

    cfg.set("server", "address", build_server_address(scheme, host, port))
    cfg.set("server", "token", token)
    cfg.set("server", "skip_libraries", skip_libraries)

    cfg.set("modules", "order", ";".join(module_order))

    cfg.set("performance", "max_workers", str(max_workers))
    cfg.set("performance", "batch_size", str(batch_size))
    cfg.set("performance", "http_timeout", str(http_timeout))

    save_config_parser(cfg)
    return redirect(url_for("index", message="Settings saved."))


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


@app.post("/api/server/test")
def test_server_endpoint():
    payload = request.get_json(silent=True) or {}
    scheme = (payload.get("scheme") or "http").strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    host = (payload.get("host") or "").strip()
    token = (payload.get("token") or "").strip()
    port_raw = str(payload.get("port") or "").strip()
    default_port = "443" if scheme == "https" else "32400"
    try:
        port = int(port_raw or default_port)
    except ValueError:
        return jsonify({"error": "Port must be a number."}), 400
    if not host or not token:
        return jsonify({"error": "Server host and Plex token are required."}), 400
    try:
        address, friendly = test_server_connection(scheme, host, port, token)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "status": "ok",
            "address": address,
            "server_name": friendly or host,
        }
    )


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
