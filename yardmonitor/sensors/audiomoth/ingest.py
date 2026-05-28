"""Collect and copy WAV files from an AudioMoth SD card."""

from __future__ import annotations

import logging
import platform
import re
import shutil
import string
from datetime import datetime
from pathlib import Path
from typing import Optional

from tqdm import tqdm

logger = logging.getLogger(__name__)

_AUDIO_EXTS = {".wav", ".WAV", ".flac", ".mp3", ".ogg"}


def detect_sd_card() -> Optional[Path]:
    """Return first mounted removable drive containing WAV files."""
    for candidate in _drive_candidates():
        if _has_audio(candidate):
            logger.info("Detected AudioMoth SD card at %s", candidate)
            return candidate
    return None


def collect_audio(source: Path) -> list[Path]:
    """Recursively collect audio files from source, sorted by name."""
    files = sorted(
        (p for p in source.rglob("*") if p.is_file() and p.suffix in _AUDIO_EXTS),
        key=lambda p: p.name,
    )
    logger.info("Found %d audio files under %s", len(files), source)
    return files


def copy_audio(files: list[Path], dest_dir: Path, skip_existing: bool = True) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for src in tqdm(files, desc="Copying audio", unit="file"):
        dest = dest_dir / src.name
        if dest.exists() and skip_existing:
            copied.append(dest)
            continue
        shutil.copy2(src, dest)
        copied.append(dest)
    logger.info("Copied %d audio files → %s", len(copied), dest_dir)
    return copied


def parse_audiomoth_datetime(filename: str) -> Optional[datetime]:
    """Parse AudioMoth filename format 20240615_183000.WAV → datetime."""
    m = re.match(r"(\d{8})_(\d{6})", filename)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return None


def make_deployment_id(location: str = "unknown", dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now()
    safe = "".join(c if c.isalnum() else "_" for c in location).strip("_") or "unknown"
    return f"{dt.strftime('%Y%m%d')}_{safe}"


# ── Helpers ───────────────────────────────────────────────────────────────


def _drive_candidates() -> list[Path]:
    system = platform.system()
    if system == "Windows":
        return _windows_removable_drives()
    if system == "Darwin":
        return [p for p in Path("/Volumes").iterdir() if p.is_dir()]
    candidates: list[Path] = []
    for base in (Path("/media"), Path("/run/media")):
        if base.exists():
            for entry in base.iterdir():
                if entry.is_dir():
                    candidates.extend(p for p in entry.iterdir() if p.is_dir())
    return candidates


def _windows_removable_drives() -> list[Path]:
    try:
        import ctypes
        drives: list[Path] = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                root = f"{letter}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:
                    drives.append(Path(root))
            bitmask >>= 1
        return drives
    except Exception as exc:
        logger.warning("Drive enumeration failed: %s", exc)
        return []


def _has_audio(path: Path) -> bool:
    return any(
        f.suffix in _AUDIO_EXTS for f in path.rglob("*") if f.is_file()
    )
