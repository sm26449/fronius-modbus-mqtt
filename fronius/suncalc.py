"""Sunrise/sunset calculation (NOAA simplified algorithm, stdlib-only).

Used by the night-window logic: a fixed 21:00–06:00 window drifts out of
sync with the real sun across the year (late-August sunrises after 06:30
caused a dawn restart-loop every morning; a June window starting at 21:00
would clip real evening production). With site coordinates configured, the
night window follows the actual sun instead.

Accuracy is a few minutes — plenty for a window that carries a
configurable margin on both edges.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone


def sun_events_utc(day: date, latitude: float,
                   longitude: float) -> tuple[datetime, datetime] | None:
    """Sunrise and sunset for ``day`` as UTC datetimes (NOAA, zenith 90.833°).

    Returns None during polar day/night at extreme latitudes.
    """
    n = day.timetuple().tm_yday
    gamma = 2.0 * math.pi / 365.0 * (n - 1 + 0.5)
    eqtime = 229.18 * (0.000075
                       + 0.001868 * math.cos(gamma)
                       - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma)
                       - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918
            - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat_r = math.radians(latitude)
    cos_ha = (math.cos(math.radians(90.833))
              / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    if cos_ha < -1.0 or cos_ha > 1.0:
        return None
    ha = math.degrees(math.acos(cos_ha))
    sunrise_min = 720.0 - 4.0 * (longitude + ha) - eqtime
    sunset_min = 720.0 - 4.0 * (longitude - ha) - eqtime
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (base + timedelta(minutes=sunrise_min),
            base + timedelta(minutes=sunset_min))


def is_solar_night(now: datetime, latitude: float, longitude: float,
                   margin_min: int = 30) -> bool:
    """Whether ``now`` (aware or naive-local) is solar night for this site.

    Night = after sunset + margin, or before sunrise + margin. The margin
    leans the window "late" on purpose: panels produce right up to sunset
    (so night must not start early), and inverters need light to boot (so
    dawn extends a little past sunrise).
    """
    if now.tzinfo is None:
        now = now.astimezone()
    events = sun_events_utc(now.date(), latitude, longitude)
    if events is None:
        return False                      # polar edge case — treat as day
    sunrise, sunset = events
    margin = timedelta(minutes=margin_min)
    return now >= sunset + margin or now <= sunrise + margin
