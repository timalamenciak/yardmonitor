"""BirdNET-Analyzer wrapper via birdnetlib.

BirdNET-Analyzer v2.4 (Cornell Lab) can identify 6362 bird and wildlife species
from 3-second audio windows.  This wrapper loads the model once and reuses it
across all files in a deployment.

Install:  pip install birdnetlib
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class BirdNetAnalyzer:
    """Run BirdNET-Analyzer on WAV files; return per-detection results."""

    def __init__(self, params: dict):
        self.min_confidence: float = float(params.get("min_confidence", 0.1))
        self.overlap: float = float(params.get("overlap", 0.0))
        self.sensitivity: float = float(params.get("sensitivity", 1.0))
        self._analyzer = None

    def _load(self) -> None:
        if self._analyzer is not None:
            return
        try:
            from birdnetlib.analyzer import Analyzer
            self._analyzer = Analyzer()
            logger.info("BirdNET-Analyzer loaded")
        except ImportError as exc:
            raise ImportError(
                f"birdnetlib failed to import ({exc}). "
                "If it is installed, a dependency may be missing. "
                "Run: pip install birdnetlib"
            ) from exc

    def analyze(
        self,
        wav_path: Path,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        date: Optional[str] = None,
    ) -> list[dict]:
        """
        Analyze a WAV file and return a list of detections:
          [{common_name, scientific_name, confidence, start_time, end_time}, ...]
        """
        self._load()
        from birdnetlib import Recording

        dt = None
        if date:
            try:
                dt = datetime.fromisoformat(date)
            except ValueError:
                pass

        kwargs: dict = {"min_conf": self.min_confidence, "overlap": self.overlap}
        if lat is not None:
            kwargs["lat"] = lat
        if lon is not None:
            kwargs["lon"] = lon
        if dt is not None:
            kwargs["date"] = dt

        rec = Recording(self._analyzer, str(wav_path), **kwargs)
        rec.analyze()

        results: list[dict] = []
        for det in rec.detections:
            results.append({
                "common_name": det.get("common_name", ""),
                "scientific_name": det.get("scientific_name", ""),
                "confidence": round(float(det.get("confidence", 0)), 4),
                "start_time": float(det.get("start_time", 0)),
                "end_time": float(det.get("end_time", 3)),
            })

        logger.debug("%s → %d detections", wav_path.name, len(results))
        return results
