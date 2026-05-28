#!/usr/bin/env python3
"""
YardMonitor Processing Server
==============================
Run this on your DGX (spark-1267) to handle all AI processing and serve the web UI.

    python server.py

Options:
  --config PATH   YAML config file (default: config/server.yaml)
  --models PATH   Model registry YAML (default: config/models.yaml)
  --host HOST     Bind address (default: 0.0.0.0)
  --port PORT     Port (default: 8000)
  --workers N     Pipeline worker threads (default: from config)

Access:
  Web UI:   http://spark-1267:8000
  API docs: http://spark-1267:8000/docs
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="YardMonitor Processing Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config",  default="config/server.yaml", help="Server config YAML")
    parser.add_argument("--models",  default="config/models.yaml", help="Model registry YAML")
    parser.add_argument("--host",    default=None, help="Bind address")
    parser.add_argument("--port",    type=int, default=None, help="TCP port")
    parser.add_argument("--workers", type=int, default=None, help="Pipeline worker threads")
    args = parser.parse_args()

    # Pass config paths to the app via environment variables before import
    os.environ["YARDMONITOR_CONFIG"] = args.config
    os.environ["YARDMONITOR_MODELS"] = args.models

    import yaml
    import uvicorn

    cfg: dict = {}
    if Path(args.config).exists():
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    srv = cfg.get("server", {})
    host    = args.host    or srv.get("host",    "0.0.0.0")
    port    = args.port    or srv.get("port",    8000)
    workers = args.workers or srv.get("pipeline_workers", 1)
    log_lvl = srv.get("log_level", "info")

    # Override worker count via env so the lifespan picks it up
    if args.workers:
        os.environ["YARDMONITOR_WORKERS"] = str(workers)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = host

    print()
    print("  YardMonitor Processing Server")
    print("  ─────────────────────────────────────────────")
    print(f"  Web UI:     http://{local_ip}:{port}")
    print(f"  Tailscale:  http://spark-1267:{port}")
    print(f"  API docs:   http://spark-1267:{port}/docs")
    print(f"  Config:     {args.config}")
    print(f"  Models:     {args.models}")
    print(f"  Workers:    {workers}")
    print("  Stop:       Ctrl+C")
    print()

    uvicorn.run(
        "yardmonitor.server.app:app",
        host=host,
        port=port,
        log_level=log_lvl,
        reload=False,
    )


if __name__ == "__main__":
    main()
