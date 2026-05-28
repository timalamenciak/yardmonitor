"""Model registry — reads config/models.yaml and exposes current model metadata.

Keeping AI model versions in a YAML file (rather than hardcoded) makes it easy to
upgrade models by editing one line without touching pipeline code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, dict] = {
    "megadetector": {
        "version": "v5a",
        "description": "Microsoft MegaDetector v5",
        "source": "PytorchWildlife",
        "link": "https://github.com/microsoft/CameraTraps/blob/main/megadetector.md",
        "params": {"device": "cuda", "batch_size": 16, "confidence_threshold": 0.1},
    },
    "speciesnet": {
        "version": "1.0",
        "description": "Google SpeciesNet",
        "source": "speciesnet",
        "link": "https://github.com/google/speciesnet",
        "params": {"country": "", "min_detection_confidence": 0.2},
    },
    "birdnet": {
        "version": "2.4",
        "description": "Cornell Lab BirdNET-Analyzer v2.4",
        "source": "birdnetlib",
        "link": "https://github.com/kahst/BirdNET-Analyzer",
        "params": {
            "min_confidence": 0.1,
            "overlap": 0.0,
            "sensitivity": 1.0,
            "lat": None,
            "lon": None,
        },
    },
}


class ModelsRegistry:
    """Reads model config from YAML; falls back to built-in defaults."""

    def __init__(self, config_path: str | Path = "config/models.yaml"):
        self._path = Path(config_path)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self._data = raw.get("models", {})
            logger.info("Model registry loaded from %s", self._path)
        else:
            logger.warning(
                "Model config not found at %s — using built-in defaults", self._path
            )
            self._data = {}

    def reload(self) -> None:
        self._load()

    def get(self, model_name: str) -> dict[str, Any]:
        base = dict(_DEFAULTS.get(model_name, {}))
        overlay = self._data.get(model_name, {})
        if overlay:
            base.update(overlay)
            base["params"] = {
                **_DEFAULTS.get(model_name, {}).get("params", {}),
                **overlay.get("params", {}),
            }
        return base

    def params(self, model_name: str) -> dict[str, Any]:
        return self.get(model_name).get("params", {})

    def version(self, model_name: str) -> str:
        return self.get(model_name).get("version", "unknown")

    def all(self) -> dict[str, dict]:
        return {name: self.get(name) for name in _DEFAULTS}
