"""Server-side AI pipeline runner.

Coordinates EXIF extraction, MegaDetector, SpeciesNet (camera trap) and
BirdNET-Analyzer (AudioMoth) for files that have already been uploaded to the
server.  Results are written to the SQLite database and exported as standard
data packages (Camtrap DP or Darwin Core Archive).
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .db import Database
from .models_registry import ModelsRegistry

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".cr2", ".nef", ".arw"}
_AUDIO_EXTS = {".wav", ".WAV", ".flac", ".mp3", ".ogg"}


class ServerPipelineRunner:
    """Runs AI pipelines for already-uploaded deployment data."""

    def __init__(
        self,
        config: dict,
        db: Database,
        models: ModelsRegistry,
        data_dir: str | Path = "data",
    ):
        self.config = config
        self.db = db
        self.models = models
        self.data_dir = str(data_dir)
        self._detector = None
        self._classifier = None
        self._birdnet = None

    def run(
        self, job_id: str, deployment_id: str, dep_dir: Path, sensor_type: str
    ) -> None:
        self._log(job_id, f"Starting {sensor_type} pipeline for {deployment_id}")
        if sensor_type == "camera_trap":
            self._run_camera_trap(job_id, deployment_id, dep_dir)
        elif sensor_type == "audiomoth":
            self._run_audiomoth(job_id, deployment_id, dep_dir)
        else:
            raise ValueError(f"Unknown sensor type: {sensor_type}")

    # ── Camera trap ───────────────────────────────────────────────────────

    def _run_camera_trap(
        self, job_id: str, deployment_id: str, dep_dir: Path
    ) -> None:
        from yardmonitor.sensors.camera_trap.exif import extract_exif_batch, parse_timestamp
        from yardmonitor.sensors.camera_trap.megadetector import MegaDetector
        from yardmonitor.sensors.camera_trap.speciesnet import SpeciesNetClassifier
        from yardmonitor.sensors.camera_trap.camtrap_dp import CamtrapDPExporter

        media_dir = dep_dir / "media"
        images = sorted(p for p in media_dir.rglob("*") if p.suffix.lower() in _IMAGE_EXTS)

        if not images:
            self._log(job_id, "No images found — nothing to process")
            return

        self._log(job_id, f"Found {len(images)} images")

        # EXIF
        self._log(job_id, "Extracting EXIF metadata...")
        exif_cache = dep_dir / "exif_cache.json"
        exif_data = extract_exif_batch(images)
        exif_cache.write_text(json.dumps(exif_data, default=str), encoding="utf-8")

        # Update media table with EXIF timestamps
        for img in images:
            exif = exif_data.get(img.name, {})
            ts = parse_timestamp(exif)
            self.db.upsert_media(
                deployment_id=deployment_id,
                filename=img.name,
                relative_path=f"media/{img.name}",
                captured_at=ts.isoformat() if ts else None,
                file_size=img.stat().st_size,
                mime_type=_mime(img.name),
                exif=exif,
            )

        # MegaDetector
        self._log(job_id, "Running MegaDetector...")
        detector = self._get_detector()
        md_cache = dep_dir / "megadetector_results.json"
        detections = detector.detect_batch(images)
        md_cache.write_text(json.dumps(detections, default=str), encoding="utf-8")
        md_model = f"MegaDetector{self.models.version('megadetector').upper()}"

        # SpeciesNet
        self._log(job_id, "Running SpeciesNet classification...")
        classifier = self._get_classifier()
        sn_cache = dep_dir / "speciesnet_results.json"
        species_results = classifier.classify_animals(images, detections)
        sn_cache.write_text(json.dumps(species_results, default=str), encoding="utf-8")
        sn_model = f"SpeciesNet-{self.models.version('speciesnet')}"

        # Write to DB and build records for Camtrap DP export
        self._log(job_id, "Writing observations to database...")
        self.db.delete_observations_for_deployment(deployment_id)
        records = []
        media_map = {m["filename"]: m for m in self.db.list_media(deployment_id)}

        for img in images:
            media_row = media_map.get(img.name)
            if not media_row:
                continue
            media_id = media_row["id"]
            dets = detections.get(img.name, [])
            sp_preds = species_results.get(img.name, [])
            ts_str = media_row.get("captured_at")

            if not dets:
                self.db.insert_observation(
                    deployment_id=deployment_id,
                    media_id=media_id,
                    sensor_type="camera_trap",
                    detection_type="blank",
                    detector_model=md_model,
                    observed_at=ts_str,
                )
            else:
                for det in dets:
                    cat = det.get("category_name", "unknown")
                    det_type = {"animal": "animal", "person": "person", "vehicle": "vehicle"}.get(cat, cat)
                    sci = common = taxon = ""
                    conf = det.get("conf")
                    if det_type == "animal" and sp_preds:
                        top = sp_preds[0]
                        sci = top.get("scientific_name", "")
                        common = top.get("common_name", "")
                        taxon = top.get("taxon_id", "")
                        conf = top.get("confidence", conf)
                    self.db.insert_observation(
                        deployment_id=deployment_id,
                        media_id=media_id,
                        sensor_type="camera_trap",
                        detection_type=det_type,
                        scientific_name=sci,
                        common_name=common,
                        confidence=conf,
                        taxon_id=taxon,
                        detector_model=md_model,
                        classifier_model=sn_model if sci else "",
                        observed_at=ts_str,
                        bbox=det.get("bbox"),
                    )

            exif = exif_data.get(img.name, {})
            ts = parse_timestamp(exif)
            records.append({
                "media_id": media_id,
                "deployment_id": deployment_id,
                "filename": img.name,
                "path": str(img),
                "relative_path": f"media/{img.name}",
                "timestamp": ts,
                "capture_method": "activityDetection",
                "exif": exif,
                "detections": dets,
                "species_predictions": sp_preds,
            })

        # Update deployment time range
        ts_list = [r["timestamp"] for r in records if r.get("timestamp")]
        if ts_list:
            self.db.update_deployment(
                deployment_id,
                start_dt=min(ts_list).isoformat(),
                end_dt=max(ts_list).isoformat(),
            )

        # Camtrap DP export
        self._log(job_id, "Exporting Camtrap DP package...")
        dep = self.db.get_deployment(deployment_id)
        dep_meta = {
            "deploymentID": deployment_id,
            "locationID": deployment_id,
            "locationName": dep.get("location_name", ""),
            "latitude": dep.get("latitude"),
            "longitude": dep.get("longitude"),
            "deploymentStart": ts_list and min(ts_list),
            "deploymentEnd": ts_list and max(ts_list),
            "cameraID": dep.get("sensor_id", ""),
        }
        exporter = CamtrapDPExporter(self.config)
        exporter.export(dep_dir, dep_meta, records)
        self._log(job_id, "Camera trap pipeline complete")

    # ── AudioMoth ─────────────────────────────────────────────────────────

    def _run_audiomoth(
        self, job_id: str, deployment_id: str, dep_dir: Path
    ) -> None:
        from yardmonitor.sensors.audiomoth.birdnet import BirdNetAnalyzer
        from yardmonitor.sensors.audiomoth.darwin_core import DarwinCoreExporter

        media_dir = dep_dir / "media"
        audio_files = sorted(p for p in media_dir.rglob("*") if p.suffix.lower() in _AUDIO_EXTS)

        if not audio_files:
            self._log(job_id, "No audio files found — nothing to process")
            return

        self._log(job_id, f"Found {len(audio_files)} audio files")

        dep = self.db.get_deployment(deployment_id)
        birdnet_params = self.models.params("birdnet")
        lat = dep.get("latitude") or birdnet_params.get("lat")
        lon = dep.get("longitude") or birdnet_params.get("lon")

        analyzer = self._get_birdnet()
        bn_model = f"BirdNET-{self.models.version('birdnet')}"

        self._log(job_id, "Loading BirdNET-Analyzer model...")
        try:
            analyzer.warm_up()
        except Exception as exc:
            self._log(job_id, f"ERROR: BirdNET model failed to load: {exc}")
            raise RuntimeError(f"BirdNET failed to load: {exc}") from exc
        self._log(job_id, "BirdNET-Analyzer model loaded")

        self._log(job_id, "Running BirdNET-Analyzer...")
        all_results: dict[str, list[dict]] = {}
        file_errors = 0
        for wav in audio_files:
            try:
                dets = analyzer.analyze(
                    wav,
                    lat=lat,
                    lon=lon,
                    date=_parse_audiomoth_date(wav.name),
                )
                all_results[wav.name] = dets
            except Exception as exc:
                file_errors += 1
                self._log(job_id, f"WARNING: BirdNET failed for {wav.name}: {exc}")
                all_results[wav.name] = []

        if file_errors:
            self._log(job_id, f"BirdNET completed with {file_errors}/{len(audio_files)} file errors")

        bn_cache = dep_dir / "birdnet_results.json"
        bn_cache.write_text(json.dumps(all_results, default=str), encoding="utf-8")

        # Update media + observations in DB
        self._log(job_id, "Writing observations to database...")
        self.db.delete_observations_for_deployment(deployment_id)

        for wav in audio_files:
            media_id = self.db.upsert_media(
                deployment_id=deployment_id,
                filename=wav.name,
                relative_path=f"media/{wav.name}",
                captured_at=_parse_audiomoth_date(wav.name),
                file_size=wav.stat().st_size,
                mime_type="audio/wav",
            )
            dets = all_results.get(wav.name, [])
            wav_ts = _parse_audiomoth_date(wav.name)
            if not dets:
                self.db.insert_observation(
                    deployment_id=deployment_id,
                    media_id=media_id,
                    sensor_type="audiomoth",
                    detection_type="blank",
                    detector_model=bn_model,
                    observed_at=wav_ts,
                )
            else:
                for det in dets:
                    obs_ts = _offset_timestamp(wav_ts, det.get("start_time", 0))
                    self.db.insert_observation(
                        deployment_id=deployment_id,
                        media_id=media_id,
                        sensor_type="audiomoth",
                        detection_type="audio_species",
                        scientific_name=det.get("scientific_name", ""),
                        common_name=det.get("common_name", ""),
                        confidence=det.get("confidence"),
                        detector_model=bn_model,
                        observed_at=obs_ts,
                        meta={
                            "start_time": det.get("start_time"),
                            "end_time": det.get("end_time"),
                        },
                    )

        # Darwin Core Archive export
        self._log(job_id, "Exporting Darwin Core Archive...")
        obs_rows = []
        for wav in audio_files:
            for det in all_results.get(wav.name, []):
                obs_rows.append({
                    "filename": wav.name,
                    "scientific_name": det.get("scientific_name", ""),
                    "common_name": det.get("common_name", ""),
                    "confidence": det.get("confidence"),
                    "start_time": det.get("start_time"),
                    "end_time": det.get("end_time"),
                    "observed_at": _offset_timestamp(
                        _parse_audiomoth_date(wav.name), det.get("start_time", 0)
                    ),
                })

        exporter = DarwinCoreExporter(self.config)
        exporter.export(dep_dir, dep, obs_rows, bn_model)
        self._log(job_id, "AudioMoth pipeline complete")

    # ── Model singletons ──────────────────────────────────────────────────

    def _get_detector(self):
        if self._detector is None:
            from yardmonitor.sensors.camera_trap.megadetector import MegaDetector
            self._detector = MegaDetector({
                "megadetector": self.models.params("megadetector")
            })
        return self._detector

    def _get_classifier(self):
        if self._classifier is None:
            from yardmonitor.sensors.camera_trap.speciesnet import SpeciesNetClassifier
            self._classifier = SpeciesNetClassifier({
                "speciesnet": self.models.params("speciesnet")
            })
        return self._classifier

    def _get_birdnet(self):
        if self._birdnet is None:
            from yardmonitor.sensors.audiomoth.birdnet import BirdNetAnalyzer
            self._birdnet = BirdNetAnalyzer(self.models.params("birdnet"))
        return self._birdnet

    def _log(self, job_id: str, msg: str) -> None:
        logger.info("[job %s] %s", job_id, msg)
        self.db.append_job_log(job_id, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ── helpers ───────────────────────────────────────────────────────────────


def _mime(filename: str) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".tiff": "image/tiff", ".tif": "image/tiff",
        ".cr2": "image/x-canon-cr2", ".nef": "image/x-nikon-nef",
        ".arw": "image/x-sony-arw",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


def _parse_audiomoth_date(filename: str) -> Optional[str]:
    """Parse AudioMoth filename like '20240615_183000.WAV' → ISO datetime string."""
    import re
    m = re.match(r"(\d{8})_(\d{6})", filename)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            return dt.isoformat()
        except ValueError:
            pass
    return None


def _offset_timestamp(base_ts: Optional[str], offset_secs: float) -> Optional[str]:
    if not base_ts:
        return None
    from datetime import timedelta
    try:
        dt = datetime.fromisoformat(base_ts)
        return (dt + timedelta(seconds=offset_secs)).isoformat()
    except ValueError:
        return base_ts
