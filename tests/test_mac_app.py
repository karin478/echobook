import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "macapp" / "launcher.sh"
APP_NAME = "Echobook.app"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def fake_tool(path: Path, log: Path) -> Path:
    path.write_text(f'#!/bin/bash\necho "$(basename "$0") $*" >> "{log}"\n')
    path.chmod(0o755)
    return path


def run_launcher(tmp_path, url):
    log = tmp_path / "calls.log"
    env = dict(
        os.environ,
        KOKORO_URL=url,
        KOKORO_CHROME=str(fake_tool(tmp_path / "chrome", log)),
        KOKORO_LAUNCHCTL=str(fake_tool(tmp_path / "launchctl", log)),
        KOKORO_OSASCRIPT=str(fake_tool(tmp_path / "osascript", log)),
        KOKORO_WAIT_STEPS="2",
    )
    rc = subprocess.run(["bash", str(LAUNCHER)], env=env, timeout=30).returncode
    if rc == 0:  # chrome is started in the background: wait for its log line
        for _ in range(50):
            if log.exists() and "chrome" in log.read_text():
                break
            time.sleep(0.1)
    return rc, log.read_text() if log.exists() else ""


def test_launcher_opens_app_window_when_service_up(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class OK(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), OK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/"
        rc, calls = run_launcher(tmp_path, url)
    finally:
        srv.shutdown()
    assert rc == 0
    assert f"chrome --app={url}" in calls
    assert "launchctl" not in calls  # service already up: don't touch launchd


def test_launcher_kickstarts_service_and_alerts_when_still_down(tmp_path):
    rc, calls = run_launcher(tmp_path, "http://127.0.0.1:9/")  # nothing listens on port 9
    assert rc != 0
    assert "launchctl kickstart gui/" in calls and "com.kokoro-reader" in calls
    assert "osascript" in calls
    assert "chrome" not in calls


def test_build_creates_app_bundle(tmp_path):
    subprocess.run(["bash", str(REPO / "scripts" / "build-mac-app.sh"), str(tmp_path)], check=True, timeout=180)
    app = tmp_path / APP_NAME
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleName"] == "Echobook"
    assert info["CFBundleIdentifier"] == "com.kokoro-reader.launcher"
    exe = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
    assert os.access(exe, os.X_OK)
    assert exe.read_text() == LAUNCHER.read_text()
    assert (app / "Contents" / "Resources" / f"{info['CFBundleIconFile']}.icns").stat().st_size > 10_000
