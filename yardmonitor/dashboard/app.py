"""Flask dashboard for YardMonitor — camera trap data + EC weather."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

from .camtrapdata import get_camtrap_stats
from .watering import get_recommendation
from .weather import (
    fetch_weather,
    find_nearest_ec_site,
    geocode_address,
    invalidate_weather_cache,
)

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = "ym-local-dashboard-key-change-in-prod"

SETTINGS_FILE = Path("data/settings.json")
DEPLOYMENTS_DIR = Path("data/deployments")


# ── Settings helpers ──────────────────────────────────────────────────────────

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(s: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Context processor — injects settings into every template ──────────────────

@app.context_processor
def inject_globals():
    return {"settings": load_settings(), "now": time.time()}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    settings = load_settings()

    weather = None
    if settings.get("ec_province") and settings.get("ec_site_code"):
        weather = fetch_weather(settings["ec_province"], settings["ec_site_code"])

    stats = get_camtrap_stats(DEPLOYMENTS_DIR)

    watering = get_recommendation(
        weather or {},
        threshold_mm=float(settings.get("watering_threshold_mm", 10)),
        horizon_days=int(settings.get("watering_horizon_days", 2)),
    )

    return render_template("index.html", weather=weather, stats=stats, watering=watering)


@app.get("/settings")
def settings_page():
    return render_template("settings.html")


@app.post("/settings")
def settings_save():
    address = request.form.get("address", "").strip()
    if not address:
        flash("Please enter an address.", "error")
        return redirect(url_for("settings_page"))

    geo = geocode_address(address)
    if not geo:
        flash(
            "Could not geocode that address. "
            "Try a more complete address including city and province.",
            "error",
        )
        return redirect(url_for("settings_page"))

    if not geo.get("province_code"):
        flash(
            f"Detected location ({geo.get('full_address')}) "
            "doesn't appear to be in Canada. "
            "Environment Canada data is only available for Canadian locations.",
            "error",
        )
        return redirect(url_for("settings_page"))

    site = find_nearest_ec_site(geo["city"], geo["province_code"])

    settings = load_settings()
    settings.update(
        {
            "address": address,
            "lat": geo["lat"],
            "lon": geo["lon"],
            "city": geo["city"],
            "province_name": geo["province_name"],
            "province_code": geo["province_code"],
            "ec_province": geo["province_code"],
            "ec_site_code": site["code"] if site else "",
            "ec_site_name": site["name"] if site else geo["city"],
        }
    )
    # Persist watering thresholds if provided
    try:
        t = float(request.form.get("watering_threshold_mm", "").strip() or settings.get("watering_threshold_mm", 10))
        settings["watering_threshold_mm"] = max(1.0, t)
    except ValueError:
        pass
    try:
        h = int(request.form.get("watering_horizon_days", "").strip() or settings.get("watering_horizon_days", 2))
        settings["watering_horizon_days"] = max(1, min(h, 5))
    except ValueError:
        pass
    save_settings(settings)
    invalidate_weather_cache()

    station = site["name"] if site else geo["city"]
    flash(f"Location updated — using EC station: {station}", "success")
    return redirect(url_for("index"))


@app.post("/weather/refresh")
def weather_refresh():
    """Force a fresh weather fetch (ignores cache)."""
    invalidate_weather_cache()
    flash("Weather data refreshed.", "success")
    return redirect(url_for("index"))


# ── Dev entry point ───────────────────────────────────────────────────────────

def run_dev():
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=5000, debug=True)
