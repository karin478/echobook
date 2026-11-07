import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parent.parent / "launchd" / "com.kokoro-reader.plist"


def test_plist_does_not_request_interactive_process_type():
    # Interactive would exempt the whole process (incl. the conversion worker) from
    # throttling; the default (Standard) lets the worker's UTILITY QoS take effect.
    data = plistlib.loads(PLIST.read_bytes())
    assert data.get("ProcessType", "Standard") == "Standard"
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
