import os
from pathlib import Path

HOST = "127.0.0.1"
PORT = 7788
DEFAULT_VOICE = "af_heart"
SAMPLE_RATE = 24000
PARA_GAP = 0.2   # extra silence between paragraphs, on top of the kept chunk edges
EDGE_KEEP = 0.2  # Kokoro pads each chunk with ~0.4 s of silence; keep at most this much per side
MP3_BITRATE = "48k"  # 24 kHz mono speech: sounds the same as 96k at half the size


def port() -> int:
    """Listening port; KOKORO_READER_PORT overrides PORT (used only by the E2E live server)."""
    env = os.environ.get("KOKORO_READER_PORT")
    return int(env) if env else PORT


def home() -> Path:
    """Data directory; KOKORO_READER_HOME overrides the default. Created if missing."""
    env = os.environ.get("KOKORO_READER_HOME")
    p = Path(env).expanduser() if env else Path.home() / "Documents" / "Kokoro Reader"
    p.mkdir(parents=True, exist_ok=True)
    return p
