import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import MP3_BITRATE, SAMPLE_RATE

HOMEBREW_FFMPEG = Path("/opt/homebrew/bin/ffmpeg")


def ffmpeg_path() -> str:
    """KOKORO_READER_FFMPEG, else Homebrew's (launchd's PATH lacks /opt/homebrew/bin), else PATH."""
    if os.environ.get("KOKORO_READER_FFMPEG"):
        return os.environ["KOKORO_READER_FFMPEG"]
    if HOMEBREW_FFMPEG.is_file():
        return str(HOMEBREW_FFMPEG)
    return shutil.which("ffmpeg") or "ffmpeg"


def _run_ffmpeg(*args: str) -> None:
    subprocess.run([ffmpeg_path(), "-loglevel", "error", "-y", *args], check=True)


def trim_silence(samples: np.ndarray, keep: float, threshold: float = 0.01) -> tuple[np.ndarray, float]:
    """Shorten leading/trailing silence to at most `keep` seconds per side.

    Returns the trimmed samples and how many seconds were cut from the front
    (callers shift timestamps by it). All-silent audio is returned unchanged.
    """
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if loud.size == 0:
        return samples, 0.0
    margin = int(keep * SAMPLE_RATE)
    start = max(0, loud[0] - margin)
    end = min(len(samples), loud[-1] + 1 + margin)
    return samples[start:end], start / SAMPLE_RATE


def write_mp3(samples: np.ndarray, dst: Path) -> float:
    """Encode samples as an mp3 (MP3_BITRATE) at dst; returns duration in seconds (0.0 and no file if empty)."""
    if len(samples) == 0:
        return 0.0
    with tempfile.TemporaryDirectory() as tmp:
        wav, out = Path(tmp) / "in.wav", Path(tmp) / "out.mp3"
        sf.write(wav, samples, SAMPLE_RATE)
        _run_ffmpeg("-i", str(wav), "-b:a", MP3_BITRATE, str(out))
        Path(dst).write_bytes(out.read_bytes())  # the caller owns the folder (never recreate a deleted book)
    return len(samples) / SAMPLE_RATE


def cut_clip(src_mp3: Path, start: float, end: float, dst: Path, pad: float = 0.15) -> None:
    """Cut [start-pad, end+pad] out of src_mp3 into dst (re-encoded mp3)."""
    a = max(0.0, start - pad)
    b = end + pad
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp3"
        _run_ffmpeg("-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src_mp3), "-b:a", MP3_BITRATE, str(out))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(out.read_bytes())
