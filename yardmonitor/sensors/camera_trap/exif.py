"""Extract EXIF metadata from images via the ExifTool CLI."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DT_FORMATS = [
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
]


def extract_exif_batch(images: list[Path]) -> dict[str, dict]:
    """
    Extract EXIF for all images in a single ExifTool call.
    Returns {filename: exif_dict}.  Falls back to per-image calls on error.
    """
    if not images:
        return {}

    try:
        result = subprocess.run(
            ["exiftool", "-j", "-n"] + [str(p) for p in images],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            records = json.loads(result.stdout)
            return {Path(r.get("SourceFile", "")).name: r for r in records}
        logger.warning("ExifTool batch exited %d: %s", result.returncode, result.stderr[:300])
    except FileNotFoundError:
        logger.warning(
            "ExifTool not found — metadata will be empty. "
            "Download from https://exiftool.org and ensure it is on PATH."
        )
    except Exception as exc:
        logger.warning("ExifTool batch failed (%s), falling back to per-image", exc)

    # Per-image fallback
    out: dict[str, dict] = {}
    for img in images:
        out[img.name] = _extract_single(img)
    return out


def _extract_single(image_path: Path) -> dict:
    try:
        result = subprocess.run(
            ["exiftool", "-j", "-n", str(image_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data[0] if data else {}
    except Exception as exc:
        logger.debug("EXIF failed for %s: %s", image_path.name, exc)
    return {}


def parse_timestamp(exif: dict) -> Optional[datetime]:
    """Return the best-available capture datetime from an EXIF dict."""
    for field in ("DateTimeOriginal", "CreateDate", "ModifyDate", "DateTime"):
        raw = exif.get(field)
        if not raw:
            continue
        for fmt in _DT_FORMATS:
            try:
                return datetime.strptime(str(raw).strip(), fmt)
            except ValueError:
                continue
    return None


def parse_gps(exif: dict) -> tuple[Optional[float], Optional[float]]:
    """Return (latitude, longitude) from EXIF, or (None, None)."""
    try:
        return float(exif["GPSLatitude"]), float(exif["GPSLongitude"])
    except (KeyError, TypeError, ValueError):
        return None, None
