"""TradingView login — real Chrome + CDP cookie extraction.

Launches Chrome with --remote-debugging-port, polls cookies via
Chrome DevTools Protocol HTTP API. Cookies come back decrypted
(no need for DPAPI/AES). Closes Chrome automatically on success.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar,
)

logger = logging.getLogger(__name__)

TV_SIGNIN_URL = "https://www.tradingview.com/accounts/signin/"
ENV_PATH = Path(__file__).resolve().parent.parent.parent / "passes_tv.env"
CDP_PORT = 19222

_CHROME_PATHS = [
    Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


def _find_chrome() -> Path | None:
    for p in _CHROME_PATHS:
        if p.exists():
            return p
    return None


def _cdp_get_sessionid() -> str | None:
    """Get TradingView sessionid via Chrome DevTools Protocol."""
    try:
        # Find a TradingView tab
        resp = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
        pages = json.loads(resp.read())

        ws_url = None
        for page in pages:
            if "tradingview.com" in page.get("url", ""):
                ws_url = page.get("webSocketDebuggerUrl")
                break

        if not ws_url:
            return None

        # Use WebSocket to call Network.getCookies
        from websocket import create_connection
        ws = create_connection(ws_url, timeout=3)
        ws.send(json.dumps({
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": ["https://www.tradingview.com"]},
        }))
        result = json.loads(ws.recv())
        ws.close()

        cookies = result.get("result", {}).get("cookies", [])
        for c in cookies:
            if c.get("name") == "sessionid" and "tradingview" in c.get("domain", ""):
                value = c["value"]
                if value and len(value) > 10:
                    return value

    except Exception as e:
        logger.debug("[TV_LOGIN] CDP poll: %s", e)

    return None


class TvLoginDialog(QDialog):
    """Launch Chrome, poll cookies via CDP, auto-save sessionid.

    Emits `token_obtained(str)` on success.
    """

    token_obtained = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TradingView — Connect Premium")
        self.setFixedSize(420, 170)

        self._session_id: str = ""
        self._chrome_proc: subprocess.Popen | None = None
        self._tmp_dir: str = ""
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(20, 16, 20, 16)

        title = QLabel("Connect TradingView Premium")
        title.setStyleSheet("font-size:15px; font-weight:bold;")
        layout.addWidget(title)

        chrome_path = _find_chrome()
        if not chrome_path:
            layout.addWidget(QLabel("Chrome not found. Please install Google Chrome."))
            layout.addStretch()
            return

        desc = QLabel(
            "Chrome will open — sign in to TradingView via Google.\n"
            "This window will detect login automatically."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#b0b4bc;")
        layout.addWidget(desc)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setFixedHeight(4)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel("Launching Chrome...")
        self._status.setStyleSheet("color:#758696; font-size:11px;")
        layout.addWidget(self._status)

        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_skip = QPushButton("Skip")
        btn_skip.setStyleSheet("color:#758696; border:none; font-size:11px;")
        btn_skip.clicked.connect(self.reject)
        bottom.addWidget(btn_skip)
        layout.addLayout(bottom)

        # Launch Chrome
        self._tmp_dir = tempfile.mkdtemp(prefix="tv_login_")
        self._chrome_proc = subprocess.Popen([
            str(chrome_path),
            f"--user-data-dir={self._tmp_dir}",
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            TV_SIGNIN_URL,
        ])

        QTimer.singleShot(3000, self._start_polling)

    def _start_polling(self) -> None:
        self._status.setText("Waiting for TradingView login...")
        self._poll_timer.start(2000)

    def _poll(self) -> None:
        # Chrome closed?
        if self._chrome_proc and self._chrome_proc.poll() is not None:
            self._poll_timer.stop()
            self._status.setText("Chrome was closed before login detected.")
            self._progress.setRange(0, 1)
            self._progress.setValue(0)
            self._cleanup()
            return

        token = _cdp_get_sessionid()
        if token:
            self._on_token_found(token)

    def _on_token_found(self, token: str) -> None:
        self._poll_timer.stop()
        self._session_id = token
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._status.setText("Login detected! Saving token...")
        self._save_token(token)
        self.token_obtained.emit(token)
        logger.info("[TV_LOGIN] Got sessionid via CDP (%d chars)", len(token))
        self._kill_chrome()
        QTimer.singleShot(1000, self.accept)

    def _kill_chrome(self) -> None:
        if self._chrome_proc and self._chrome_proc.poll() is None:
            try:
                self._chrome_proc.terminate()
                self._chrome_proc.wait(timeout=5)
            except Exception:
                try:
                    self._chrome_proc.kill()
                except Exception:
                    pass
        self._cleanup()

    def _cleanup(self) -> None:
        if self._tmp_dir:
            try:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except Exception:
                pass
            self._tmp_dir = ""

    def _save_token(self, token: str) -> None:
        env_file = ENV_PATH
        lines: list[str] = []
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()

        found = False
        for i, line in enumerate(lines):
            if line.startswith("TV_AUTH_TOKEN="):
                lines[i] = f"TV_AUTH_TOKEN={token}"
                found = True
                break

        if not found:
            lines.append(f"TV_AUTH_TOKEN={token}")

        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("[TV_LOGIN] Saved TV_AUTH_TOKEN to %s", env_file)

    def get_token(self) -> str:
        return self._session_id

    def reject(self) -> None:
        self._poll_timer.stop()
        self._kill_chrome()
        super().reject()
