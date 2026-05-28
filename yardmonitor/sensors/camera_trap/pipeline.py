"""Camera trap pipeline orchestrator — ties all stages together."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from yardmonitor.core.pipeline import BasePipeline
from yardmonitor.utils.logging_utils import configure_logging
from yardmonitor.utils.storage import load_json, save_json

from .camtrap_dp import CamtrapDPExporter
from .exif import extract_exif_batch, parse_timestamp
from .ingest import collect_images, copy_images, detect_sd_card, make_deployment_id
from .megadetector import MegaDetector
from .speciesnet import SpeciesNetClassifier
from .timelapse import TimelapseGenerator

logger = logging.getLogger(__name__)


def _load_config(path: str | Path) -> dict:
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    logger.warning("Config not found at %s — using defaults", p)
    return {}


class CameraTrapPipeline(BasePipeline):
    """
    End-to-end camera trap pipeline.

    Stages (each is checkpointed and skipped on re-run):
      1. ingest        — copy images from SD card
      2. exif          — extract EXIF metadata
      3. megadetector  — detect animals / persons / vehicles
      4. speciesnet    — classify animal species
      5. timelapse     — generate timelapse video
      6. camtrap_dp    — write Camtrap DP CSV package
    """

    sensor_type = "camera_trap"

    def __init__(self, config_path: str | Path = "config/camera_trap.yaml"):
        config = _load_config(config_path)
        super().__init__(config)

        self.detector = MegaDetector(config)
        self.classifier = SpeciesNetClassifier(config)
        self.timelapse_gen = TimelapseGenerator(config)
        self.exporter = CamtrapDPExporter(config)

    # ── public API ────────────────────────────────────────────────────────

    def run(
        self,
        sd_card_path: Optional[str] = None,
        deployment_id: Optional[str] = None,
        location: str = "unknown",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        run_megadetector: bool = True,
        run_speciesnet: bool = True,
        run_timelapse: bool = True,
    ) -> dict:
        configure_logging("INFO")

        # ── 1. Locate SD card ─────────────────────────────────────────────
        sd_path = Path(sd_card_path) if sd_card_path else detect_sd_card(self.config)
        if sd_path is None:
            logger.error(
                "No SD card detected. Plug in your card or pass --sd-card <path>."
            )
            return {"status": "error", "reason": "no_sd_card"}

        logger.info("SD card: %s", sd_path)
        source_images = collect_images(sd_path, self.config)
        if not source_images:
            logger.error("No images found on SD card at %s", sd_path)
            return {"status": "error", "reason": "no_images"}

        # ── Resolve deployment paths ──────────────────────────────────────
        dep_id = deployment_id or make_deployment_id(location)
        output_base = Path(
            self.config.get("ingest", {}).get("output_dir", "data/deployments")
        )
        dep_dir = output_base / dep_id
        media_dir = dep_dir / "media"
        state_path = dep_dir / "pipeline_state.json"

        self.load_state(state_path)
        logger.info("Deployment: %s  →  %s", dep_id, dep_dir)

        # ── 2. Ingest ─────────────────────────────────────────────────────
        if not self.is_step_complete("ingest"):
            skip = self.config.get("ingest", {}).get("skip_existing", True)
            images = copy_images(source_images, media_dir, skip_existing=skip)
            self.mark_step_complete("ingest", state_path, {"count": len(images)})
        else:
            images = sorted(
                p for p in media_dir.glob("*")
                if p.is_file() and not p.name.endswith(".json")
            )
            logger.info("Ingest already done — %d images loaded", len(images))

        # ── 3. EXIF ───────────────────────────────────────────────────────
        exif_cache_path = dep_dir / "exif_cache.json"
        if not self.is_step_complete("exif"):
            logger.info("Extracting EXIF metadata…")
            exif_data = extract_exif_batch(images)
            save_json(exif_data, exif_cache_path)
            self.mark_step_complete("exif", state_path, {"count": len(exif_data)})
        else:
            exif_data = load_json(exif_cache_path) if exif_cache_path.exists() else {}
            logger.info("EXIF already done — %d records loaded", len(exif_data))

        # Build canonical per-image records
        records = self._build_records(images, exif_data, dep_id)

        # Derive deployment-level metadata
        dep_start, dep_end = _time_range(records)
        lat = latitude or self.config.get("deployment", {}).get("latitude") or _first_gps_lat(records)
        lon = longitude or self.config.get("deployment", {}).get("longitude") or _first_gps_lon(records)

        dep_meta = self._build_deployment_meta(
            dep_id, location, lat, lon, dep_start, dep_end
        )

        # ── 4. MegaDetector ───────────────────────────────────────────────
        md_cache_path = dep_dir / "megadetector_results.json"
        if run_megadetector and not self.is_step_complete("megadetector"):
            logger.info("Running MegaDetector on %d images…", len(images))
            try:
                detections = self.detector.detect_batch(images)
                save_json(detections, md_cache_path)
                self.mark_step_complete("megadetector", state_path)
            except Exception as exc:
                logger.error("MegaDetector failed: %s", exc)
                detections = {img.name: [] for img in images}
        elif self.is_step_complete("megadetector") and md_cache_path.exists():
            detections = load_json(md_cache_path)
            logger.info("MegaDetector already done — results loaded from cache")
        else:
            detections = {img.name: [] for img in images}

        for rec in records:
            rec["detections"] = detections.get(rec["filename"], [])

        # ── 5. SpeciesNet ─────────────────────────────────────────────────
        sn_cache_path = dep_dir / "speciesnet_results.json"
        if run_speciesnet and not self.is_step_complete("speciesnet"):
            logger.info("Running SpeciesNet…")
            try:
                species_results = self.classifier.classify_animals(images, detections)
                save_json(species_results, sn_cache_path)
                self.mark_step_complete("speciesnet", state_path)
            except Exception as exc:
                logger.error("SpeciesNet failed: %s", exc)
                species_results = {img.name: [] for img in images}
        elif self.is_step_complete("speciesnet") and sn_cache_path.exists():
            species_results = load_json(sn_cache_path)
            logger.info("SpeciesNet already done — results loaded from cache")
        else:
            species_results = {img.name: [] for img in images}

        for rec in records:
            rec["species_predictions"] = species_results.get(rec["filename"], [])

        # ── 6. Timelapse ──────────────────────────────────────────────────
        if run_timelapse and not self.is_step_complete("timelapse"):
            ts_map = {
                r["filename"]: r["timestamp"]
                for r in records
                if isinstance(r.get("timestamp"), datetime)
            }
            tl_path = self.timelapse_gen.generate(
                images=images,
                output_dir=dep_dir / "outputs",
                timestamps=ts_map or None,
                deployment_id=dep_id,
            )
            self.mark_step_complete(
                "timelapse", state_path,
                {"path": str(tl_path) if tl_path else None},
            )
        elif self.is_step_complete("timelapse"):
            logger.info("Timelapse already done")

        # ── 7. Camtrap DP ─────────────────────────────────────────────────
        logger.info("Writing Camtrap DP package…")
        self.exporter.export(dep_dir, dep_meta, records)
        self.mark_step_complete("camtrap_dp", state_path)

        # ── Summary ───────────────────────────────────────────────────────
        animal_images = sum(
            1
            for r in records
            if any(d.get("category_name") == "animal" for d in r.get("detections", []))
        )
        species_seen = {
            sp["scientific_name"]
            for r in records
            for sp in r.get("species_predictions", [])
            if sp.get("scientific_name")
        }
        summary = {
            "status": "complete",
            "deployment_id": dep_id,
            "deployment_dir": str(dep_dir),
            "image_count": len(images),
            "animal_images": animal_images,
            "species_detected": sorted(species_seen),
        }
        logger.info(
            "Pipeline complete — %d images, %d with animals, %d species identified",
            summary["image_count"],
            summary["animal_images"],
            len(summary["species_detected"]),
        )
        return summary

    # ── private helpers ───────────────────────────────────────────────────

    def _build_records(
        self, images: list[Path], exif_data: dict, dep_id: str
    ) -> list[dict]:
        records = []
        for img in images:
            exif = exif_data.get(img.name, {})
            ts = parse_timestamp(exif)
            records.append(
                {
                    "media_id": str(uuid.uuid4()),
                    "deployment_id": dep_id,
                    "filename": img.name,
                    "path": str(img),
                    "relative_path": f"media/{img.name}",
                    "timestamp": ts,
                    "capture_method": "activityDetection",
                    "exif": exif,
                    "detections": [],
                    "species_predictions": [],
                }
            )
        return records

    def _build_deployment_meta(
        self,
        dep_id: str,
        location: str,
        lat: Optional[float],
        lon: Optional[float],
        dep_start: Optional[datetime],
        dep_end: Optional[datetime],
    ) -> dict:
        cam = self.config.get("camera", {})
        dep = self.config.get("deployment", {})
        return {
            "deploymentID": dep_id,
            "locationID": dep_id,
            "locationName": location or dep.get("location_name", ""),
            "latitude": lat,
            "longitude": lon,
            "coordinateUncertainty": dep.get("coordinate_uncertainty"),
            "deploymentStart": dep_start,
            "deploymentEnd": dep_end,
            "cameraID": cam.get("id", ""),
            "cameraMake": cam.get("make", ""),
            "cameraModel": cam.get("model", ""),
            "cameraHeight": dep.get("height_m"),
            "cameraTilt": dep.get("tilt_degrees"),
            "cameraHeading": dep.get("heading_degrees"),
        }


# ── module-level helpers ──────────────────────────────────────────────────


def _time_range(
    records: list[dict],
) -> tuple[Optional[datetime], Optional[datetime]]:
    ts = [r["timestamp"] for r in records if isinstance(r.get("timestamp"), datetime)]
    return (min(ts), max(ts)) if ts else (None, None)


def _first_gps_lat(records: list[dict]) -> Optional[float]:
    for r in records:
        v = r.get("exif", {}).get("GPSLatitude")
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _first_gps_lon(records: list[dict]) -> Optional[float]:
    for r in records:
        v = r.get("exif", {}).get("GPSLongitude")
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# ── CLI entry point ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="YardMonitor — Camera Trap Pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sd-card", metavar="PATH",
        help="Path to SD card root (auto-detected if omitted)",
    )
    parser.add_argument(
        "--deployment-id", metavar="ID",
        help="Deployment ID, e.g. '20240615_backyard' (auto-generated if omitted)",
    )
    parser.add_argument("--location", default="unknown", help="Location name")
    parser.add_argument("--lat", type=float, metavar="DEG", help="Latitude (decimal)")
    parser.add_argument("--lon", type=float, metavar="DEG", help="Longitude (decimal)")
    parser.add_argument(
        "--config", default="config/camera_trap.yaml", help="Path to YAML config file",
    )
    parser.add_argument("--skip-megadetector", action="store_true")
    parser.add_argument("--skip-speciesnet", action="store_true")
    parser.add_argument("--skip-timelapse", action="store_true")

    args = parser.parse_args()

    pipeline = CameraTrapPipeline(config_path=args.config)
    result = pipeline.run(
        sd_card_path=args.sd_card,
        deployment_id=args.deployment_id,
        location=args.location,
        latitude=args.lat,
        longitude=args.lon,
        run_megadetector=not args.skip_megadetector,
        run_speciesnet=not args.skip_speciesnet,
        run_timelapse=not args.skip_timelapse,
    )

    if result.get("status") != "complete":
        sys.exit(1)
