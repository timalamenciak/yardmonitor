"""Export processed records to Camera Trap Data Package (Camtrap DP) format.

Spec: https://tdwg.github.io/camtrap-dp/
"""

from __future__ import annotations

import csv
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PROFILE = (
    "https://raw.githubusercontent.com/tdwg/camtrap-dp/1.0/camtrap-dp-profile.json"
)
_SCHEMA_BASE = "https://raw.githubusercontent.com/tdwg/camtrap-dp/1.0"


class CamtrapDPExporter:
    """
    Writes the four Camtrap DP artefacts into a deployment directory:
      datapackage.json  deployments.csv  media.csv  observations.csv
    """

    def __init__(self, config: dict):
        self.cfg = config.get("camtrap_dp", {})

    def export(
        self,
        deployment_dir: Path,
        deployment_meta: dict,
        image_records: list[dict],
    ) -> None:
        """
        Write / overwrite all Camtrap DP files.

        deployment_meta keys (all optional except deploymentID):
          deploymentID, locationID, locationName, latitude, longitude,
          coordinateUncertainty, deploymentStart, deploymentEnd,
          cameraID, cameraMake, cameraModel, cameraHeight, cameraHeading, cameraTilt

        Each image_record must contain:
          media_id, filename, relative_path, timestamp (datetime | None),
          capture_method, exif (dict), detections (list), species_predictions (list)
        """
        deployment_dir.mkdir(parents=True, exist_ok=True)
        dep_id = deployment_meta["deploymentID"]

        self._write_deployments_csv(deployment_dir, deployment_meta)
        self._write_media_csv(deployment_dir, dep_id, image_records)
        self._write_observations_csv(deployment_dir, dep_id, image_records)
        self._write_datapackage_json(deployment_dir, deployment_meta, image_records)

        logger.info("Camtrap DP package written to %s", deployment_dir)

    # ── CSV writers ───────────────────────────────────────────────────────

    def _write_deployments_csv(self, dest: Path, m: dict) -> None:
        rows = [
            {
                "deploymentID": m.get("deploymentID", ""),
                "locationID": m.get("locationID", m.get("deploymentID", "")),
                "locationName": m.get("locationName", ""),
                "latitude": _or_empty(m.get("latitude")),
                "longitude": _or_empty(m.get("longitude")),
                "coordinateUncertainty": _or_empty(m.get("coordinateUncertainty")),
                "deploymentStart": _fmt_dt(m.get("deploymentStart")),
                "deploymentEnd": _fmt_dt(m.get("deploymentEnd")),
                "cameraID": m.get("cameraID", ""),
                "cameraMake": m.get("cameraMake", ""),
                "cameraModel": m.get("cameraModel", ""),
                "cameraInterval": "",
                "cameraHeight": _or_empty(m.get("cameraHeight")),
                "cameraTilt": _or_empty(m.get("cameraTilt")),
                "cameraHeading": _or_empty(m.get("cameraHeading")),
                "detectionDistance": "",
                "timestampIssues": "false",
                "baitUse": "false",
                "featureType": m.get("featureType", ""),
                "habitat": m.get("habitat", ""),
                "deploymentGroups": "",
                "deploymentTags": "",
                "deploymentComments": "",
            }
        ]
        _write_csv(dest / "deployments.csv", rows)

    def _write_media_csv(
        self, dest: Path, dep_id: str, records: list[dict]
    ) -> None:
        rows = [
            {
                "mediaID": r["media_id"],
                "deploymentID": dep_id,
                "captureMethod": r.get("capture_method", "activityDetection"),
                "timestamp": _fmt_dt(r.get("timestamp")),
                "filePath": r.get("relative_path", r.get("filename", "")),
                "filePublic": "true",
                "fileName": r.get("filename", ""),
                "fileMediatype": _mediatype(r.get("filename", "")),
                "exifData": json.dumps(r.get("exif", {})) if r.get("exif") else "",
                "favorite": "false",
                "mediaComments": "",
            }
            for r in records
        ]
        _write_csv(dest / "media.csv", rows)

    def _write_observations_csv(
        self, dest: Path, dep_id: str, records: list[dict]
    ) -> None:
        rows: list[dict] = []
        now = datetime.now().isoformat()

        for rec in records:
            dets = rec.get("detections", [])
            species = rec.get("species_predictions", [])
            ts = _fmt_dt(rec.get("timestamp"))

            if not dets:
                rows.append(_blank_obs(rec, dep_id, ts, now))
                continue

            for det in dets:
                cat = det.get("category_name", "unknown")
                obs_type = _obs_type(cat)
                det_conf = det.get("conf", "")

                sci_name = common_name = taxon_id = ""
                conf = det_conf

                if obs_type == "animal" and species:
                    top = species[0]
                    sci_name = top.get("scientific_name", "")
                    common_name = top.get("common_name", "")
                    taxon_id = top.get("taxon_id", "")
                    conf = top.get("confidence", det_conf)

                rows.append(
                    {
                        "observationID": str(uuid.uuid4()),
                        "deploymentID": dep_id,
                        "mediaID": rec["media_id"],
                        "eventID": rec.get("event_id", rec["media_id"]),
                        "eventStart": ts,
                        "eventEnd": ts,
                        "observationLevel": "media",
                        "observationType": obs_type,
                        "cameraSetupType": "",
                        "taxonID": taxon_id,
                        "scientificName": sci_name,
                        "count": "",
                        "lifeStage": "",
                        "sex": "",
                        "behavior": "",
                        "individualID": "",
                        "classificationMethod": "machine",
                        "classifiedBy": "MegaDetector+SpeciesNet" if sci_name else "MegaDetector",
                        "classificationTimestamp": now,
                        "classificationProbability": f"{conf:.4f}" if conf != "" else "",
                        "observationTags": "",
                        "observationComments": "",
                    }
                )

        _write_csv(dest / "observations.csv", rows)

    # ── datapackage.json ──────────────────────────────────────────────────

    def _write_datapackage_json(
        self, dest: Path, meta: dict, records: list[dict]
    ) -> None:
        ts_list = [
            r["timestamp"]
            for r in records
            if isinstance(r.get("timestamp"), datetime)
        ]
        t_start = min(ts_list).isoformat() if ts_list else ""
        t_end = max(ts_list).isoformat() if ts_list else ""

        lat, lon = meta.get("latitude"), meta.get("longitude")
        spatial = (
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {},
            }
            if lat is not None and lon is not None
            else None
        )

        pkg: dict[str, Any] = {
            "$schema": _PROFILE,
            "name": meta.get("deploymentID", "deployment"),
            "id": str(uuid.uuid4()),
            "created": datetime.now().isoformat(),
            "title": (
                f"{self.cfg.get('project_name', 'YardMonitor')} — "
                f"{meta.get('locationName', '')}"
            ),
            "contributors": [],
            "description": "",
            "version": "1.0",
            "profile": _PROFILE,
            "licenses": [{"name": self.cfg.get("license", "CC-BY-4.0"), "scope": "data"}],
            "bibliographicCitation": "",
            "project": {
                "title": self.cfg.get("project_name", "YardMonitor"),
                "acronym": "",
                "description": "",
                "captureMethod": ["activityDetection"],
                "individualAnimals": False,
                "observationLevel": ["media"],
            },
            "temporal": {"start": t_start, "end": t_end},
            "taxonomic": _build_taxonomic(records),
            "resources": [
                {
                    "name": "deployments",
                    "path": "deployments.csv",
                    "profile": "tabular-data-resource",
                    "schema": f"{_SCHEMA_BASE}/deployments-table-schema.json",
                },
                {
                    "name": "media",
                    "path": "media.csv",
                    "profile": "tabular-data-resource",
                    "schema": f"{_SCHEMA_BASE}/media-table-schema.json",
                },
                {
                    "name": "observations",
                    "path": "observations.csv",
                    "profile": "tabular-data-resource",
                    "schema": f"{_SCHEMA_BASE}/observations-table-schema.json",
                },
            ],
        }
        if spatial:
            pkg["spatial"] = spatial

        with open(dest / "datapackage.json", "w", encoding="utf-8") as f:
            json.dump(pkg, f, indent=2, default=str)


# ── helpers ───────────────────────────────────────────────────────────────


def _blank_obs(rec: dict, dep_id: str, ts: str, now: str) -> dict:
    return {
        "observationID": str(uuid.uuid4()),
        "deploymentID": dep_id,
        "mediaID": rec["media_id"],
        "eventID": rec.get("event_id", rec["media_id"]),
        "eventStart": ts,
        "eventEnd": ts,
        "observationLevel": "media",
        "observationType": "blank",
        "cameraSetupType": "",
        "taxonID": "",
        "scientificName": "",
        "count": "",
        "lifeStage": "",
        "sex": "",
        "behavior": "",
        "individualID": "",
        "classificationMethod": "machine",
        "classifiedBy": "MegaDetector",
        "classificationTimestamp": now,
        "classificationProbability": "",
        "observationTags": "",
        "observationComments": "",
    }


def _obs_type(category_name: str) -> str:
    return {"animal": "animal", "person": "human", "vehicle": "vehicle"}.get(
        category_name, "unknown"
    )


def _mediatype(filename: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".cr2": "image/x-canon-cr2",
        ".nef": "image/x-nikon-nef",
        ".arw": "image/x-sony-arw",
    }.get(Path(filename).suffix.lower(), "image/jpeg")


def _fmt_dt(dt: Any) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _or_empty(v: Any) -> str:
    return "" if v is None else str(v)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.touch()
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.debug("Wrote %d rows → %s", len(rows), path)


def _build_taxonomic(records: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for rec in records:
        for sp in rec.get("species_predictions", []):
            name = sp.get("scientific_name", "")
            if name and name not in seen:
                seen[name] = {
                    "taxonID": sp.get("taxon_id", ""),
                    "taxonIDReference": "",
                    "scientificName": name,
                    "vernacularNames": {"en": sp.get("common_name", "")},
                }
    return list(seen.values())
