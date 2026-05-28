"""
Garden watering recommendation.

Uses EC citypage data:
  - yesterday_precip_mm  — measured precipitation at the station yesterday
  - forecast[].precip_mm — forecast accumulation per period (when EC provides it)
  - forecast[].pop       — probability of precipitation per period (% integer)

Decision logic:
  1. If yesterday's rain ≥ threshold → skip
  2. If forecast rain in the window ≥ threshold → skip
  3. If rain is likely (PoP ≥ 60 %) in the window AND yesterday had ≥ half the threshold → maybe
  4. If rain is likely but uncertain amount → maybe
  5. Otherwise → water
"""

from __future__ import annotations

DEFAULT_THRESHOLD_MM: float = 10.0   # mm that counts as meaningful garden watering
DEFAULT_HORIZON_DAYS: int = 2        # days ahead to examine


def get_recommendation(
    weather: dict,
    threshold_mm: float = DEFAULT_THRESHOLD_MM,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """
    Return a recommendation dict:
      verdict       "yes" | "no" | "maybe" | "unknown"
      reason        short headline string
      detail        one-sentence explanation
      yesterday_mm  float
      forecast_mm   float — sum of forecast accumulations in window
      high_pop      bool  — any period in window has PoP ≥ 60 %
      threshold_mm  float
    """
    if not weather:
        return _r("unknown", "No weather data",
                  "Set your location in Settings to get a watering recommendation.",
                  0, 0, False, threshold_mm)

    yesterday_mm: float = float(weather.get("yesterday_precip_mm") or 0)
    forecast: list[dict] = weather.get("forecast") or []

    # EC gives two periods per day (day + night), so horizon_days * 2 covers the window
    window = forecast[: horizon_days * 2]

    forecast_mm: float = sum(float(p.get("precip_mm") or 0) for p in window)
    high_pop: bool = any(int(p.get("pop") or 0) >= 60 for p in window)
    days_label = f"{horizon_days} day{'s' if horizon_days != 1 else ''}"

    # ── Decision tree ─────────────────────────────────────────────────────

    if yesterday_mm >= threshold_mm:
        return _r(
            "no",
            f"Skip — {yesterday_mm:.0f} mm fell yesterday",
            "Soil should still have adequate moisture from yesterday's rain.",
            yesterday_mm, forecast_mm, high_pop, threshold_mm,
        )

    if forecast_mm >= threshold_mm:
        return _r(
            "no",
            f"Skip — {forecast_mm:.0f} mm forecast over the next {days_label}",
            "Let the rain do the work.",
            yesterday_mm, forecast_mm, high_pop, threshold_mm,
        )

    if high_pop and yesterday_mm >= threshold_mm * 0.5:
        return _r(
            "maybe",
            f"Probably skip — {yesterday_mm:.0f} mm yesterday + rain likely soon",
            "Forecast amounts are low, but recent moisture and upcoming rain may be enough.",
            yesterday_mm, forecast_mm, high_pop, threshold_mm,
        )

    if high_pop:
        return _r(
            "maybe",
            "Rain is coming — amount uncertain",
            "Water drought-sensitive plants now; hardier ones can wait for the rain.",
            yesterday_mm, forecast_mm, high_pop, threshold_mm,
        )

    return _r(
        "yes",
        "Water today",
        f"Only {yesterday_mm:.0f} mm yesterday and no significant rain forecast in {days_label}.",
        yesterday_mm, forecast_mm, high_pop, threshold_mm,
    )


def _r(verdict, reason, detail, yesterday_mm, forecast_mm, high_pop, threshold_mm):
    return {
        "verdict": verdict,
        "reason": reason,
        "detail": detail,
        "yesterday_mm": yesterday_mm,
        "forecast_mm": forecast_mm,
        "high_pop": high_pop,
        "threshold_mm": threshold_mm,
    }
