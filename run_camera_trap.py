#!/usr/bin/env python3
"""
YardMonitor — Camera Trap Pipeline
====================================
Plug in your SD card, then run:

    python run_camera_trap.py

The pipeline will:
  1. Auto-detect your SD card and copy all images
  2. Extract EXIF metadata (requires ExifTool on PATH)
  3. Run MegaDetector to classify each image as animal/person/vehicle/blank
  4. Run SpeciesNet to identify animal species
  5. Generate a timelapse video (requires ffmpeg on PATH)
  6. Write a Camtrap DP-compliant data package (deployments / media / observations CSVs)

Results land in:  data/deployments/<YYYYMMDD_location>/

Common options
--------------
  --sd-card PATH          Override SD card auto-detection
  --location NAME         Location label, e.g. "backyard_east" (used in folder name)
  --lat FLOAT             Deployment latitude  (decimal degrees)
  --lon FLOAT             Deployment longitude (decimal degrees)
  --skip-megadetector     Skip animal detection (fast, no AI)
  --skip-speciesnet       Skip species classification
  --skip-timelapse        Skip timelapse video generation
  --config PATH           YAML config file (default: config/camera_trap.yaml)

Re-running the script on the same SD card is safe — completed steps are
checkpointed in pipeline_state.json and skipped automatically.
"""

import sys
from pathlib import Path

# Allow running directly from the repo root without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from yardmonitor.sensors.camera_trap.pipeline import main

if __name__ == "__main__":
    main()
