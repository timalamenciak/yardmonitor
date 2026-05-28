#!/usr/bin/env python3
"""
YardMonitor Dashboard
======================
Start the local web dashboard and make it available on your home network:

    python run_dashboard.py

Then open http://<your-server-ip>:5000 in any browser on the same network.

Options:
  --port PORT    TCP port to listen on (default: 5000)
  --host HOST    Bind address (default: 0.0.0.0 — all interfaces)
  --debug        Enable Flask debug/reload mode (dev only)

First-time setup:
  1. Open http://<server-ip>:5000/settings
  2. Enter your Canadian home address
  3. YardMonitor will find your nearest Environment Canada weather station

Note: this uses Flask's built-in WSGI server, which is fine for a single-home
dashboard. If you want HTTPS or to expose it outside your LAN, put nginx or
Caddy in front of it.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from yardmonitor.dashboard.app import app


def main() -> None:
    parser = argparse.ArgumentParser(description="YardMonitor Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind address (0.0.0.0 = all interfaces, 127.0.0.1 = localhost only)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    print()
    print("  🌿 YardMonitor Dashboard")
    print("  ─────────────────────────────────────────────")
    if args.host == "0.0.0.0":
        import socket
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            local_ip = "<server-ip>"
        print(f"  Local:    http://127.0.0.1:{args.port}")
        print(f"  Network:  http://{local_ip}:{args.port}")
    else:
        print(f"  URL:      http://{args.host}:{args.port}")
    print("  Stop:     Ctrl+C")
    print()

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
