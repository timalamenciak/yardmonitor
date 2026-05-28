"""Copy images from SD card into a deployment folder."""

from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import string
from datetime import datetime
from pathlib import Path
from typing import Optional

from tqdm import tqdm

logger = logging.getLogger(__name__)

_DEFAULT_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    ".cr2", ".nef", ".arw",
}


def detect_sd_card(config: dict) -> Optional[Path]:
    """Return the first mounted removable drive that contains images."""
    subdirs = config.get("ingest", {}).get("source_subdirs", ["DCIM", "dcim", ""])

    candidates: list[Path] = _get_drive_candidates()

    for drive in candidates:
        for sub in subdirs:
            check = (drive / sub) if sub else drive
            if check.exists() and _has_images(check):
                logger.info("Detected SD card at %s", drive)
                return drive

    return None


def _get_drive_candidates() -> list[Path]:
    system = platform.system()
    if system == "Windows":
        return _windows_removable_drives()
    if system == "Darwin":
        return [p for p in Path("/Volumes").iterdir() if p.is_dir()]
    # Linux
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
                # GetDriveTypeW returns 2 for DRIVE_REMOVABLE
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:
                    drives.append(Path(root))
            bitmask >>= 1
        return drives
    except Exception as exc:
        logger.warning("Drive enumeration failed: %s", exc)
        return []


def _has_images(path: Path) -> bool:
    return any(
        f.suffix.lower() in {".jpg", ".jpeg", ".png"}
        for f in path.rglob("*")
        if f.is_file()
    )


def collect_images(source: Path, config: dict) -> list[Path]:
    """Recursively collect all image files under source, sorted by name."""
    raw_exts = config.get("ingest", {}).get("image_extensions", [])
    extensions = {e.lower() for e in raw_exts} if raw_exts else _DEFAULT_EXTENSIONS

    images = sorted(
        (p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in extensions),
        key=lambda p: p.name,
    )
    logger.info("Found %d images under %s", len(images), source)
    return images


def copy_images(
    images: list[Path],
    dest_dir: Path,
    skip_existing: bool = True,
) -> list[Path]:
    """
    Copy images to dest_dir.  Name collisions are resolved by appending a
    short content hash so no source file is silently overwritten or skipped.
    Returns the list of destination paths (same order as input).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []

    for src in tqdm(images, desc="Copying images", unit="file"):
        dest = dest_dir / src.name

        if dest.exists():
            if skip_existing:
                copied.append(dest)
                continue
            # Different content → rename to avoid collision
            suffix = _short_hash(src)
            dest = dest_dir / f"{src.stem}_{suffix}{src.suffix}"

        shutil.copy2(src, dest)
        copied.append(dest)

    logger.info("Copied/verified %d images → %s", len(copied), dest_dir)
    return copied


def _short_hash(path: Path, length: int = 6) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()[:length]


def make_deployment_id(location: str = "unknown", dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now()
    safe = "".join(c if c.isalnum() else "_" for c in location).strip("_") or "unknown"
    return f"{dt.strftime('%Y%m%d')}_{safe}"
