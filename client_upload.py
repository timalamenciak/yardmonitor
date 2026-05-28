#!/usr/bin/env python3
"""
YardMonitor Client Upload Tool
================================
Run this on your laptop when you plug in an SD card from your trail camera
or AudioMoth.  It uploads the files to your DGX (spark-1267) over Tailscale
and queues an AI processing job automatically.

Usage:
    python client_upload.py --sensor-type camera_trap --location "backyard"
    python client_upload.py --sensor-type audiomoth   --location "pond"
    python client_upload.py --sensor-type camera_trap --drive D:\\ --location "front gate"

Options:
  --server URL       YardMonitor server (default: http://spark-1267:8000)
  --sensor-type      camera_trap | audiomoth
  --drive PATH       SD card path (auto-detected if omitted)
  --location NAME    Human-readable location name
  --sensor-id ID     Camera / AudioMoth serial or label
  --lat FLOAT        Deployment latitude (decimal degrees)
  --lon FLOAT        Deployment longitude (decimal degrees)
  --deployment-id ID Reuse an existing deployment ID (to add more files)
  --no-process       Upload files but do not trigger AI processing
  --dry-run          List files that would be uploaded without sending anything
"""

from __future__ import annotations

import argparse
import platform
import string
import sys
from pathlib import Path

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".cr2", ".nef", ".arw"}
_AUDIO_EXTS = {".wav", ".WAV", ".flac", ".mp3"}


def _collect_files(drive: Path, sensor_type: str) -> list[Path]:
    exts = _IMAGE_EXTS if sensor_type == "camera_trap" else _AUDIO_EXTS
    files = sorted(
        (p for p in drive.rglob("*") if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.name,
    )
    return files


def _detect_drive(sensor_type: str) -> Path | None:
    exts = _IMAGE_EXTS if sensor_type == "camera_trap" else _AUDIO_EXTS

    candidates: list[Path] = []
    system = platform.system()

    if system == "Windows":
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    root = f"{letter}:\\"
                    if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:
                        candidates.append(Path(root))
                bitmask >>= 1
        except Exception:
            pass
    elif system == "Darwin":
        candidates = [p for p in Path("/Volumes").iterdir() if p.is_dir()]
    else:
        for base in (Path("/media"), Path("/run/media")):
            if base.exists():
                for entry in base.iterdir():
                    if entry.is_dir():
                        candidates.extend(p for p in entry.iterdir() if p.is_dir())

    for drive in candidates:
        if any(p.suffix.lower() in exts for p in drive.rglob("*") if p.is_file()):
            return drive
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="YardMonitor Client Upload Tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--server",        default="http://spark-1267:8000", help="Server URL")
    parser.add_argument("--sensor-type",   required=True, choices=["camera_trap", "audiomoth"])
    parser.add_argument("--drive",         help="SD card path (auto-detected if omitted)")
    parser.add_argument("--location",      default="unknown", help="Location name")
    parser.add_argument("--sensor-id",     default="", help="Sensor serial / label")
    parser.add_argument("--lat",           type=float, help="Latitude (decimal)")
    parser.add_argument("--lon",           type=float, help="Longitude (decimal)")
    parser.add_argument("--deployment-id", help="Reuse existing deployment ID")
    parser.add_argument("--no-process",    action="store_true", help="Upload only, do not trigger AI")
    parser.add_argument("--dry-run",       action="store_true", help="List files without uploading")
    args = parser.parse_args()

    try:
        import requests
    except ImportError:
        print("ERROR: 'requests' is not installed.  Run: pip install requests", file=sys.stderr)
        sys.exit(1)

    try:
        from tqdm import tqdm
        _has_tqdm = True
    except ImportError:
        _has_tqdm = False

    server = args.server.rstrip("/")

    # ── 1. Locate drive ───────────────────────────────────────────────────
    if args.drive:
        drive = Path(args.drive)
        if not drive.exists():
            print(f"ERROR: drive path not found: {drive}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Detecting SD card…")
        drive = _detect_drive(args.sensor_type)
        if drive is None:
            print(
                "ERROR: No SD card detected.  Plug in your card or pass --drive <path>.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Found: {drive}")

    # ── 2. Collect files ──────────────────────────────────────────────────
    files = _collect_files(drive, args.sensor_type)
    if not files:
        print(f"ERROR: No {'image' if args.sensor_type == 'camera_trap' else 'audio'} "
              f"files found under {drive}", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(files)} files found")

    if args.dry_run:
        print("\n-- DRY RUN — files that would be uploaded:")
        for f in files:
            print(f"  {f}")
        return

    # ── 3. Create deployment on server ────────────────────────────────────
    print(f"\nConnecting to {server}…")
    try:
        resp = requests.post(
            f"{server}/api/deployments",
            data={
                "sensor_type":   args.sensor_type,
                "location_name": args.location,
                "sensor_id":     args.sensor_id,
                "latitude":      args.lat or "",
                "longitude":     args.lon or "",
                "deployment_id": args.deployment_id or "",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(f"ERROR: Cannot reach server at {server}.  "
              "Check that it's running and reachable over Tailscale.", file=sys.stderr)
        sys.exit(1)

    dep_id = resp.json()["deployment_id"]
    print(f"  Deployment: {dep_id}")
    print(f"  View at:    {server}/deployments/{dep_id}")

    # ── 4. Upload files ───────────────────────────────────────────────────
    print(f"\nUploading {len(files)} files…")
    upload_url = f"{server}/api/deployments/{dep_id}/upload"
    failed: list[Path] = []

    iterator = tqdm(files, unit="file") if _has_tqdm else files
    for fpath in iterator:
        try:
            with open(fpath, "rb") as f:
                r = requests.post(
                    upload_url,
                    files={"file": (fpath.name, f)},
                    timeout=120,
                )
                r.raise_for_status()
        except Exception as exc:
            failed.append(fpath)
            if not _has_tqdm:
                print(f"  WARN: failed {fpath.name}: {exc}")

    if failed:
        print(f"\nWARNING: {len(failed)} files failed to upload:")
        for f in failed[:10]:
            print(f"  {f.name}")

    print(f"\n  {len(files) - len(failed)}/{len(files)} files uploaded successfully")

    # ── 5. Trigger processing ─────────────────────────────────────────────
    if not args.no_process:
        print("\nQueueing AI processing job…")
        try:
            r = requests.post(
                f"{server}/api/deployments/{dep_id}/process", timeout=30
            )
            r.raise_for_status()
            job_id = r.json()["job_id"]
            print(f"  Job ID:  {job_id}")
        except Exception as exc:
            print(f"  WARNING: could not queue job: {exc}", file=sys.stderr)
            print(f"  You can trigger it manually at {server}/deployments/{dep_id}")

    print()
    print("  Done!")
    print(f"  Track progress:  {server}/deployments/{dep_id}")
    print(f"  All jobs:        {server}/jobs")
    print()


if __name__ == "__main__":
    main()
