"""Business timezone helpers.

Database timestamps remain naive UTC for compatibility with the existing
schema. Conversion to a business timezone happens only at presentation and
local-period boundaries.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "America/Mexico_City"

TIMEZONE_CHOICES = (
    ("America/Mexico_City", "Ciudad de México"),
    ("America/Cancun", "Cancún"),
    ("America/Tijuana", "Tijuana"),
    ("America/Hermosillo", "Hermosillo"),
    ("America/Chihuahua", "Chihuahua"),
)

SUPPORTED_TIMEZONES = frozenset(value for value, _label in TIMEZONE_CHOICES)
UTC = timezone.utc


def safe_timezone_name(value):
    candidate = (value or "").strip()
    if candidate not in SUPPORTED_TIMEZONES:
        return DEFAULT_TIMEZONE
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE
    return candidate


def business_zone(value):
    name = safe_timezone_name(value)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def utc_naive(value):
    """Return an aware or naive datetime as naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def utc_to_local(value, timezone_name=DEFAULT_TIMEZONE):
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.astimezone(business_zone(timezone_name))


def local_today(timezone_name=DEFAULT_TIMEZONE, now_utc=None):
    now_utc = now_utc or datetime.now(UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    return now_utc.astimezone(business_zone(timezone_name)).date()


def local_date_bounds_utc(start_date, end_date, timezone_name=DEFAULT_TIMEZONE):
    """Return naive UTC boundaries for the local interval [start, end)."""
    zone = business_zone(timezone_name)
    start_local = datetime.combine(start_date, time.min, tzinfo=zone)
    end_local = datetime.combine(end_date, time.min, tzinfo=zone)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def local_day_bounds_utc(day, timezone_name=DEFAULT_TIMEZONE):
    return local_date_bounds_utc(
        day,
        date.fromordinal(day.toordinal() + 1),
        timezone_name,
    )
