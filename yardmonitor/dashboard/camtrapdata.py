"""Read Camtrap DP packages from disk and return aggregated stats."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def get_camtrap_stats(deployments_dir: Path) -> dict:
    """
    Scan all deployment directories and aggregate:
      - per-deployment summaries
      - global totals
      - 20 most-recent animal observations
    """
    if not deployments_dir.exists():
        return _empty()

    deployments: list[dict] = []
    all_species: set[str] = set()
    total_images = 0
    total_animals = 0
    all_recent: list[dict] = []

    for dep_dir in sorted(deployments_dir.iterdir(), reverse=True):
        if not dep_dir.is_dir() or not (dep_dir / "deployments.csv").exists():
            continue
        info = _read_deployment(dep_dir)
        if info:
            deployments.append(info)
            total_images += info["image_count"]
            total_animals += info["animal_count"]
            all_species.update(info["species"])
            all_recent.extend(info.pop("_recent_obs", []))

    all_recent.sort(key=lambda o: o.get("timestamp", ""), reverse=True)

    return {
        "deployments": deployments,
        "total_deployments": len(deployments),
        "total_images": total_images,
        "total_animals": total_animals,
        "all_species": sorted(all_species),
        "recent_observations": all_recent[:20],
    }


def _read_deployment(dep_dir: Path) -> Optional[dict]:
    try:
        deps = _csv(dep_dir / "deployments.csv")
        if not deps:
            return None
        d = deps[0]

        media = _csv(dep_dir / "media.csv")
        obs = _csv(dep_dir / "observations.csv")

        animal_obs = [o for o in obs if o.get("observationType") == "animal"]
        species = {
            o["scientificName"]
            for o in animal_obs
            if o.get("scientificName")
        }

        recent = sorted(
            [o for o in animal_obs if o.get("scientificName")],
            key=lambda o: o.get("eventStart", ""),
            reverse=True,
        )[:5]

        return {
            "deployment_id": d.get("deploymentID", dep_dir.name),
            "location_name": d.get("locationName") or dep_dir.name,
            "lat": d.get("latitude", ""),
            "lon": d.get("longitude", ""),
            "start": _fmt_date(d.get("deploymentStart", "")),
            "end": _fmt_date(d.get("deploymentEnd", "")),
            "camera": f"{d.get('cameraMake','')} {d.get('cameraModel','')}".strip() or "—",
            "image_count": len(media),
            "animal_count": len(animal_obs),
            "species": sorted(species),
            "_recent_obs": [
                {
                    "deployment_id": dep_dir.name,
                    "location": d.get("locationName") or dep_dir.name,
                    "timestamp": o.get("eventStart", ""),
                    "timestamp_short": _fmt_date(o.get("eventStart", "")),
                    "scientific_name": o.get("scientificName", ""),
                    "confidence": _pct(o.get("classificationProbability", "")),
                    "method": o.get("classifiedBy", "machine"),
                }
                for o in recent
            ],
        }
    except Exception as exc:
        logger.warning("Failed to read %s: %s", dep_dir, exc)
        return None


def _csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _fmt_date(iso: str) -> str:
    if not iso:
        return "—"
    return iso[:10]  # keep YYYY-MM-DD


def _pct(val: str) -> str:
    try:
        return f"{float(val) * 100:.0f}%"
    except (ValueError, TypeError):
        return val or "—"


def _empty() -> dict:
    return {
        "deployments": [],
        "total_deployments": 0,
        "total_images": 0,
        "total_animals": 0,
        "all_species": [],
        "recent_observations": [],
    }
