"""Generate timelapse MP4 from sorted camera trap images via ffmpeg."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TimelapseGenerator:
    """Produce a timelapse video from a collection of images."""

    def __init__(self, config: dict):
        cfg = config.get("timelapse", {})
        self.enabled: bool = cfg.get("enabled", True)
        self.fps: int = cfg.get("fps", 10)
        self.resize_width: Optional[int] = cfg.get("resize_width", 1280)
        self.codec: str = cfg.get("codec", "libx264")

    def generate(
        self,
        images: list[Path],
        output_dir: Path,
        timestamps: Optional[dict[str, datetime]] = None,
        deployment_id: str = "timelapse",
    ) -> Optional[Path]:
        """
        Create a timelapse video from `images`.

        Images are sorted by timestamp (if provided) or filename.
        Returns the output path on success, None if skipped or failed.
        """
        if not self.enabled:
            return None

        if not shutil.which("ffmpeg"):
            logger.warning(
                "ffmpeg not found — timelapse skipped. "
                "Install from https://ffmpeg.org and ensure it is on PATH."
            )
            return None

        if not images:
            logger.warning("No images provided for timelapse")
            return None

        sorted_imgs = _sort(images, timestamps)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"{deployment_id}_timelapse.mp4"

        logger.info(
            "Generating timelapse: %d frames @ %d fps → %s",
            len(sorted_imgs), self.fps, out_path,
        )

        # Build an ffconcat file list for reliable cross-platform ordering
        frame_duration = 1.0 / self.fps
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            list_path = Path(fh.name)
            for img in sorted_imgs:
                # Backslashes in Windows paths must be escaped or replaced
                safe = str(img.absolute()).replace("\\", "/")
                fh.write(f"file '{safe}'\n")
                fh.write(f"duration {frame_duration:.6f}\n")
            # ffconcat needs the last file repeated without a duration line
            safe = str(sorted_imgs[-1].absolute()).replace("\\", "/")
            fh.write(f"file '{safe}'\n")

        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c:v", self.codec,
                "-pix_fmt", "yuv420p",
            ]
            if self.resize_width:
                # -2 keeps aspect ratio and ensures even height (required by libx264)
                cmd += ["-vf", f"scale={self.resize_width}:-2"]
            cmd.append(str(out_path))

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode != 0:
                logger.error("ffmpeg failed:\n%s", proc.stderr[-800:])
                return None

            logger.info("Timelapse saved → %s", out_path)
            return out_path

        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timed out")
            return None
        except Exception as exc:
            logger.error("Timelapse generation error: %s", exc)
            return None
        finally:
            list_path.unlink(missing_ok=True)


def _sort(
    images: list[Path],
    timestamps: Optional[dict[str, datetime]],
) -> list[Path]:
    if timestamps:
        return sorted(images, key=lambda p: timestamps.get(p.name, datetime.min))
    return sorted(images, key=lambda p: p.name)
