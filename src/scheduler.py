import dataclasses
import datetime as dt
import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, List

import requests
from dotenv import load_dotenv

load_dotenv()


@dataclasses.dataclass(frozen=True)
class TimeWindow:
    start_minute: int
    end_minute: int

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute == self.end_minute:
            return True
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


def _parse_hhmm(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if h < 0 or h > 23 or m < 0 or m > 59:
        raise ValueError(f"Invalid time '{value}'. Hour 0-23, minute 0-59")
    return h * 60 + m


def _windows_from_env() -> list[dict]:
    windows: list[dict] = []
    raw = os.getenv("PAUSE_WINDOWS", "").strip()
    if raw:
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or "-" not in chunk:
                continue
            start, end = chunk.split("-", 1)
            windows.append({"start": start.strip(), "end": end.strip()})
        return windows
    pause = os.getenv("PAUSE_TIME", "").strip()
    resume = os.getenv("RESUME_TIME", "").strip()
    if pause and resume:
        return [{"start": pause, "end": resume}]
    return []


def _parse_windows(windows: list[dict]) -> List[TimeWindow]:
    result: List[TimeWindow] = []
    for w in windows:
        start = str(w.get("start", "")).strip()
        end = str(w.get("end", "")).strip()
        if start and end:
            result.append(
                TimeWindow(start_minute=_parse_hhmm(start), end_minute=_parse_hhmm(end))
            )
    return result


def is_pause_time(now: dt.datetime, windows: Iterable[TimeWindow]) -> bool:
    minute = now.hour * 60 + now.minute
    return any(w.contains(minute) for w in windows)


class QBittorrentClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._authenticated = False

    def _post(self, endpoint: str, data: dict) -> requests.Response:
        return self.session.post(
            f"{self.base_url}{endpoint}", data=data, timeout=self.timeout
        )

    def login(self) -> None:
        response = self._post(
            "/api/v2/auth/login",
            {"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        if "ok" not in response.text.lower():
            raise RuntimeError(f"qBittorrent login failed: {response.text!r}")
        self._authenticated = True

    def _ensure_auth(self) -> None:
        if not self._authenticated:
            self.login()

    def get_torrents(self) -> list[dict]:
        self._ensure_auth()
        response = self.session.get(
            f"{self.base_url}/api/v2/torrents/info", timeout=self.timeout
        )
        if response.status_code == 403:
            self._authenticated = False
            self.login()
            response = self.session.get(
                f"{self.base_url}/api/v2/torrents/info", timeout=self.timeout
            )
        response.raise_for_status()
        return response.json()

    def _torrent_action(
        self, hashes: list[str], actions: list[tuple[str, str]]
    ) -> None:
        self._ensure_auth()
        response: requests.Response | None = None
        for endpoint, _ in actions:
            response = self._post(endpoint, {"hashes": "|".join(hashes)})
            if response.status_code not in (403, 404):
                break
        else:
            self._authenticated = False
            self.login()
            for endpoint, _ in actions:
                response = self._post(endpoint, {"hashes": "|".join(hashes)})
                if response.status_code not in (403, 404):
                    break
        response.raise_for_status()  # type: ignore[union-attr]

    def pause_hashes(self, hashes: list[str]) -> None:
        if not hashes:
            return
        self._torrent_action(
            hashes,
            [
                ("/api/v2/torrents/pause", "pause"),
                ("/api/v2/torrents/stop", "stop"),
            ],
        )

    def resume_hashes(self, hashes: list[str]) -> None:
        if not hashes:
            return
        self._torrent_action(
            hashes,
            [
                ("/api/v2/torrents/resume", "resume"),
                ("/api/v2/torrents/start", "start"),
            ],
        )


_PAUSED_STATES = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP", "pausedDL|pausedUP"}


def _hashes_to_pause(torrents: list[dict]) -> list[str]:
    return [
        str(t.get("hash", "")).strip()
        for t in torrents
        if str(t.get("state", "")) not in _PAUSED_STATES
        and str(t.get("hash", "")).strip()
    ]


def _hashes_to_resume(torrents: list[dict]) -> list[str]:
    return [
        str(t.get("hash", "")).strip()
        for t in torrents
        if str(t.get("state", "")) in _PAUSED_STATES
        and str(t.get("hash", "")).strip()
    ]


_LOG_BUFFER_SIZE = 200


class Scheduler:
    def __init__(self):
        self._config_lock = threading.Lock()
        self._config: dict = {}
        self._log_queue: queue.Queue[dict] = queue.Queue(maxsize=_LOG_BUFFER_SIZE)
        self._stop = threading.Event()
        self._reload_event = threading.Event()
        self._torrent_count = 0
        self._paused_count = 0
        self._is_paused = False
        self._client: QBittorrentClient | None = None
        self._client_lock = threading.Lock()
        self._running = False
        self._load_from_env()

    def _log(self, level: str, msg: str) -> None:
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {"time": ts, "level": level, "msg": msg}
        try:
            self._log_queue.put_nowait(entry)
        except queue.Full:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._log_queue.put_nowait(entry)
            except queue.Full:
                pass

    def _load_from_env(self) -> None:
        cfg = {
            "qbittorrent_url": os.getenv("QBITTORRENT_URL", "http://localhost:8080"),
            "qbittorrent_user": os.getenv("QBITTORRENT_USER", ""),
            "qbittorrent_password": os.getenv("QBITTORRENT_PASSWORD", ""),
            "scan_interval": int(os.getenv("SCAN_INTERVAL_SECONDS", "60")),
            "pause_windows": _windows_from_env(),
            "http_timeout": int(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
            "schedule_enabled": True,
        }
        with self._config_lock:
            self._config = cfg

    def get_config(self) -> dict:
        with self._config_lock:
            return dict(self._config)

    def update_config(self, data: dict) -> None:
        with self._config_lock:
            for key in [
                "qbittorrent_url",
                "qbittorrent_user",
                "qbittorrent_password",
                "scan_interval",
                "pause_windows",
                "http_timeout",
                "schedule_enabled",
            ]:
                if key in data:
                    self._config[key] = data[key]
        self._log("INFO", "Configuration updated via web UI")

    @property
    def _interval(self) -> int:
        with self._config_lock:
            return max(5, self._config.get("scan_interval", 60))

    @property
    def _timeout(self) -> int:
        with self._config_lock:
            return self._config.get("http_timeout", 10)

    @property
    def _windows(self) -> List[TimeWindow]:
        with self._config_lock:
            return _parse_windows(self._config.get("pause_windows", []))

    def _ensure_client(self) -> QBittorrentClient:
        with self._config_lock:
            url = self._config.get("qbittorrent_url", "")
            user = self._config.get("qbittorrent_user", "")
            password = self._config.get("qbittorrent_password", "")
            timeout = self._config.get("http_timeout", 10)
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = QBittorrentClient(url, user, password, timeout)
                    return self._client
        if (
            self._client.base_url != url.rstrip("/")
            or self._client.username != user
        ):
            with self._client_lock:
                self._client = QBittorrentClient(url, user, password, timeout)
                return self._client
        return self._client

    def get_status(self) -> dict:
        with self._config_lock:
            enabled = self._config.get("schedule_enabled", True)
        return {
            "is_paused": self._is_paused,
            "schedule_enabled": enabled,
            "pause_windows": [
                f"{w.start_minute//60:02d}:{w.start_minute%60:02d}-"
                f"{w.end_minute//60:02d}:{w.end_minute%60:02d}"
                for w in self._windows
            ],
            "torrent_count": self._torrent_count,
            "paused_count": self._paused_count,
            "scheduler_running": self._running,
        }

    def get_recent_logs(self) -> dict:
        logs: list[dict] = []
        while True:
            try:
                logs.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        for entry in logs:
            try:
                self._log_queue.put_nowait(entry)
            except queue.Full:
                break
        return {"logs": logs}

    def restart(self) -> None:
        self._log("INFO", "Restarting scheduler...")
        with self._client_lock:
            self._client = None
        self._load_from_env()
        self._reload_event.set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._running = True
        self._log("INFO", "Scheduler thread started")
        while not self._stop.is_set():
            self._run_tick()
            self._reload_event.wait(timeout=self._interval)
            self._reload_event.clear()
        self._running = False
        self._log("INFO", "Scheduler thread stopped")

    def _run_tick(self) -> None:
        try:
            with self._config_lock:
                enabled = self._config.get("schedule_enabled", True)

            client = self._ensure_client()
            now = dt.datetime.now().astimezone()
            windows = self._windows
            should_pause = enabled and is_pause_time(now, windows)
            self._is_paused = should_pause

            torrents = client.get_torrents()
            self._torrent_count = len(torrents)
            self._paused_count = sum(
                1 for t in torrents if str(t.get("state", "")) in _PAUSED_STATES
            )

            if should_pause:
                to_pause = _hashes_to_pause(torrents)
                if to_pause:
                    client.pause_hashes(to_pause)
                    self._log(
                        "INFO",
                        f"Pause window active ({now.strftime('%H:%M')}). Paused {len(to_pause)} torrent(s)",
                    )
            else:
                to_resume = _hashes_to_resume(torrents)
                if to_resume:
                    if not enabled:
                        self._log(
                            "INFO",
                            f"Schedule disabled ({now.strftime('%H:%M')}). Resumed {len(to_resume)} torrent(s)",
                        )
                    else:
                        self._log(
                            "INFO",
                            f"Outside pause window ({now.strftime('%H:%M')}). Resumed {len(to_resume)} torrent(s)",
                        )
                    client.resume_hashes(to_resume)

        except requests.RequestException as exc:
            self._log("WARNING", f"qBittorrent API request failed: {exc}")
        except Exception as exc:
            self._log("ERROR", f"Unexpected scheduler error: {exc}")


def make_app(scheduler: Scheduler, web_port: int, web_host: str):
    from flask import Flask, jsonify, render_template, request

    here = Path(__file__).parent.resolve()
    app = Flask(
        __name__,
        template_folder=str(here / "templates"),
        static_folder=str(here / "static"),
        static_url_path="/static",
    )

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(scheduler.get_config())

    @app.route("/api/config", methods=["PUT"])
    def put_config():
        data = request.get_json() or {}
        scheduler.update_config(data)
        return jsonify({"ok": True})

    @app.route("/api/status", methods=["GET"])
    def get_status():
        return jsonify(scheduler.get_status())

    @app.route("/api/logs", methods=["GET"])
    def get_logs():
        return jsonify(scheduler.get_recent_logs())

    @app.errorhandler(Exception)
    def handle_error(exc):
        scheduler._log("ERROR", f"Web server error: {exc}")
        return jsonify({"error": str(exc)}), 500

    return app


def configure_logging(scheduler: Scheduler) -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(handler)

    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        rec = old_factory(*args, **kwargs)
        scheduler._log(rec.levelname, rec.getMessage())
        return rec

    logging.setLogRecordFactory(record_factory)


def main() -> int:
    web_host = os.getenv("WEB_HOST", "0.0.0.0")
    web_port = int(os.getenv("WEB_PORT", "8081"))

    scheduler = Scheduler()
    configure_logging(scheduler)

    def _handle_stop(signum, _frame):
        logging.info("Received signal %s, shutting down", signum)
        scheduler.stop()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    app = make_app(scheduler, web_port, web_host)

    sched_thread = threading.Thread(target=scheduler.run, daemon=True)
    sched_thread.start()

    logging.info("Starting web UI on http://%s:%d", web_host, web_port)

    try:
        app.run(host=web_host, port=web_port, debug=False, threaded=True)
    finally:
        scheduler.stop()
        sched_thread.join(timeout=5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
