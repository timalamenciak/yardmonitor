"""Google SpeciesNet — species classification on animal detections."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from tqdm import tqdm

logger = logging.getLogger(__name__)


class SpeciesNetClassifier:
    """Classify wildlife species using Google's SpeciesNet model."""

    def __init__(self, config: dict):
        cfg = config.get("speciesnet", {})
        self.enabled: bool = cfg.get("enabled", True)
        self.country: str = cfg.get("country", "")
        self.min_conf: float = cfg.get("min_detection_confidence", 0.2)
        self._available: Optional[bool] = None

    def classify_animals(
        self,
        images: list[Path],
        detections: dict[str, list[dict]],
    ) -> dict[str, list[dict]]:
        """
        Run species classification on images that contain animal detections.

        Returns {filename: [prediction, ...]} where each prediction is:
          {scientific_name, common_name, confidence, taxon_id}
        """
        base: dict[str, list[dict]] = {p.name: [] for p in images}

        if not self.enabled:
            return base

        if not self._check_available():
            logger.warning(
                "speciesnet not installed — skipping species classification. "
                "Run: pip install speciesnet"
            )
            return base

        animal_images = [
            p for p in images
            if any(
                d.get("category_name") == "animal" and d.get("conf", 0) >= self.min_conf
                for d in detections.get(p.name, [])
            )
        ]

        if not animal_images:
            logger.info("No qualifying animal detections — SpeciesNet not needed")
            return base

        logger.info("Running SpeciesNet on %d animal images", len(animal_images))
        return self._run(animal_images, base)

    # ── internal ──────────────────────────────────────────────────────────

    def _check_available(self) -> bool:
        if self._available is None:
            try:
                import speciesnet  # noqa: F401
                self._available = True
            except ImportError:
                self._available = False
        return self._available

    def _run(self, animal_images: list[Path], base: dict) -> dict:
        # Try Python API first, then fall back to subprocess CLI
        result = self._run_python_api(animal_images, base)
        if result is not None:
            return result
        return self._run_cli(animal_images, base)

    def _run_python_api(
        self, animal_images: list[Path], base: dict
    ) -> Optional[dict]:
        try:
            # SpeciesNet ships a run_model function callable from Python
            from speciesnet.scripts.run_model import run_model  # type: ignore

            with tempfile.TemporaryDirectory() as tmp:
                instances_path = Path(tmp) / "instances.json"
                out_path = Path(tmp) / "predictions.json"

                instances = [
                    {
                        "filepath": str(p),
                        **({"country": self.country} if self.country else {}),
                    }
                    for p in animal_images
                ]
                instances_path.write_text(
                    json.dumps({"instances": instances}), encoding="utf-8"
                )

                run_model(
                    instances_json=str(instances_path),
                    predictions_json=str(out_path),
                )

                if out_path.exists():
                    raw = json.loads(out_path.read_text(encoding="utf-8"))
                    return _parse_output(raw, base)

        except ImportError:
            pass
        except Exception as exc:
            logger.debug("SpeciesNet Python API failed: %s", exc)

        return None

    def _run_cli(self, animal_images: list[Path], base: dict) -> dict:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_path = Path(tmp) / "predictions.json"
                fp_arg = ",".join(str(p) for p in animal_images)

                cmd = [
                    "python", "-m", "speciesnet.scripts.run_model",
                    "--filepaths", fp_arg,
                    "--predictions_json", str(out_path),
                ]
                if self.country:
                    cmd += ["--country", self.country]

                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if proc.returncode == 0 and out_path.exists():
                    raw = json.loads(out_path.read_text(encoding="utf-8"))
                    return _parse_output(raw, base)
                logger.warning("SpeciesNet CLI error: %s", proc.stderr[:400])
        except Exception as exc:
            logger.warning("SpeciesNet CLI failed: %s", exc)

        return base


def _parse_output(raw: dict, base: dict) -> dict:
    """Merge SpeciesNet JSON predictions into the base results dict."""
    results = dict(base)
    for pred_block in raw.get("predictions", []):
        filename = Path(pred_block.get("filepath", "")).name
        preds = pred_block.get("predictions", [])
        if not preds or filename not in results:
            continue
        results[filename] = [
            {
                "scientific_name": p.get("label", ""),
                "common_name": p.get("common_name", ""),
                "confidence": float(p.get("score", 0)),
                "taxon_id": p.get("taxon_id", ""),
            }
            for p in preds
        ]
    return results
