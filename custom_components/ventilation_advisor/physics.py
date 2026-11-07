"""Pure calculations for comfort windows, airing durations and quiet hours."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, tzinfo

from .settings import parse_time


def absolute_humidity(temperature_c: float, relative_humidity: float) -> float:
    """Return absolute humidity in g/m³ (Magnus approximation).

    Raises ``ValueError`` for non-finite results.
    """
    value = (
        6.112
        * math.exp(17.67 * temperature_c / (temperature_c + 243.5))
        * relative_humidity
        * 2.1674
        / (273.15 + temperature_c)
    )
    if not math.isfinite(value):
        raise ValueError("non-finite absolute humidity")
    return value


def relative_humidity(temperature_c: float, water_content: float) -> float:
    """Convert absolute humidity in g/m³ to relative humidity at a temperature."""
    value = 100.0 * water_content / absolute_humidity(temperature_c, 100.0)
    if not math.isfinite(value):
        raise ValueError("non-finite relative humidity")
    return value


def recommended_airing_duration_s(
    delta_t: float | None,
    reference_minutes: float,
    shortest_minutes: float,
    longest_minutes: float,
) -> float:
    """Return the clamped square-root air-exchange time in seconds."""
    if delta_t is None or not math.isfinite(delta_t) or delta_t == 0:
        return longest_minutes * 60.0
    minutes = reference_minutes * math.sqrt(10.0 / abs(delta_t))
    return min(longest_minutes, max(shortest_minutes, minutes)) * 60.0


def comfort_margin(lower: float, upper: float, margin: float) -> float:
    """Keep hysteresis small enough for narrow comfort windows."""
    return min(margin, (upper - lower) / 4.0)


def classify_quantity(
    value: float, lower: float, upper: float, margin: float, previous: str = "within"
) -> str:
    """Classify a quantity with the same symmetric hysteresis on both edges."""
    if value > upper + margin:
        return "above"
    if value < lower - margin:
        return "below"
    if previous == "above" and value > upper - margin:
        return "above"
    if previous == "below" and value < lower + margin:
        return "below"
    return "within"


def meaningful_pull(
    indoor: float, outdoor: float, direction: int, minimum: float, active: bool
) -> bool:
    """Whether outside pulls meaningfully in the chosen direction (+1/-1)."""
    difference = direction * (outdoor - indoor)
    return difference >= (minimum / 2.0 if active else minimum)


def adverse_effect(
    indoor: float,
    outdoor: float,
    bound: float,
    direction: int,
    margin: float,
    minimum: float,
    active: bool = False,
) -> bool:
    """Detect an outward pull at an edge, releasing more deeply inside."""
    edge_distance = direction * (indoor - bound)
    near_edge = edge_distance >= -margin
    if active:
        near_edge = edge_distance >= -2.0 * margin
    return (
        near_edge
        and direction * (outdoor - bound) > 0
        and meaningful_pull(indoor, outdoor, direction, minimum, active)
    )


def _to_time(value: str) -> time:
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid time {value!r}")
    return time(parsed[0], parsed[1])


def is_quiet(local_now: datetime, start: str, end: str) -> bool:
    """Return whether ``local_now`` is inside the quiet interval [start, end)."""
    t_start, t_end = _to_time(start), _to_time(end)
    current = local_now.time().replace(second=0, microsecond=0)
    if t_start == t_end:
        return False
    if t_start < t_end:
        return t_start <= current < t_end
    return current >= t_start or current < t_end


def _local_at(day: datetime, t: time, tz: tzinfo) -> datetime:
    """Return the first real instant at or after wall-clock ``t`` on ``day``."""
    candidate = datetime.combine(day.date(), t, tzinfo=tz)
    # Round-tripping through UTC moves non-existent (DST gap) wall times forward.
    return candidate.astimezone(tz)


def next_boundary(now: datetime, start: str, end: str, tz: tzinfo) -> datetime | None:
    """Return the next quiet-hours boundary strictly after ``now``."""
    t_start, t_end = _to_time(start), _to_time(end)
    if t_start == t_end:
        return None
    local_now = now.astimezone(tz)
    candidates: list[datetime] = []
    for offset in range(3):
        day = local_now + timedelta(days=offset)
        for t in (t_start, t_end):
            instant = _local_at(day, t, tz)
            if instant > now:
                candidates.append(instant)
    return min(candidates) if candidates else None


def last_quiet_end(now: datetime, start: str, end: str, tz: tzinfo) -> datetime | None:
    """Return the most recent quiet-hours end at or before ``now``."""
    t_start, t_end = _to_time(start), _to_time(end)
    if t_start == t_end:
        return None
    local_now = now.astimezone(tz)
    for offset in range(3):
        instant = _local_at(local_now - timedelta(days=offset), t_end, tz)
        if instant <= now:
            return instant
    return None  # pragma: no cover
