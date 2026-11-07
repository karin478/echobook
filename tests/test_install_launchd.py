import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-launchd.sh"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

# Fake launchctl: the old service stays visible to `print` for 2 calls after bootout, and the
# first bootstrap fails with error 5 (what real launchd does while a bootout is still settling).
FAKE = """#!/bin/bash
state="$FAKE_DIR/state"; log="$FAKE_DIR/calls.log"
echo "$1" >> "$log"
case "$1" in
  print) n=$(cat "$state.print" 2>/dev/null || echo 0); echo $((n+1)) > "$state.print"; [ "$n" -lt 2 ] && exit 0 || exit 113 ;;
  bootstrap) n=$(cat "$state.boot" 2>/dev/null || echo 0); echo $((n+1)) > "$state.boot"; [ "$n" -lt 1 ] && exit 5 || exit 0 ;;
  *) exit 0 ;;
esac
"""


def test_install_waits_for_bootout_and_retries_bootstrap(tmp_path):
    fake = tmp_path / "launchctl"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path), FAKE_DIR=str(tmp_path),
               KOKORO_LAUNCHCTL=str(fake), KOKORO_RETRY_SLEEP="0.05")
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    calls = (tmp_path / "calls.log").read_text().split()
    assert calls[0] == "bootout"
    assert calls.count("print") >= 3          # polled until the old service was gone
    assert calls.count("bootstrap") == 2      # first attempt failed, retried once
    assert (tmp_path / "Library" / "LaunchAgents" / "com.kokoro-reader.plist").exists()
