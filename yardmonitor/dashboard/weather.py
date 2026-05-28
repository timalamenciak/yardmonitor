"""Environment Canada weather data via the Datamart citypage XML feed."""

from __future__ import annotations

import csv
import difflib
import io
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SITE_LIST_URL = "https://dd.weather.gc.ca/citypage_weather/docs/site_list_en.csv"
CITYPAGE_URL = "https://dd.weather.gc.ca/citypage_weather/xml/{province}/{site_code}_e.xml"

DATA_DIR = Path("data")
SITE_LIST_CACHE = DATA_DIR / "ec_site_list.csv"
WEATHER_CACHE = DATA_DIR / "weather_cache.json"
WEATHER_CACHE_TTL = 30 * 60  # 30 minutes

PROVINCE_NAME_TO_CODE: dict[str, str] = {
    "ontario": "ON",
    "québec": "QC",
    "quebec": "QC",
    "british columbia": "BC",
    "alberta": "AB",
    "manitoba": "MB",
    "saskatchewan": "SK",
    "nova scotia": "NS",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "newfoundland": "NL",
    "prince edward island": "PE",
    "northwest territories": "NT",
    "nunavut": "NU",
    "yukon": "YT",
    "yukon territory": "YT",
}

# EC icon code → weather emoji
EC_ICON: dict[str, str] = {
    "0": "☀️", "1": "🌤️", "2": "⛅", "3": "🌥️", "4": "🌥️",
    "5": "🌥️", "6": "🌥️", "7": "🌧️", "8": "🌩️", "9": "⛈️",
    "10": "☁️", "11": "🌫️", "12": "🌦️", "13": "🌦️", "14": "🌧️",
    "15": "🌨️", "16": "🌨️", "17": "❄️", "18": "🌧️", "19": "🌨️",
    "20": "🌨️", "21": "🌨️", "22": "🌩️", "23": "⛈️", "24": "⛈️",
    "25": "⛈️", "26": "🌬️", "27": "🌨️", "28": "🌧️", "29": "❓",
    "30": "🌙", "31": "🌙", "32": "🌥️", "33": "☁️", "34": "☁️",
    "35": "🌥️", "36": "🌧️", "37": "🌩️", "38": "🌦️", "39": "🌦️",
    "40": "⛈️", "41": "🌨️", "42": "🌨️", "43": "❄️", "44": "🌬️",
    "45": "🌧️", "46": "🌧️", "47": "🌨️", "48": "🌨️",
}


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode_address(address: str) -> Optional[dict]:
    """
    Convert a free-text address into coordinates + Canadian province code.
    Returns None if geocoding fails or the address is outside Canada.
    """
    try:
        geolocator = Nominatim(user_agent="yardmonitor_dashboard/0.1", timeout=10)
        location = geolocator.geocode(address, addressdetails=True, language="en")
    except (GeocoderTimedOut, GeocoderServiceError) as exc:
        logger.warning("Geocoder error: %s", exc)
        return None
    except Exception as exc:
        logger.error("Geocoding failed: %s", exc)
        return None

    if not location:
        return None

    addr = location.raw.get("address", {})
    country = addr.get("country_code", "").lower()
    if country != "ca":
        logger.warning("Address resolved outside Canada (country_code=%s)", country)
        return None

    province_raw = (
        addr.get("state") or addr.get("province") or addr.get("region") or ""
    ).lower()
    province_code = PROVINCE_NAME_TO_CODE.get(province_raw, "")

    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or address.split(",")[0].strip()
    )

    return {
        "lat": location.latitude,
        "lon": location.longitude,
        "city": city,
        "province_name": province_raw.title(),
        "province_code": province_code,
        "full_address": location.address,
    }


# ── EC site discovery ─────────────────────────────────────────────────────────

def get_site_list() -> list[dict]:
    """Download (and weekly-cache) EC's citypage site list CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if SITE_LIST_CACHE.exists():
        age_days = (datetime.now().timestamp() - SITE_LIST_CACHE.stat().st_mtime) / 86400
        if age_days < 7:
            return _parse_site_csv(SITE_LIST_CACHE.read_text(encoding="utf-8-sig"))

    try:
        r = requests.get(SITE_LIST_URL, timeout=20)
        r.raise_for_status()
        SITE_LIST_CACHE.write_bytes(r.content)
        return _parse_site_csv(r.content.decode("utf-8-sig"))
    except Exception as exc:
        logger.error("Failed to fetch EC site list: %s", exc)
        if SITE_LIST_CACHE.exists():
            return _parse_site_csv(SITE_LIST_CACHE.read_text(encoding="utf-8-sig"))
        return []


def _parse_site_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [
        {
            "name": (row.get("English Names") or "").strip(),
            "name_fr": (row.get("French Names") or "").strip(),
            "province": (row.get("Provinces") or "").strip(),
            "code": (row.get("Codes") or "").strip(),
        }
        for row in reader
        if (row.get("Codes") or "").strip()
    ]


def find_nearest_ec_site(city: str, province_code: str) -> Optional[dict]:
    """
    Return the best-matching EC citypage site for a city + province code.
    Uses exact match, then difflib fuzzy match, then prefix match.
    """
    sites = get_site_list()
    prov_sites = [s for s in sites if s["province"] == province_code]

    if not prov_sites:
        logger.warning("No EC sites for province %s", province_code)
        return None

    city_lower = city.lower().strip()

    # 1. Exact match
    for s in prov_sites:
        if s["name"].lower() == city_lower:
            return s

    # 2. Fuzzy match
    names = [s["name"].lower() for s in prov_sites]
    close = difflib.get_close_matches(city_lower, names, n=1, cutoff=0.55)
    if close:
        return next(s for s in prov_sites if s["name"].lower() == close[0])

    # 3. Prefix / containment
    for s in prov_sites:
        sn = s["name"].lower()
        if city_lower.startswith(sn) or sn.startswith(city_lower):
            return s

    logger.warning("No site matched '%s' in %s — using first province site", city, province_code)
    return prov_sites[0]


# ── Weather fetching + parsing ────────────────────────────────────────────────

def fetch_weather(province_code: str, site_code: str) -> Optional[dict]:
    """
    Return parsed weather dict (current conditions + forecast).
    Results are cached for WEATHER_CACHE_TTL seconds.
    """
    if WEATHER_CACHE.exists():
        try:
            cached = json.loads(WEATHER_CACHE.read_text(encoding="utf-8"))
            if datetime.now().timestamp() - cached.get("fetched_at", 0) < WEATHER_CACHE_TTL:
                return cached
        except Exception:
            pass

    url = CITYPAGE_URL.format(province=province_code, site_code=site_code)
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = _parse_citypage_xml(r.text)
        data["fetched_at"] = datetime.now().timestamp()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WEATHER_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    except Exception as exc:
        logger.error("EC weather fetch failed: %s", exc)
        if WEATHER_CACHE.exists():
            try:
                return json.loads(WEATHER_CACHE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None


def invalidate_weather_cache() -> None:
    if WEATHER_CACHE.exists():
        WEATHER_CACHE.unlink()


def _parse_citypage_xml(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)

    def _text(node, path: str, default: str = "") -> str:
        el = node.find(path) if node is not None else None
        return (el.text or "").strip() if el is not None else default

    def _attr(node, path: str, attr: str, default: str = "") -> str:
        el = node.find(path) if node is not None else None
        return el.get(attr, default) if el is not None else default

    # Location
    loc = root.find("location")
    location_name = _text(loc, "name")

    # Current conditions
    cur = root.find("currentConditions")
    current: dict = {}
    if cur is not None:
        current = {
            "station": _text(cur, "station"),
            "condition": _text(cur, "condition"),
            "icon_code": _text(cur, "iconCode"),
            "temperature": _text(cur, "temperature"),
            "temp_unit": _attr(cur, "temperature", "units", "°C"),
            "dewpoint": _text(cur, "dewpoint"),
            "humidex": _text(cur, "humidex"),
            "wind_chill": _text(cur, "windChill"),
            "pressure": _text(cur, "pressure"),
            "pressure_tendency": _attr(cur, "pressure", "tendency"),
            "visibility": _text(cur, "visibility"),
            "humidity": _text(cur, "relativeHumidity"),
            "wind_speed": _text(cur, "wind/speed"),
            "wind_gust": _text(cur, "wind/gust"),
            "wind_dir": _text(cur, "wind/direction"),
            "obs_time": _text(cur.find("dateTime[@name='observation']"), "textSummary")
            if cur.find("dateTime[@name='observation']") is not None
            else "",
        }
        current["icon"] = EC_ICON.get(current["icon_code"], "🌡️")
        # Feels-like: prefer wind chill in winter, humidex in summer
        current["feels_like"] = current["wind_chill"] or current["humidex"] or ""

    # Forecast
    forecast: list[dict] = []
    fg = root.find("forecastGroup")
    if fg is not None:
        for fc in fg.findall("forecast"):
            period_el = fc.find("period")
            period_name = period_el.get("textForecastName", "") if period_el is not None else ""

            abbr = fc.find("abbreviatedForecast")
            icon_code = _text(abbr, "iconCode")
            summary = _text(abbr, "textSummary")
            pop = _text(abbr, "pop")

            temps_el = fc.find("temperatures/temperature")
            temp_val = (temps_el.text or "").strip() if temps_el is not None else ""
            temp_class = temps_el.get("class", "") if temps_el is not None else ""

            # Precipitation accumulation (not always present)
            precip_mm_raw = _text(fc, "precipitation/accumulation/amount")
            try:
                precip_mm = float(precip_mm_raw) if precip_mm_raw else 0.0
            except ValueError:
                precip_mm = 0.0

            forecast.append(
                {
                    "period": period_name,
                    "icon_code": icon_code,
                    "icon": EC_ICON.get(icon_code, "🌡️"),
                    "summary": summary,
                    "pop": pop,
                    "temperature": temp_val,
                    "temp_class": temp_class,
                    "precip_mm": precip_mm,
                }
            )

    # Yesterday's measured precipitation
    yest = root.find("yesterday")
    yesterday_precip_mm = 0.0
    if yest is not None:
        raw = _text(yest, "precip")
        try:
            yesterday_precip_mm = float(raw) if raw else 0.0
        except ValueError:
            pass

    return {
        "location_name": location_name,
        "current": current,
        "forecast": forecast[:10],
        "yesterday_precip_mm": yesterday_precip_mm,
    }
