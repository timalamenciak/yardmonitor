"""MegaDetector v5 — animal / person / vehicle detector via PytorchWildlife."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)

# MDv5 uses 1-indexed categories matching the original MegaDetector JSON format
CATEGORIES: dict[str, str] = {
    "1": "animal",
    "2": "person",
    "3": "vehicle",
}


class MegaDetector:
    """Batch-detect animals, persons, and vehicles in camera trap images."""

    def __init__(self, config: dict):
        cfg = config.get("megadetector", {})
        self.device: str = cfg.get("device", "cpu")
        self.batch_size: int = cfg.get("batch_size", 16)
        self.threshold: float = cfg.get("confidence_threshold", 0.1)
        self.version: str = cfg.get("model_version", "v5a").lower()
        self._model = None

    def _load_model(self) -> None:
        if self._model is not None:
            return
        try:
            from PytorchWildlife.models import detection as pw_detection

            cls = pw_detection.MegaDetectorV5B if self.version == "v5b" else pw_detection.MegaDetectorV5
            self._model = cls(device=self.device, pretrained=True)
            logger.info("Loaded MegaDetector %s on %s", self.version, self.device)
        except ImportError:
            raise ImportError(
                "PytorchWildlife is not installed. "
                "Run: pip install PytorchWildlife"
            )

    def detect_batch(self, image_paths: list[Path]) -> dict[str, list[dict]]:
        """
        Run detection on all images.

        Returns {filename: [detection, ...]} where each detection is:
          {category, category_name, conf, bbox: [x, y, w, h] normalized 0-1}
        """
        self._load_model()
        results: dict[str, list[dict]] = {p.name: [] for p in image_paths}

        for i in tqdm(
            range(0, len(image_paths), self.batch_size),
            desc="MegaDetector",
            unit="batch",
        ):
            for img_path in image_paths[i : i + self.batch_size]:
                try:
                    results[img_path.name] = self._detect_one(img_path)
                except Exception as exc:
                    logger.warning("Detection failed for %s: %s", img_path.name, exc)

        animal_count = sum(
            1 for dets in results.values()
            if any(d["category_name"] == "animal" for d in dets)
        )
        logger.info(
            "MegaDetector done — %d/%d images have animal detections",
            animal_count, len(image_paths),
        )
        return results

    def _detect_one(self, img_path: Path) -> list[dict]:
        from PIL import Image as PILImage

        img_arr = np.array(PILImage.open(img_path).convert("RGB"))
        img_h, img_w = img_arr.shape[:2]

        raw = self._model.single_image_detection(
            img=img_arr,
            img_path=str(img_path),
            conf_thres=self.threshold,
        )
        return _parse_detections(raw, img_w, img_h)


def _parse_detections(raw: dict, img_w: int, img_h: int) -> list[dict]:
    """Convert PytorchWildlife output to the canonical detection format."""
    det_obj = raw.get("detections") if isinstance(raw, dict) else None
    if det_obj is None:
        return []

    detections: list[dict] = []

    # Modern PytorchWildlife returns a supervision Detections object
    try:
        xyxy = det_obj.xyxy          # shape (N, 4), absolute pixel coords
        confs = det_obj.confidence    # shape (N,)
        classes = det_obj.class_id    # shape (N,), 0-indexed

        if xyxy is None or len(xyxy) == 0:
            return []

        for box, conf, cls in zip(xyxy, confs, classes):
            x1, y1, x2, y2 = (float(v) for v in box)
            cat = str(int(cls) + 1)
            detections.append({
                "category": cat,
                "category_name": CATEGORIES.get(cat, "unknown"),
                "conf": float(conf),
                "bbox": [x1 / img_w, y1 / img_h, (x2 - x1) / img_w, (y2 - y1) / img_h],
            })
    except AttributeError:
        # Fallback for older dict-based formats
        if isinstance(det_obj, list):
            for det in det_obj:
                cat = str(det.get("category", "1"))
                detections.append({
                    "category": cat,
                    "category_name": CATEGORIES.get(cat, "unknown"),
                    "conf": float(det.get("conf", 0)),
                    "bbox": det.get("bbox", []),
                })

    return detections
