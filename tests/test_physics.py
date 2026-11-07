"""Tests for pure calculations."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ventilation_advisor import physics
from custom_components.ventilation_advisor.settings import defaults

TZ = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")


def test_absolute_humidity_reference_values() -> None:
    assert physics.absolute_humidity(20, 50) == pytest.approx(8.63, abs=0.05)
    assert physics.absolute_humidity(0, 80) == pytest.approx(3.88, abs=0.05)


@pytest.mark.parametrize("delta", [25, 10, 5, 2, 0.7, 0, -25, -5, None])
def test_recommended_airing_profile(delta: float | None) -> None:
    settings = defaults()
    duration = physics.recommended_airing_duration_s(
        delta,
        settings["reference_airing_time"],
        settings["airing_time_lower_limit"],
        settings["airing_time_upper_limit"],
    )
    expected = 45 if not delta else min(45, max(5, 10 * (10 / abs(delta)) ** 0.5))
    assert duration == pytest.approx(expected * 60)
    assert duration * settings["maximum_airing_time_factor"] == pytest.approx(
        expected * 90
    )


@pytest.mark.parametrize(
    ("value", "previous", "expected"),
    [
        (23.5, "within", "within"),
        (23.51, "within", "above"),
        (23, "above", "above"),
        (22.5, "above", "within"),
        (18.5, "within", "within"),
        (18.49, "within", "below"),
        (19, "below", "below"),
        (19.5, "below", "within"),
        (17, "above", "below"),
        (25, "below", "above"),
    ],
)
def test_quantity_classification(value: float, previous: str, expected: str) -> None:
    assert physics.classify_quantity(value, 19, 23, 0.5, previous) == expected


def test_custom_airing_clamps_and_missing_values() -> None:
    assert physics.recommended_airing_duration_s(100, 8, 6, 20) == 360
    assert physics.recommended_airing_duration_s(0.01, 8, 6, 20) == 1200
    assert physics.recommended_airing_duration_s(10, 12, 6, 20) == 720
    assert physics.recommended_airing_duration_s(float("nan"), 8, 6, 20) == 1200
    assert physics.comfort_margin(24, 25, 0.5) == 0.25
    assert physics.comfort_margin(40, 45, 3) == 1.25


def test_reference_humidity_roundtrip_and_reheating() -> None:
    water = physics.absolute_humidity(16, 70)
    assert physics.relative_humidity(16, water) == pytest.approx(70)
    assert physics.relative_humidity(21, water) < 70
    assert physics.relative_humidity(
        21, physics.absolute_humidity(0, 80)
    ) == pytest.approx(21.18, abs=0.1)


@pytest.mark.parametrize(
    "direction,bound,indoor,outdoor", [(-1, 19, 19.5, 10), (1, 23, 22.5, 30)]
)
def test_adverse_effect_at_edges_and_hysteresis(
    direction, bound, indoor, outdoor
) -> None:
    assert physics.adverse_effect(indoor, outdoor, bound, direction, 0.5, 2)
    deeper = indoor - direction * 0.1
    assert not physics.adverse_effect(deeper, outdoor, bound, direction, 0.5, 2)
    assert physics.adverse_effect(deeper, outdoor, bound, direction, 0.5, 2, True)
    assert not physics.adverse_effect(
        bound - direction * 1.01, outdoor, bound, direction, 0.5, 2, True
    )
    assert not physics.adverse_effect(indoor, bound, bound, direction, 0.5, 2)


def test_meaningful_pull_threshold_and_release() -> None:
    assert physics.meaningful_pull(21, 19, -1, 2, False)
    assert not physics.meaningful_pull(21, 19.1, -1, 2, False)
    assert physics.meaningful_pull(21, 20, -1, 2, True)
    assert not physics.meaningful_pull(21, 20.01, -1, 2, True)


@pytest.mark.parametrize(
    ("hhmm", "start", "end", "quiet"),
    [
        ("21:59", "22:00", "07:00", False),
        ("22:00", "22:00", "07:00", True),
        ("03:00", "22:00", "07:00", True),
        ("07:00", "22:00", "07:00", False),
        ("13:00", "12:00", "14:00", True),
        ("14:00", "12:00", "14:00", False),
        ("13:00", "08:00", "08:00", False),
    ],
)
def test_is_quiet(hhmm: str, start: str, end: str, quiet: bool) -> None:
    h, m = map(int, hhmm.split(":"))
    local = datetime(2026, 1, 5, h, m, tzinfo=TZ)
    assert physics.is_quiet(local, start, end) is quiet


def test_next_boundary_and_last_end() -> None:
    now = datetime(2026, 1, 5, 23, 0, tzinfo=TZ)
    assert physics.next_boundary(now, "22:00", "07:00", TZ) == datetime(
        2026, 1, 6, 7, 0, tzinfo=TZ
    )
    assert physics.last_quiet_end(now, "22:00", "07:00", TZ) == datetime(
        2026, 1, 5, 7, 0, tzinfo=TZ
    )
    assert physics.next_boundary(now, "08:00", "08:00", TZ) is None


def test_boundaries_dst_spring_forward() -> None:
    # 2026-03-29: Berlin clocks jump from 02:00 to 03:00.
    now = datetime(2026, 3, 29, 0, 30, tzinfo=TZ)
    boundary = physics.next_boundary(now, "22:00", "02:30", TZ)
    assert boundary is not None
    assert boundary.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    # Normal boundary after the jump keeps wall clock time.
    nxt = physics.next_boundary(now, "22:00", "07:00", TZ)
    assert nxt is not None
    assert nxt.astimezone(UTC) == datetime(2026, 3, 29, 5, 0, tzinfo=UTC)


def test_boundaries_dst_fall_back() -> None:
    # 2026-10-25: Berlin clocks go back from 03:00 to 02:00.
    now = datetime(2026, 10, 24, 23, 0, tzinfo=TZ)
    nxt = physics.next_boundary(now, "22:00", "07:00", TZ)
    assert nxt is not None
    assert nxt.astimezone(UTC) == datetime(2026, 10, 25, 6, 0, tzinfo=UTC)
