"""AudioMoth audio recorder pipeline — local one-shot execution.

For server-based GPU processing on the DGX, use `server.py` instead.
This local pipeline is for running directly on a machine with the AudioMoth SD card.

Stages:
  1. ingest    — copy WAV files from SD card
  2. birdnet   — species ID via BirdNET-Analyzer
  3. export    — write Darwin Core Archive
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from yardmonitor.core.pipeline import BasePipeline
from yardmonitor.utils.logging_utils import configure_logging
from yardmonitor.utils.storage import load_json, save_json

from .birdnet import BirdNetAnalyzer
from .darwin_core import DarwinCoreExporter
from .ingest import collect_audio, copy_audio, detect_sd_card, make_deployment_id, parse_audiomoth_datetime

logger = logging.getLogger(__name__)


class AudioMothPipeline(BasePipeline):
    """Local processing pipeline for AudioMoth .WAV recordings."""

    sensor_type = "audiomoth"

    def __init__(self, config: dict | None = None):
        super().__init__(config or {})
        birdnet_params = config.get("birdnet", {}) if config else {}
        self.analyzer = BirdNetAnalyzer(birdnet_params)
        self.exporter = DarwinCoreExporter(config or {})

    def run(
        self,
        sd_card_path: Optional[str] = None,
        deployment_id: Optional[str] = None,
        location: str = "unknown",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        output_dir: str = "data/deployments",
        skip_existing: bool = True,
    ) -> dict:
        configure_logging("INFO")

        sd_path = Path(sd_card_path) if sd_card_path else detect_sd_card()
        if sd_path is None:
            logger.error("No AudioMoth SD card detected. Pass --sd-card <path>.")
            return {"status": "error", "reason": "no_sd_card"}

        audio_files = collect_audio(sd_path)
        if not audio_files:
            logger.error("No audio files found at %s", sd_path)
            return {"status": "error", "reason": "no_audio"}

        first_dt = parse_audiomoth_datetime(audio_files[0].name)
        dep_id = deployment_id or make_deployment_id(location, first_dt)
        dep_dir = Path(output_dir) / dep_id
        media_dir = dep_dir / "media"
        state_path = dep_dir / "pipeline_state.json"

        self.load_state(state_path)
        logger.info("Deployment: %s → %s", dep_id, dep_dir)

        # ── 1. Ingest ─────────────────────────────────────────────────────
        if not self.is_step_complete("ingest"):
            copied = copy_audio(audio_files, media_dir, skip_existing=skip_existing)
            self.mark_step_complete("ingest", state_path, {"count": len(copied)})
        else:
            copied = sorted(p for p in media_dir.glob("*") if p.is_file())
            logger.info("Ingest already done — %d files loaded", len(copied))

        # ── 2. BirdNET ────────────────────────────────────────────────────
        bn_cache_path = dep_dir / "birdnet_results.json"
        if not self.is_step_complete("birdnet"):
            logger.info("Running BirdNET-Analyzer on %d files…", len(copied))
            results: dict[str, list[dict]] = {}
            for wav in copied:
                try:
                    results[wav.name] = self.analyzer.analyze(
                        wav, lat=latitude, lon=longitude,
                        date=parse_audiomoth_datetime(wav.name),
                    )
                except Exception as exc:
                    logger.warning("BirdNET failed for %s: %s", wav.name, exc)
                    results[wav.name] = []
            save_json(results, bn_cache_path)
            self.mark_step_complete("birdnet", state_path, {"files": len(results)})
        else:
            results = load_json(bn_cache_path) if bn_cache_path.exists() else {}
            logger.info("BirdNET already done — results loaded")

        # ── 3. Export Darwin Core Archive ─────────────────────────────────
        obs_rows = []
        for wav in copied:
            for det in results.get(wav.name, []):
                base_dt = parse_audiomoth_datetime(wav.name)
                observed_at = None
                if base_dt:
                    from datetime import timedelta
                    observed_at = (base_dt + timedelta(seconds=det.get("start_time", 0))).isoformat()
                obs_rows.append({
                    "filename": wav.name,
                    "scientific_name": det.get("scientific_name", ""),
                    "common_name": det.get("common_name", ""),
                    "confidence": det.get("confidence"),
                    "start_time": det.get("start_time"),
                    "end_time": det.get("end_time"),
                    "observed_at": observed_at,
                })

        fake_dep = {"id": dep_id, "location_name": location, "latitude": latitude, "longitude": longitude}
        self.exporter.export(dep_dir, fake_dep, obs_rows, "BirdNET-Analyzer")
        self.mark_step_complete("export", state_path)

        # ── Summary ───────────────────────────────────────────────────────
        all_species = {
            det["scientific_name"]
            for dets in results.values()
            for det in dets
            if det.get("scientific_name")
        }
        summary = {
            "status": "complete",
            "deployment_id": dep_id,
            "deployment_dir": str(dep_dir),
            "file_count": len(copied),
            "species_detected": sorted(all_species),
        }
        logger.info(
            "Pipeline complete — %d files, %d species identified",
            summary["file_count"],
            len(summary["species_detected"]),
        )
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="YardMonitor — AudioMoth Pipeline")
    parser.add_argument("--sd-card", metavar="PATH")
    parser.add_argument("--deployment-id", metavar="ID")
    parser.add_argument("--location", default="unknown")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--output-dir", default="data/deployments")
    parser.add_argument(
        "--min-confidence", type=float, default=0.1,
        help="Minimum BirdNET confidence threshold (0-1)"
    )
    args = parser.parse_args()

    pipeline = AudioMothPipeline(config={"birdnet": {"min_confidence": args.min_confidence}})
    result = pipeline.run(
        sd_card_path=args.sd_card,
        deployment_id=args.deployment_id,
        location=args.location,
        latitude=args.lat,
        longitude=args.lon,
        output_dir=args.output_dir,
    )
    if result.get("status") != "complete":
        sys.exit(1)
